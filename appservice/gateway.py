import asyncio
import json
import logging
import urllib.parse
from typing import List

import urllib3
import websockets

import discord
from misc import dict_cls, log_except, request, wrap_async


class Gateway(object):
    def __init__(self, http: urllib3.PoolManager, token: str):
        self.http = http
        self.token = token
        self.logger = logging.getLogger("discord")
        self.cdn_url = "https://cdn.discordapp.com"
        self.Payloads = discord.Payloads(self.token)
        self.loop = self.websocket = None

        self.query_cache = {}

    @log_except
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.query_ev = asyncio.Event()

        self.heartbeat_task = None
        self.resume = False

        while True:
            try:
                await self.gateway_handler(self.get_gateway_url())
            except websockets.ConnectionClosedError:
                # TODO reconnect ?
                self.logger.exception("Quitting, connection lost.")
                break

            # Stop sending heartbeats until we reconnect.
            if self.heartbeat_task and not self.heartbeat_task.cancelled():
                self.heartbeat_task.cancel()

    def get_gateway_url(self) -> str:
        resp = self.send("GET", "/gateway")

        return resp["url"]

    async def heartbeat_handler(self, interval_ms: int) -> None:
        while True:
            await asyncio.sleep(interval_ms / 1000)
            await self.websocket.send(json.dumps(self.Payloads.HEARTBEAT()))

    def query_handler(self, data: dict) -> None:
        members = data["members"]
        guild_id = data["guild_id"]

        for member in members:
            user = member["user"]
            self.query_cache[guild_id].append(user)

        self.query_ev.set()

    def handle_otype(self, data: dict, otype: str) -> None:
        if data.get("embeds"):
            return  # TODO embeds

        if otype == "MESSAGE_CREATE" or otype == "MESSAGE_UPDATE":
            obj = discord.Message(data)
        elif otype == "MESSAGE_DELETE":
            obj = dict_cls(data, discord.DeletedMessage)
        elif otype == "TYPING_START":
            obj = dict_cls(data, discord.Typing)
        elif otype == "GUILD_MEMBERS_CHUNK":
            self.query_handler(data)
            return
        else:
            self.logger.info(f"Unknown OTYPE: {otype}")
            return

        func = getattr(self, f"on_{otype.lower()}", None)

        if not func:
            self.logger.warning(
                f"Function '{func}' not defined, ignoring message."
            )
            return

        try:
            func(obj)
        except Exception:
            self.logger.exception(f"Ignoring exception in {func}:")

    async def gateway_handler(self, gateway_url: str) -> None:
        async with websockets.connect(
            f"{gateway_url}/?v=8&encoding=json"
        ) as websocket:
            self.websocket = websocket
            async for message in websocket:
                data = json.loads(message)
                data_dict = data.get("d")

                opcode = data.get("op")

                seq = data.get("s")
                if seq:
                    self.Payloads.seq = seq

                if opcode == discord.GatewayOpCodes.DISPATCH:
                    otype = data.get("t")

                    if otype == "READY":
                        self.Payloads.session = data_dict["session_id"]

                        self.logger.info("READY")

                    else:
                        self.handle_otype(data_dict, otype)

                elif opcode == discord.GatewayOpCodes.HELLO:
                    heartbeat_interval = data_dict.get("heartbeat_interval")

                    self.logger.info(
                        f"Heartbeat Interval: {heartbeat_interval}"
                    )

                    # Send periodic hearbeats to gateway.
                    self.heartbeat_task = asyncio.ensure_future(
                        self.heartbeat_handler(heartbeat_interval)
                    )

                    await websocket.send(
                        json.dumps(
                            self.Payloads.RESUME()
                            if self.resume
                            else self.Payloads.IDENTIFY()
                        )
                    )

                elif opcode == discord.GatewayOpCodes.RECONNECT:
                    self.logger.info("Received RECONNECT.")

                    self.resume = True
                    await websocket.close()

                elif opcode == discord.GatewayOpCodes.INVALID_SESSION:
                    self.logger.info("Received INVALID_SESSION.")

                    self.resume = False
                    await websocket.close()

                elif opcode == discord.GatewayOpCodes.HEARTBEAT_ACK:
                    # NOP
                    pass

                else:
                    self.logger.info(
                        f"Unknown OP code {opcode}:\n"
                        f"{json.dumps(data, indent=4)}"
                    )

    @wrap_async
    async def query_member(self, guild_id: str, name: str) -> discord.User:
        """
        Query the members for a given guild and return the first match.
        """

        self.query_ev.clear()

        def query():
            if not self.query_cache.get(guild_id):
                self.query_cache[guild_id] = []

            user = [
                user
                for user in self.query_cache[guild_id]
                if name.lower() in user["username"].lower()
            ]

            return None if not user else discord.User(user[0])

        user = query()

        if user:
            return user

        if not self.websocket or self.websocket.closed:
            self.logger.warning("Not fetching members, websocket closed.")
            return

        await self.websocket.send(
            json.dumps(self.Payloads.QUERY(guild_id, name))
        )

        # Wait for our websocket to receive the chunk.
        await asyncio.wait_for(self.query_ev.wait(), timeout=5)

        return query()

    def get_channel(self, channel_id: str) -> discord.Channel:
        """
        Get the channel object for a given channel ID.
        """

        resp = self.send("GET", f"/channels/{channel_id}")

        return dict_cls(resp, discord.Channel)

    def get_emotes(self, guild_id: str) -> List[discord.Emote]:
        """
        Get all the emotes for a given guild.
        """

        resp = self.send("GET", f"/guilds/{guild_id}/emojis")

        return [dict_cls(emote, discord.Emote) for emote in resp]

    def get_members(self, guild_id: str) -> List[discord.User]:
        """
        Get all the members for a given guild.
        """

        resp = self.send(
            "GET", f"/guilds/{guild_id}/members", params={"limit": 1000}
        )

        return [discord.User(member["user"]) for member in resp]

    def create_webhook(self, channel_id: str, name: str) -> discord.Webhook:
        """
        Create a webhook with the specified name in a given channel.
        """

        resp = self.send(
            "POST", f"/channels/{channel_id}/webhooks", {"name": name}
        )

        return dict_cls(resp, discord.Webhook)

    def edit_webhook(
        self, content: str, message_id: str, webhook: discord.Webhook
    ) -> None:
        self.send(
            "PATCH",
            f"/webhooks/{webhook.id}/{webhook.token}/messages/"
            f"{message_id}",
            {"content": content},
        )

    def delete_webhook(
        self, message_id: str, webhook: discord.Webhook
    ) -> None:
        self.send(
            "DELETE",
            f"/webhooks/{webhook.id}/{webhook.token}/messages/"
            f"{message_id}",
        )

    def send_webhook(self, webhook: discord.Webhook, **kwargs) -> str:
        content = {
            **kwargs,
            # Disable 'everyone' and 'role' mentions.
            "allowed_mentions": {"parse": ["users"]},
        }

        resp = self.send(
            "POST",
            f"/webhooks/{webhook.id}/{webhook.token}",
            content,
            {"wait": True},
        )

        return resp["id"]

    def send_message(self, message: str, channel_id: str) -> None:
        self.send(
            "POST", f"/channels/{channel_id}/messages", {"content": message}
        )

    @request
    def send(
        self, method: str, path: str, content: dict = {}, params: dict = {}
    ) -> dict:
        endpoint = (
            f"https://discord.com/api/v8{path}?"
            f"{urllib.parse.urlencode(params)}"
        )
        headers = {
            "Authorization": f"Bot {self.token}",
            "Content-Type": "application/json",
        }

        # 'body' being an empty dict breaks "GET" requests.
        content = json.dumps(content) if content else None

        return self.http.request(
            method, endpoint, body=content, headers=headers
        )

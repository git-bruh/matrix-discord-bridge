import asyncio
import json
import logging
import urllib.parse
from typing import List, Dict

import urllib3
import websockets

import discord
from misc import dict_cls, log_except, request


class Gateway:
    def __init__(self, http: urllib3.PoolManager, token: str):
        self.http = http
        self.token = token
        self.logger = logging.getLogger("discord")
        self.Payloads = discord.Payloads(self.token)
        self.websocket = None

    @log_except
    async def run(self) -> None:
        self.heartbeat_task: asyncio.Future = None
        self.resume = False

        gateway_url = self.get_gateway_url()

        while True:
            try:
                await self.gateway_handler(gateway_url)
            except (
                websockets.ConnectionClosedError,
                websockets.InvalidMessage,
            ):
                self.logger.exception("Connection lost, reconnecting.")

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

    async def handle_resp(self, data: dict) -> None:
        data_dict = data["d"]

        opcode = data["op"]

        seq = data["s"]

        if seq:
            self.Payloads.seq = seq

        if opcode == discord.GatewayOpCodes.DISPATCH:
            otype = data["t"]

            if otype == "READY":
                self.Payloads.session = data_dict["session_id"]

                self.logger.info("READY")
            else:
                self.handle_otype(data_dict, otype)
        elif opcode == discord.GatewayOpCodes.HELLO:
            heartbeat_interval = data_dict.get("heartbeat_interval")

            self.logger.info(f"Heartbeat Interval: {heartbeat_interval}")

            # Send periodic hearbeats to gateway.
            self.heartbeat_task = asyncio.ensure_future(
                self.heartbeat_handler(heartbeat_interval)
            )

            await self.websocket.send(
                json.dumps(
                    self.Payloads.RESUME()
                    if self.resume
                    else self.Payloads.IDENTIFY()
                )
            )
        elif opcode == discord.GatewayOpCodes.RECONNECT:
            self.logger.info("Received RECONNECT.")

            self.resume = True
            await self.websocket.close()
        elif opcode == discord.GatewayOpCodes.INVALID_SESSION:
            self.logger.info("Received INVALID_SESSION.")

            self.resume = False
            await self.websocket.close()
        elif opcode == discord.GatewayOpCodes.HEARTBEAT_ACK:
            # NOP
            pass
        else:
            self.logger.info(
                "Unknown OP code: {opcode}\n{json.dumps(data, indent=4)}"
            )

    def handle_otype(self, data: dict, otype: str) -> None:
        if otype in ("MESSAGE_CREATE", "MESSAGE_UPDATE", "MESSAGE_DELETE"):
            obj = discord.Message(data)
        elif otype == "TYPING_START":
            obj = dict_cls(data, discord.Typing)
        elif otype == "GUILD_CREATE":
            obj = discord.Guild(data)
        elif otype == "GUILD_MEMBER_UPDATE":
            obj = discord.GuildMemberUpdate(data)
        elif otype == "GUILD_EMOJIS_UPDATE":
            obj = discord.GuildEmojisUpdate(data)
        else:
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
            self.logger.exception(f"Ignoring exception in '{func.__name__}':")

    async def gateway_handler(self, gateway_url: str) -> None:
        async with websockets.connect(
            f"{gateway_url}/?v=8&encoding=json"
        ) as websocket:
            self.websocket = websocket

            async for message in websocket:
                await self.handle_resp(json.loads(message))

    def get_channel(self, channel_id: str) -> discord.Channel:
        """
        Get the channel  for a given channel ID.
        """

        resp = self.send("GET", f"/channels/{channel_id}")

        return dict_cls(resp, discord.Channel)

    def get_channels(self, guild_id: str) -> Dict[str, discord.Channel]:
        """
        Get all channels for a given guild ID.
        """

        resp = self.send("GET", f"/guilds/{guild_id}/channels")

        return {channel["id"]: dict_cls(channel, discord.Channel) for channel in resp}

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

    def send_webhook(
        self,
        webhook: discord.Webhook,
        avatar_url: str,
        content: str,
        username: str,
    ) -> discord.Message:
        payload = {
            "avatar_url": avatar_url,
            "content": content,
            "username": username,
            # Disable 'everyone' and 'role' mentions.
            "allowed_mentions": {"parse": ["users"]},
        }

        resp = self.send(
            "POST",
            f"/webhooks/{webhook.id}/{webhook.token}",
            payload,
            {"wait": True},
        )

        return discord.Message(resp)

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
        payload = json.dumps(content) if content else None

        return self.http.request(
            method, endpoint, body=payload, headers=headers
        )

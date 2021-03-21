import asyncio
import json
import logging
import urllib.parse

import websockets

import discord
from misc import RequestError, dict_cls


class Gateway(object):
    def __init__(self, http, token):
        self.http = http
        self.token = token
        self.logger = logging.getLogger("discord")
        self.cdn_url = "https://cdn.discordapp.com"
        self.heartbeat_task = self.resume = self.seq = self.session = None

    async def run(self) -> None:
        while True:
            try:
                await self.gateway_handler(self.get_gateway_url())
            except websockets.ConnectionClosedError:
                # TODO reconnect ?
                self.logger.critical("Connection lost, quitting.")
                break

            # Stop sending heartbeats until we reconnect.
            if self.heartbeat_task and not self.heartbeat_task.cancelled():
                self.heartbeat_task.cancel()

    def get_gateway_url(self) -> str:
        resp = self.send("GET", "/gateway")

        return resp["url"]

    async def heartbeat_handler(self, websocket, interval_ms: int) -> None:
        while True:
            await asyncio.sleep(interval_ms / 1000)
            await websocket.send(
                json.dumps(
                    discord.Payloads(
                        self.token, self.seq, self.session
                    ).HEARTBEAT
                )
            )

    def handle_otype(self, data: dict, otype: str) -> None:
        if otype == "MESSAGE_CREATE" or otype == "MESSAGE_UPDATE":
            obj = self.get_message_object(data)
        elif otype == "MESSAGE_DELETE":
            obj = dict_cls(data, discord.DeletedMessage)
        elif otype == "TYPING_START":
            obj = dict_cls(data, discord.Typing)
        else:
            self.logger.info(f"Unknown OTYPE: {otype}")
            return

        func = f"on_{otype.lower()}"  # Eg. `on_message_create`

        try:
            getattr(self, func)(obj)
        except AttributeError:
            self.logger.warning(
                f"Function {func} not defined, ignoring message."
            )
        except Exception:
            self.logger.exception(f"Ignoring exception in {func}:")

    async def gateway_handler(self, gateway_url: str) -> None:
        async with websockets.connect(
            f"{gateway_url}/?v=8&encoding=json"
        ) as websocket:
            async for message in websocket:
                data = json.loads(message)
                data_dict = data.get("d")

                opcode = data.get("op")

                seq = data.get("s")
                if seq:
                    self.seq = seq

                if opcode == discord.GatewayOpCodes.DISPATCH:
                    otype = data.get("t")

                    if otype == "READY":
                        self.session = data_dict["session_id"]

                        self.logger.info("READY")

                    # TODO embeds
                    elif data_dict.get("embeds"):
                        pass

                    else:
                        # TODO remove temporary try except for testing.
                        try:
                            self.handle_otype(data_dict, otype)
                        except Exception:
                            self.logger.exception(
                                json.dumps(data_dict, indent=4)
                            )

                elif opcode == discord.GatewayOpCodes.HELLO:
                    heartbeat_interval = data_dict.get("heartbeat_interval")

                    self.logger.info(
                        f"Heartbeat Interval: {heartbeat_interval}"
                    )

                    # Send periodic hearbeats to gateway.
                    self.heartbeat_task = asyncio.ensure_future(
                        self.heartbeat_handler(websocket, heartbeat_interval)
                    )

                    payload = discord.Payloads(
                        self.token, self.seq, self.session
                    )

                    await websocket.send(
                        json.dumps(
                            payload.RESUME if self.resume else payload.IDENTIFY
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

        resp = self.http.request(
            method, endpoint, body=content, headers=headers
        )

        if resp.status < 200 or resp.status >= 300:
            raise RequestError(
                f"Failed to '{method}' '{resp.geturl()}':\n{resp.data}"
            )

        return {} if resp.status == 204 else json.loads(resp.data)

    # TODO clean up this stuff somehow ?

    def get_user_object(self, author: dict) -> discord.User:
        author_id = author["id"]
        avatar = author["avatar"]

        if not avatar:
            avatar_url = None
        else:
            avatar_ext = "gif" if avatar.startswith("a_") else "png"
            avatar_url = (
                f"{self.cdn_url}/avatars/{author_id}/{avatar}.{avatar_ext}"
            )

        return discord.User(
            avatar_url=avatar_url,
            discriminator=author["discriminator"],
            id=author_id,
            username=author["username"],
        )

    def get_message_object(self, message: dict) -> discord.Message:
        return discord.Message(
            attachments=message.get("attachments", []),
            author=self.get_user_object(message.get("author", {})),
            content=message["content"],
            channel_id=message["channel_id"],
            id=message["id"],
            reference=message.get("message_reference", {}).get("message_id"),
            webhook_id=message.get("webhook_id"),
        )

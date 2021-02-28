import asyncio
import json
import logging
import os

import aiohttp.web

import discord
import discord.ext.commands


class AppService(object):
    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.loop = asyncio.get_event_loop()

        self.base_url = "http://127.0.0.1:8008"

        self.token = "wfghWEGh3wgWHEf3478sHFWE"

        self.app = aiohttp.web.Application(client_max_size=None)
        self.add_routes()

        self.run_discord()

    def run_discord(self) -> None:
        allowed_mentions = discord.AllowedMentions(everyone=False, roles=False)
        command_prefix = "/"
        # Intents to fetch members from guild.
        intents = discord.Intents.default()
        intents.members = True

        self.discord_client = DiscordClient(
            self, allowed_mentions=allowed_mentions,
            command_prefix=command_prefix, intents=intents
        )

        self.loop.create_task(self.discord_client.start(
            os.getenv("TOKEN")
            )
        )

    def add_routes(self) -> None:
        self.app.router.add_route(
            "PUT", "/transactions/{transaction}", self.receive_event
        )
        # self.app.router.add_route("GET", "/rooms/{alias}", self.query_alias)

    def run(self, host: str = "127.0.0.1", port: int = 5000) -> None:
        self.ready.set()
        aiohttp.web.run_app(self.app, host=host, port=port)

    async def receive_event(self, transaction) \
            -> aiohttp.web_response.Response:
        json = await transaction.json()
        events = json["events"]

        for event in events:
            if await self.to_return(event):
                continue

            if event["type"] == "m.room.message":
                await self.handle_message(event)

        return aiohttp.web.Response(body=b"{}")

    async def to_return(self, event) -> bool:
        await self.discord_client.ready.wait()

        if event["sender"].startswith("@discord_"):
            return True

        return False

    async def handle_message(self, event: dict) -> None:
        # body = event["content"]["body"]
        # sender = event["sender"]
        print(event)

    '''
    async def send_webhook(self, event: dict) -> int:
        try:
            hook_message = await hook.send(
                username=author[:80], avatar_url=avatar,
                content=message, embed=embed, wait=True
            )

            message_store[event_id] = hook_message
            message_store[hook_message.id] = event_id
        except discord.errors.HTTPException as e:
            print(
            f"Failed to send message {event_id} to channel {channel.id}: {e}"
            )
    '''

    async def send(self, method, path, content=None, params={}, headers={},
                   api_path="/_matrix/client/r0") -> None:
        method = method.upper()

        if not content:
            content = {}

        headers["Content-Type"] = "application/json"
        content = json.dumps(content)

        params["access_token"] = self.token

        endpoint = self.base_url + api_path + path

        request = aiohttp.request(
            method, endpoint, params=params, data=content, headers=headers
        )

        async with request as response:
            return await response.json()

    async def register(self, user_id: int) -> dict:
        content = {
            "type": "m.login.application_service",
            "username": f"discord_{user_id}"
        }

        resp = await self.send(
            "POST", "/register", content, params={"access_token": self.token}
        )

        return resp

    async def join_room(self, alias: str, mxid: str) -> None:
        resp = await self.send(
            "POST", f"/join/{alias}", params={"user_id": mxid}
        )

        print(resp)

    async def send_message(self, room_id: str, content: str, mxid: str) -> str:
        content = await self.create_message_event(content)

        resp = await self.send(
            "PUT", f"/rooms/{room_id}/send/m.room.message/TODOtransactionID", content,
            params={"user_id": mxid}
        )

        print(resp)

    async def create_message_event(self, message: str) -> dict:
        content = {"body": message, "msgtype": "m.text"}

        return content


class DiscordClient(discord.ext.commands.Bot):
    def __init__(self, appservice, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.ready = asyncio.Event()
        self.appservice = appservice

    async def to_return(self, channel_id: int) -> bool:
        await self.appservice.ready.wait()

        # TODO
        return False

    async def on_ready(self) -> None:
        # TODO

        self.ready.set()

    async def on_message(self, message) -> None:
        # Process other stuff like cogs before ignoring the message.
        await self.process_commands(message)

        if await self.to_return(message.channel.id):
            return

        mxid, room_id = await self.wrap(message)

        await self.appservice.send_message(
            room_id, message.clean_content, mxid
        )

    async def wrap(self, message) -> None:
        # TODO proper database and check if user already exists
        resp = await self.appservice.register(message.author.id)
        mxid = resp["user_id"]

        # TODO database/config file
        room_id = os.environ("ROOM_ID")
        await self.appservice.join_room(room_id, mxid)

        # TODO set avatar, nickname

        return mxid, room_id


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    app = AppService()

    app.run()


if __name__ == "__main__":
    main()

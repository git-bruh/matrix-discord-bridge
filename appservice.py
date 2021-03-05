import asyncio
import json
import logging
import os
import sys
import uuid

from dataclasses import dataclass
from typing import Union

import aiohttp.web

import discord
import discord.ext.commands


def config_gen(config_file: str) -> dict:
    config_dict = {
        "as_token": "my-secret-token",
        "homeserver": "http://127.0.0.1:8008",
        "discord_cmd_prefix": "/",
        "discord_token": "my-secret-token",
        "bridge": []
    }

    if not os.path.exists(config_file):
        with open(config_file, "w") as f:
            json.dump(config_dict, f, indent=4)
            print(f"Configuration dumped to '{config_file}'")
            sys.exit()

    with open(config_file, "r") as f:
        return json.loads(f.read())


config = config_gen("config.json")


class AppService(object):
    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.loop = asyncio.get_event_loop()

        self.base_url = config["homeserver"]
        self.plain_url = self.base_url.split("://")[-1].split(":")[-1]

        self.token = config["as_token"]

        self.app = aiohttp.web.Application(client_max_size=None)
        self.add_routes()

        self.run_discord()

    @dataclass
    class Message(object):
        body: str
        channel_id: int
        event_id: str
        homeserver: str
        room_id: str
        sender: str
        sender_avatar: str

    @dataclass
    class RequestError(Exception):
        def __init__(self, code, content):
            super().__init__(f"{code}: {content}")

    def run_discord(self) -> None:
        allowed_mentions = discord.AllowedMentions(everyone=False, roles=False)
        command_prefix = config["discord_cmd_prefix"]

        # Intents to fetch members from Guilds.
        intents = discord.Intents.default()
        intents.members = True

        self.discord_client = DiscordClient(
            self, allowed_mentions=allowed_mentions,
            command_prefix=command_prefix, intents=intents
        )

        self.loop.create_task(
            self.discord_client.start(config["discord_token"])
        )

    def add_routes(self) -> None:
        self.app.router.add_route(
            "PUT", "/transactions/{transaction}", self.receive_event
        )
        # self.app.router.add_route("GET", "/rooms/{alias}", self.query_alias)

    def run(self, host: str = "127.0.0.1", port: int = 5000) -> None:
        self.ready.set()
        aiohttp.web.run_app(self.app, host=host, port=port)

    async def receive_event(self, transaction: aiohttp.web_request.Request) \
            -> aiohttp.web_response.Response:
        json = await transaction.json()
        events = json.get("events")

        for event in events:
            event_type = event.get("type")

            if event_type == "m.room.message":
                await self.handle_message(event)

        return aiohttp.web.Response(body=b"{}")

    async def to_return(self, event: dict) -> bool:
        await self.discord_client.ready.wait()

        if event.get("sender").startswith("@discord_"):
            return True

        return False

    async def get_message_object(self, event: dict) -> Message:
        body = event.get("content").get("body")
        event_id = event.get("event_id")
        homeserver = event.get("sender").split(":")[-1]
        room_id = event.get("room_id")
        # channel_id = await self.get_channel(room_id)
        channel_id = config["bridge"][0]
        sender = event.get("sender")
        sender_avatar = await self.get_avatar(sender)

        return self.Message(
            body, channel_id, event_id, homeserver,
            room_id, sender, sender_avatar
        )

    async def handle_message(self, event: dict) -> None:
        message = await self.get_message_object(event)

        # Ignore empty messages.
        if await self.to_return(event) or not message.body:
            return

        await self.send_webhook(message)

    async def send_webhook(self, message: Message) -> None:
        channel = self.discord_client.channel_cache[message.channel_id]

        hook_name = "matrix_bridge"

        hooks = await channel.webhooks()

        hook = discord.utils.get(hooks, name=hook_name)
        if not hook:
            hook = await channel.create_webhook(name=hook_name)

        try:
            await hook.send(
                username=message.sender[:80], avatar_url=message.sender_avatar,
                content=message.body, embed=None, wait=True
            )

            # message_cache[event_id] = hook_message
            # message_cache[hook_message.id] = event_id
        except discord.errors.HTTPException as e:
            print(
                f"Failed to send message {message.event_id} to channel "
                f"{channel.id}: {e}"
            )

    async def send(self, method: str, path: str = "",
                   content: Union[bytes, dict] = {}, params: dict = {},
                   content_type: str = "application/json",
                   endpoint: str = "/_matrix/client/r0") -> dict:
        method = method.upper()

        headers = {"Content-Type": content_type}

        if type(content) == dict:
            content = json.dumps(content)

        params["access_token"] = self.token

        endpoint = f"{self.base_url}{endpoint}{path}"

        while True:
            request = aiohttp.request(
                method, endpoint, params=params, data=content, headers=headers
            )

            async with request as response:
                if response.status < 200 or response.status >= 300:
                    raise self.RequestError(
                        response.status, await response.text()
                    )

                if response.status == 429:
                    await asyncio.sleep(
                        response.json()["retry_after_ms"] / 1000
                    )
                else:
                    return await response.json()

    async def register(self, user_id: int) -> dict:
        content = {"type": "m.login.application_service",
                   "username": f"discord_{user_id}"}

        resp = await self.send(
            "POST", "/register", content
        )

        return resp.get("user_id")

    async def get_avatar(self, mxid: str) -> str:
        resp = await self.send(
            "GET", f"/profile/{mxid}/avatar_url"
        )

        resp = resp.get("avatar_url")
        # User does not have an avatar.
        if not resp:
            return

        resp = resp[6:].split("/")
        hs, identifier = resp[0], resp[1]

        return f"{self.base_url}/_matrix/media/r0/download/{hs}/{identifier}"

    async def set_nick(self, nickname: str, mxid: str) -> None:
        await self.send(
            "PUT", f"/profile/{mxid}/displayname", {"displayname": nickname},
            params={"user_id": mxid}
        )

    async def set_avatar(self, avatar_uri: str, mxid: str) -> None:
        await self.send(
            "PUT", f"/profile/{mxid}/avatar_url", {"avatar_url": avatar_uri},
            params={"user_id": mxid}
        )

    async def upload(self, url: str) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.get(str(url)) as resp:
                file = await resp.read()
                content_type = resp.content_type

        resp = await self.send(
            "POST", content=file, content_type=content_type,
            params={"filename": f"{uuid.uuid4()}"},
            endpoint="/_matrix/media/r0/upload"
        )

        return resp.get("content_uri")

    async def join_room(self, alias: str, mxid: str) -> str:
        # Get the room's ID from it's alias.
        resp = await self.send(
            "GET", f"/directory/room/{alias.replace('#', '%23')}"
        )

        resp = await self.send(
            "POST", f"/join/{resp.get('room_id')}", params={"user_id": mxid}
        )

        return resp.get("room_id")

    async def send_message(self, room_id: str, content: str, mxid: str) -> str:
        content = await self.create_message_event(content)

        resp = await self.send(
            "PUT", f"/rooms/{room_id}/send/m.room.message/{uuid.uuid4()}",
            content, params={"user_id": mxid}
        )

        return resp.get("event_id")

    async def create_message_event(self, message: str) -> dict:
        content = {"body": message, "msgtype": "m.text"}

        return content


class DiscordClient(discord.ext.commands.Bot):
    def __init__(self, appservice, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.appservice = appservice

        self.ready = asyncio.Event()
        self.channel_cache = {}

    async def to_return(self, message: discord.Message) -> bool:
        await self.appservice.ready.wait()

        if message.channel.id not in config["bridge"] or \
                message.author.discriminator == "0000":
            return True

        return False

    async def on_ready(self) -> None:
        # Populate the channel cache.
        for channel in config["bridge"]:
            channel_obj = self.get_channel(int(channel))
            self.channel_cache[channel] = channel_obj

        self.ready.set()

    async def on_message(self, message: discord.Message) -> None:
        # Process other stuff like cogs before ignoring the message.
        await self.process_commands(message)

        if await self.to_return(message):
            return

        mxid, room_id = await self.wrap(message)

        await self.appservice.send_message(
            room_id, message.clean_content, mxid
        )

    async def wrap(self, message: discord.Message) -> None:
        # TODO Database
        mxid = await self.appservice.register(message.author.id)

        await self.appservice.set_nick(message.author.display_name, mxid)

        await self.appservice.set_avatar(
            await self.appservice.upload(message.author.avatar_url), mxid
        )

        # room_alias = f"#discord_{message.channel.id}:localhost"
        room_alias = "#logged_testing:localhost"
        room_id = await self.appservice.join_room(room_alias, mxid)

        return mxid, room_id


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    app = AppService()

    app.run()


if __name__ == "__main__":
    main()

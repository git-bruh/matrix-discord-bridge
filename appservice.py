import asyncio
import json
import logging
import os
import sqlite3
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
        "database": "bridge.db"
    }

    if not os.path.exists(config_file):
        with open(config_file, "w") as f:
            json.dump(config_dict, f, indent=4)
            print(f"Configuration dumped to '{config_file}'")
            sys.exit()

    with open(config_file, "r") as f:
        return json.loads(f.read())


config = config_gen("config.json")


class DataBase(object):
    def __init__(self, db_file) -> None:
        self.create(db_file)

    def create(self, db_file) -> None:
        exists = os.path.exists(db_file)

        self.conn = sqlite3.connect(db_file)
        self.conn.row_factory = self.dict_factory

        self.cur = self.conn.cursor()

        if exists:
            return

        self.execute(
            "CREATE TABLE bridge(room_id TEXT PRIMARY KEY, channel_id INT);"
        )

        self.execute(
            "CREATE TABLE users(mxid TEXT PRIMARY KEY, "
            "avatar_url TEXT, nickname TEXT);"
        )

    def dict_factory(self, cursor, row):
        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d

    def execute(self, operation) -> None:
        self.cur.execute(operation)
        self.conn.commit()

    def add_room(self, room_id: str, channel_id: int) -> None:
        self.execute(
            "INSERT INTO bridge (room_id, channel_id) "
            f"VALUES ('{room_id}', {channel_id})"
        )

    def add_user(self, mxid: str) -> None:
        self.execute(f"INSERT INTO users (mxid) VALUES ('{mxid}')")

    def get_channel(self, room_id: str) -> int:
        self.cur.execute("SELECT channel_id FROM bridge WHERE room_id = ?",
                         [room_id])

        room = self.cur.fetchone()

        # Return '0' if nothing is bridged.
        return 0 if not room else room["channel_id"]

    def list_channels(self) -> list:
        self.execute("SELECT channel_id FROM bridge")

        channels = self.cur.fetchall()

        # Returns '[]' if nothing is bridged.
        return [channel["channel_id"] for channel in channels]

    def query_user(self, mxid: str) -> bool:
        self.execute("SELECT mxid FROM users")

        users = self.cur.fetchall()

        return next((True for user in users if user["mxid"] == mxid), False)


class AppService(object):
    def __init__(self) -> None:
        self.ready = asyncio.Event()
        self.loop = asyncio.get_event_loop()
        self.base_url = config["homeserver"]
        self.plain_url = self.base_url.split("://")[-1].split(":")[0].replace(
            "127.0.0.1", "localhost"
        )

        self.token = config["as_token"]

        self.app = aiohttp.web.Application(client_max_size=None)
        self.db = DataBase(config["database"])

        self.add_routes()

        self.run_discord()

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

            print(event)

            if event_type == "m.room.member":
                await self.handle_member(event)
            elif event_type == "m.room.message":
                await self.handle_message(event)

        return aiohttp.web.Response(body=b"{}")

    async def to_return(self, event: dict) -> bool:
        await self.discord_client.ready.wait()

        if event.get("sender").startswith("@discord_"):
            return True

        return False

    @dataclass
    class Event(object):
        body: str
        channel_id: int
        event_id: str
        is_direct: bool
        homeserver: str
        room_id: str
        sender: str

    @dataclass
    class User(object):
        avatar_url: str
        display_name: str

    def get_event_object(self, event: dict) -> Event:
        content = event.get("content")

        body = content.get("body")
        event_id = event.get("event_id")
        homeserver = event.get("sender").split(":")[-1]
        is_direct = content.get("is_direct")
        room_id = event.get("room_id")
        channel_id = self.db.get_channel(room_id)
        sender = event.get("sender")

        return self.Event(
            body, channel_id, event_id, is_direct, homeserver, room_id, sender
        )

    async def get_user_object(self, mxid: str) -> User:
        avatar_url, display_name = await self.get_profile(mxid)

        return self.User(avatar_url, display_name)

    async def handle_member(self, event: dict) -> None:
        event = self.get_event_object(event)

        # Ignore invites from other homeservers.
        if event.sender.split(":")[-1] != self.plain_url:
            return

        # Join the direct message room.
        if event.is_direct:
            await self.join_room(event.room_id)

    async def handle_message(self, event: dict) -> None:
        message = self.get_event_object(event)
        user = await self.get_user_object(message.sender)

        # Ignore empty messages.
        if await self.to_return(event) or not message.body:
            return

        if message.body.startswith("!bridge"):
            try:
                channel = int(message.body.split()[-1])
            except ValueError:
                return

            await self.create_room(channel, message.sender)

            return

        if message.channel_id not in self.db.list_channels():
            return

        await self.send_webhook(message, user)

    async def send_webhook(self, message: Event, user: User) -> None:
        channel = self.discord_client.get_channel(message.channel_id)

        hook_name = "matrix_bridge"

        hooks = await channel.webhooks()

        hook = discord.utils.get(hooks, name=hook_name)
        if not hook:
            hook = await channel.create_webhook(name=hook_name)

        try:
            await hook.send(
                username=user.display_name[:80], avatar_url=user.avatar_url,
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
                    raise Exception(
                        f"{response.status}: {await response.text()}"
                    )

                if response.status == 429:
                    await asyncio.sleep(
                        response.json()["retry_after_ms"] / 1000
                    )
                else:
                    return await response.json()

    async def register(self, mxid: str) -> str:
        content = {"type": "m.login.application_service",
                   "username": mxid}

        resp = await self.send("POST", "/register", content)

        self.db.add_user(mxid)

        return resp["user_id"]

    async def create_room(self, channel_id: int, sender: str):
        room_alias = f"discord_{channel_id}"

        content = {
            "visibility": "private", "room_alias_name": room_alias,
            "invite": [sender], "creation_content": {"m.federate": True},
            "initial_state": [
                {"type": "m.room.join_rules",
                 "content": {"join_rule": "invite"}},
                {"type": "m.room.history_visibility",
                 "content": {"history_visibility": "shared"}}
            ], "power_level_content_override": {"users": {sender: 100}}
        }

        resp = await self.send("POST", "/createRoom", content)

        self.db.add_room(resp["room_id"], channel_id)

    async def get_profile(self, mxid: str) -> tuple:
        resp = await self.send(
            "GET", f"/profile/{mxid}"
        )

        avatar_url = resp.get("avatar_url")
        avatar_url = avatar_url[6:].split("/")
        try:
            avatar_url = f"{self.base_url}/_matrix/media/r0/download/" \
                         f"{avatar_url[0]}/{avatar_url[1]}"
        except IndexError:
            avatar_url = None

        display_name = resp.get("displayname")

        return avatar_url, display_name

    async def get_members(self, room_id: str) -> list:
        resp = await self.send(
            "GET", f"/rooms/{room_id}/members",
            params={"membership": "join", "not_membership": "leave"}
        )

        return [
            content["sender"] for content in resp["chunk"]
            if content["content"]["membership"] == "join"
        ]

    async def set_nick(self, nickname: str, mxid: str) -> None:
        await self.send(
            "PUT", f"/profile/{mxid}/displayname",
            {"displayname": nickname}, params={"user_id": mxid}
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

        resp = await self.send(
            "POST", content=file, content_type=resp.content_type,
            params={"filename": f"{uuid.uuid4()}"},
            endpoint="/_matrix/media/r0/upload"
        )

        return resp.get("content_uri")

    async def get_room_id(self, alias: str) -> str:
        resp = await self.send(
            "GET", f"/directory/room/{alias.replace('#', '%23')}"
        )

        return resp.get("room_id")

    async def join_room(self, room_id: str, mxid: str = "") -> str:
        params = {"user_id": mxid} if mxid else {}

        resp = await self.send("POST", f"/join/{room_id}", params=params)
        return resp.get("room_id")

    async def send_invite(self, room_id: str, mxid: str) -> None:
        await self.send(
            "POST", f"/rooms/{room_id}/invite", {"user_id": mxid}
        )

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

        self.app = appservice

        self.ready = asyncio.Event()

    async def to_return(self, message: discord.Message) -> bool:
        await self.app.ready.wait()

        if message.channel.id not in self.app.db.list_channels() or \
                message.author.discriminator == "0000":
            return True

        return False

    async def on_ready(self) -> None:
        self.ready.set()

    async def on_message(self, message: discord.Message) -> None:
        # Process other stuff like cogs before ignoring the message.
        await self.process_commands(message)

        if await self.to_return(message):
            return

        mxid, room_id = await self.wrap(message)

        await self.app.send_message(
            room_id, message.clean_content, mxid
        )

    async def wrap(self, message: discord.Message) -> tuple:
        mxid_tmp = f"_discord_{message.author.id}"
        mxid = f"@{mxid_tmp}:{self.app.plain_url}"

        room_alias = f"#discord_{message.channel.id}:{self.app.plain_url}"
        room_id = await self.app.get_room_id(room_alias)

        if not self.app.db.query_user(mxid_tmp):
            await self.app.register(mxid_tmp)

            await self.app.set_nick(
                f"{message.author.name}#{message.author.discriminator}", mxid
            )

            await self.app.set_avatar(
                await self.app.upload(message.author.avatar_url), mxid
            )

        if mxid not in await self.app.get_members(room_id):
            await self.app.send_invite(room_id, mxid)
            await self.app.join_room(room_id, mxid)

        return mxid, room_id


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    app = AppService()

    app.run()

    app.db.cur.close()
    app.db.conn.close()


if __name__ == "__main__":
    main()

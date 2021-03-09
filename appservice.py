import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import sys
import uuid
import urllib3
import bottle
import websockets
from dataclasses import dataclass
from typing import Union


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
            "avatar_url TEXT, username TEXT);"
        )

    def dict_factory(self, cursor, row):
        # https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.row_factory

        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d

    def execute(self, operation: str) -> None:
        self.cur.execute(operation) # TODO remove this useless function
        self.conn.commit()

    def add_room(self, room_id: str, channel_id: int) -> None:
        self.execute(
            "INSERT INTO bridge (room_id, channel_id) "
            f"VALUES ('{room_id}', {channel_id})"
        )

    def add_user(self, mxid: str) -> None:
        self.execute(f"INSERT INTO users (mxid) VALUES ('{mxid}')")

    def get_channel(self, room_id: str) -> int:
        self.cur.execute("SELECT channel_id FROM bridge WHERE room_id = ?", [room_id])

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


class AppService(bottle.Bottle):
    def __init__(self) -> None:
        super(AppService, self).__init__()

        self.base_url  = config["homeserver"]
        self.plain_url = self.base_url.split("://") \
            [-1].split(":")[0].replace("127.0.0.1", "localhost")
        self.db        = DataBase(config["database"])
        self.discord   = DiscordClient(self)
        self.token     = config["as_token"]
        self.manager   = urllib3.PoolManager()

        # Add route for bottle.
        self.route("/transactions/<transaction>",
                   callback=self.receive_event, method="PUT")

    def start(self):
        self.run(host="127.0.0.1", port=5000)

        # TODO
        logging.info("Closing database")

        self.db.cur.close()
        self.db.conn.close()

    def receive_event(self, transaction: str) -> dict:
        """
        The homeserver hits this endpoint to send us new events.
        """

        events = bottle.request.json.get("events")

        for event in events:
            event_type = event.get("type")

            if event_type == "m.room.member":
                self.handle_member(event)
            elif event_type == "m.room.message":
                self.handle_message(event)

        return {}

    def send(self, method: str, content: Union[bytes, dict],
             content_type: str = "application/json",
             path: str = "", params: dict = {},
             endpoint: str = "/_matrix/client/r0") -> dict:
        headers  = {"Content-Type": content_type}
        content  = json.dumps(content) if type(content) == dict else content
        endpoint = f"{self.base_url}{endpoint}{path}"
        params["access_token"] = self.token

        resp = self.manager.request(method, endpoint, body=content, fields=params, headers=headers)

        return

    def to_return(self, event: dict) -> bool:
        if event.get("sender").startswith("@_discord"):
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

        body       = content.get("body")
        event_id   = event.get("event_id")
        homeserver = event.get("sender").split(":")[-1]
        is_direct  = content.get("is_direct")
        room_id    = event.get("room_id")
        sender     = event.get("sender")
        channel_id = self.db.get_channel(room_id)

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
            logging.info(f"Joining direct message room {event.room_id}")
            await self.join_room(event.room_id)

    async def handle_bridge(self, message: Event) -> None:
        try:
            channel = int(message.body.split()[1])
        except ValueError:
            return

        # See if the given channel is valid.
        check = self.discord_client.get_channel(channel)
        if not check or len(str(channel)) != 18:
            return

        logging.info(f"Creating bridged room for channel {channel}")

        await self.create_room(channel, message.sender)

    async def handle_message(self, event: dict) -> None:
        message = self.get_event_object(event)
        user    = await self.get_user_object(message.sender)

        # Ignore empty messages.
        if self.to_return(event) or not message.body:
            return

        if message.body.startswith("!bridge"):
            await self.handle_bridge(message)

        if message.channel_id not in self.db.list_channels() \
                or not message.channel_id:
            return

        await self.send_webhook(message, user)

    '''
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
    '''

    async def register(self, mxid: str) -> str:
        content = {"type": "m.login.application_service",
                   "username": mxid[1:-(len(self.app.plain_url) + 1)]}

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
        resp = await self.send("GET", f"/profile/{mxid}")

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

    '''
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
    '''

    async def get_room_id(self, alias: str) -> str:
        resp = await self.send("GET", f"/directory/room/{alias.replace('#', '%23')}")

        return resp.get("room_id")

    async def join_room(self, room_id: str, mxid: str = "") -> str:
        params = {"user_id": mxid} if mxid else {}

        resp = await self.send("POST", f"/join/{room_id}", params=params)

        return resp.get("room_id")

    async def send_invite(self, room_id: str, mxid: str) -> None:
        logging.info(f"Inviting user {mxid} to room {room_id}")

        await self.send("POST", f"/rooms/{room_id}/invite", {"user_id": mxid})

    async def send_message(self, room_id: str, content: str, mxid: str) -> str:
        content = self.create_message_event(content)

        resp = await self.send(
            "PUT", f"/rooms/{room_id}/send/m.room.message/{uuid.uuid4()}",
            content, params={"user_id": mxid}
        )

        return resp.get("event_id")

    def create_message_event(self, message: str) -> dict:
        content = {"body": message, "msgtype": "m.text"}

        return content


class DiscordClient(object):
    def __init__(self, appservice) -> None:
        self.app   = appservice
        self.token = config["discord_token"]

    class InteractionResponseType(object):
        PONG                        = 0
        ACKNOWLEDGE                 = 1
        CHANNEL_MESSAGE             = 2
        CHANNEL_MESSAGE_WITH_SOURCE = 4
        ACKNOWLEDGE_WITH_SOURCE     = 5

    class GatewayIntents(object):
        def bitmask(bit: int) -> int:
            return 1 << bit

        GUILDS                   = bitmask(0)
        GUILD_MEMBERS            = bitmask(1)
        GUILD_BANS               = bitmask(2)
        GUILD_EMOJIS             = bitmask(3)
        GUILD_INTEGRATIONS       = bitmask(4)
        GUILD_WEBHOOKS           = bitmask(5)
        GUILD_INVITES            = bitmask(6)
        GUILD_VOICE_STATES       = bitmask(7)
        GUILD_PRESENCES          = bitmask(8)
        GUILD_MESSAGES           = bitmask(9)
        GUILD_MESSAGE_REACTIONS  = bitmask(10)
        GUILD_MESSAGE_TYPING     = bitmask(11)
        DIRECT_MESSAGES          = bitmask(12)
        DIRECT_MESSAGE_REACTIONS = bitmask(13)
        DIRECT_MESSAGE_TYPING    = bitmask(14)

    class GatewayOpCodes(object):
        DISPATCH              = 0
        HEARTBEAT             = 1
        IDENTIFY              = 2
        PRESENCE_UPDATE       = 3
        VOICE_STATE_UPDATE    = 4
        RESUME                = 6
        RECONNECT             = 7
        REQUEST_GUILD_MEMBERS = 8
        INVALID_SESSION       = 9
        HELLO                 = 10
        HEARTBEAT_ACK         = 11

    class Payloads(GatewayIntents, GatewayOpCodes):
        def __init__(self):
            # TODO: Use updated seqnum
            self.HEARTBEAT = {"op": self.HEARTBEAT, "d": 0}

            self.IDENTIFY = {
                "op": self.IDENTIFY,
                "d": {"token": config["discord_token"], "intents":
                      self.GUILDS |
                      self.GUILD_MESSAGES,
                      "properties": {"$os": "discord", "$browser": "discord",
                      "$device": "discord"}}
                }

    async def start(self):
        await self.gateway_handler(self.get_gateway_url())

    async def heartbeat_handler(self, websocket, interval_ms: int) -> None:
        while True:
            await asyncio.sleep(interval_ms / 1000)
            await websocket.send(json.dumps(self.Payloads().HEARTBEAT))

    async def gateway_handler(self, gateway_url: str) -> None:
        gateway_url += "/?v=8&encoding=json"
        async with websockets.connect(gateway_url) as websocket:
            async for message in websocket:
                data      = json.loads(message)
                data_dict = data.get("d")

                opcode = data.get("op")

                if opcode == self.GatewayOpCodes.DISPATCH:
                    otype = data.get("t")
                    if otype == "READY":
                        logging.info("READY")

                    elif otype == "MESSAGE_CREATE":
                        self.handle_message(data_dict)

                    elif otype == "MESSAGE_DELETE":
                        self.handle_deletion(data_dict)

                    elif otype == "MESSAGE_UPDATE":
                        self.handle_edit(data_dict)

                    else:
                        logging.info(f"Unknown {otype}")

                elif opcode == self.GatewayOpCodes.HELLO:
                    heartbeat_interval = data_dict.get("heartbeat_interval")
                    logging.info(f"Heartbeat Interval: {heartbeat_interval}")

                    # Send periodic hearbeats to gateway.
                    asyncio.ensure_future(self.heartbeat_handler(
                        websocket, heartbeat_interval
                    ))

                    await websocket.send(json.dumps(self.Payloads().IDENTIFY))

                elif opcode == self.GatewayOpCodes.HEARTBEAT_ACK:
                    # NOP
                    pass

                else:
                    logging.info(f"Unknown event:\n{json.dumps(data, indent=4)}")

    @dataclass
    class Member(object):
        avatar_url: str
        discriminator: str
        id: str
        username: str

    class Message(object):
        def __init__(self, attachments: list, author, content: str,
                     channel_id: str, edited: bool, message_id: str) -> None:
            self.attachments = attachments
            self.author      = author
            self.content     = content
            self.channel_id  = channel_id
            self.edited      = edited
            self.id          = message_id

    def get_member_object(self, author: dict) -> Member:
        author_id     = author.get("id")
        avatar        = author.get("avatar")

        if not avatar:
            avatar_url = None
        else:
            avatar_ext = "gif" if avatar.startswith("a_") else "png"
            avatar_url = "https://cdn.discordapp.com/avatars/" \
                         f"{author_id}/{avatar}.{avatar_ext}"

        discriminator = author.get("discriminator")
        username      = author.get("username")

        return self.Member(avatar_url, discriminator, author_id, username)

    def get_message_object(self, message: dict) -> Message:
        author = self.get_member_object(message.get("author"))

        attachments = message.get("attachments")
        content     = message.get("content")
        channel_id  = message.get("channel_id")
        message_id  = message.get("id")
        edited      = True if message.get("edited_timestamp") else False

        return self.Message(attachments, author, content, channel_id, edited, message_id)

    def to_return(self, message: Message) -> bool:
        if message.author.discriminator == "0000":
            return True

        return False

    def handle_message(self, message: dict) -> None:
        message = self.get_message_object(message)

        if self.to_return(message):
            return

    def handle_deletion(self, message: dict) -> None:
        return
        # self.app.redact(message.get("id")) # message.get("channel_id")

    def handle_edit(self, message: dict) -> None:
        message = self.get_message_object(message)

        if self.to_return(message):
            return

    def send(self, method: str, path: str, content: dict = {}) -> dict:
        endpoint = "https://discord.com/api/v8"
        headers  = {"Authorization": f"Bot {self.token}", "Content-Type": "application/json"}

        # 'body' being an empty dict breaks "GET" requests.
        content = json.dumps(content) if content else None

        resp = self.app.manager.request(method, f"{endpoint}{path}", body=content, headers=headers)

        return json.loads(resp.data)

    def get_gateway_url(self) -> str:
        resp = self.send("GET", "/gateway")

        return resp.get("url")

    def get_webhooks(self, channel_id: str) -> None:
        webhooks = self.send("GET", f"/channels/{channel_id}/webhooks")
        return [ {webhook["name"]: webhook["token"]} for webhook in webhooks ]

    def send_message(self, message: str, channel_id: str) -> None:
        self.send("POST", f"/channels/{channel_id}/messages", {"content": message})

def main() -> None:
    logging.basicConfig(level=logging.INFO)

    app = AppService()

    # Start the bottle app in a separate thread.
    app_thread = threading.Thread(target=app.start, daemon=True)
    app_thread.start()

    try:
        asyncio.run(app.discord.start())
    except KeyboardInterrupt:
        sys.exit()

if __name__ == "__main__":
    main()

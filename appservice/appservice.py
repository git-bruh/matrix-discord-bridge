import asyncio
import json
import logging
import os
import threading
import sys
import uuid
import urllib3
import urllib.parse
import bottle
import db
import discord
import websockets
import matrix
from typing import Union
from db import DataBase


def config_gen(config_file: str) -> dict:
    config_dict = {
        "as_token": "my-secret-as-token",
        "hs_token": "my-secret-hs-token",
        "user_id": "appservice-discord",
        "homeserver": "http://127.0.0.1:8008",
        "server_name": "localhost",
        "discord_cmd_prefix": "/",
        "discord_token": "my-secret-discord-token",
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

class AppService(bottle.Bottle):
    def __init__(self) -> None:
        super(AppService, self).__init__()

        self.as_token    = config["as_token"]
        self.hs_token    = config["hs_token"]
        self.base_url    = config["homeserver"]
        self.server_name = config["server_name"]
        self.user_id     = f"@{config['user_id']}:{self.server_name}"
        self.db          = DataBase(config["database"])
        self.discord     = DiscordClient(self)
        self.logger      = logging.getLogger("appservice")
        self.manager     = urllib3.PoolManager()

        # Add route for bottle.
        self.route("/transactions/<transaction>",
                   callback=self.receive_event, method="PUT")

    def start(self):
        self.run(host="127.0.0.1", port=5000)

    def receive_event(self, transaction: str) -> dict:
        """
        Check whether the homeserver passed the correct token and handle events.
        """

        hs_token = bottle.request.query.getone("access_token")

        if not hs_token:
            bottle.response.status = 401
            return {"errcode": "DISCORD.APPSERVICE_UNAUTHORIZED"}

        if hs_token != self.hs_token:
            bottle.response.status = 403
            return {"errcode": "DISCORD.APPSERVICE_FORBIDDEN"}

        events = bottle.request.json.get("events")

        for event in events:
            event_type = event.get("type")

            try:
                if event_type == "m.room.member":
                    self.handle_member(event)
                elif event_type == "m.room.message":
                    self.handle_message(event)
            except Exception:
                # Don't change the response code as the homeserver
                # will keep trying to send us the same event and back off.
                self.logger.exception("")

        return {}

    def send(self, method: str, path: str = "",
             content: Union[bytes, dict] = {}, params: dict = {},
             content_type: str = "application/json",
             endpoint: str = "/_matrix/client/r0") -> dict:
        params["access_token"] = self.as_token
        headers  = {"Content-Type": content_type}
        content  = json.dumps(content) if type(content) == dict else content
        endpoint = f"{self.base_url}{endpoint}{path}?{urllib.parse.urlencode(params)}"

        resp = self.manager.request(method, endpoint, body=content, headers=headers)

        # if resp.status == 429:
        # handle rate limit ?
        # if resp.status < 200 or response.status >= 300:
        # raise exception

        return json.loads(resp.data)

    def get_event_object(self, event: dict) -> matrix.Event:
        content = event.get("content")

        body       = content.get("body")
        event_id   = event.get("event_id")
        homeserver = event.get("sender").split(":")[-1]
        is_direct  = content.get("is_direct")
        room_id    = event.get("room_id")
        sender     = event.get("sender")
        state_key  = event.get("state_key")
        channel_id = self.db.get_channel(room_id)

        return matrix.Event(
            body, channel_id, event_id, is_direct, homeserver, room_id, sender, state_key
        )

    def get_user_object(self, mxid: str) -> matrix.User:
        avatar_url, display_name = self.get_profile(mxid)

        return matrix.User(avatar_url, display_name)

    def to_return(self, event: dict) -> bool:
        if event.get("sender").startswith(("@_discord", self.user_id)):
            return True

        return False

    def handle_member(self, event: dict) -> None:
        event = self.get_event_object(event)

        # Ignore events that aren't for us.
        if event.sender.split(":")[-1] != self.server_name or \
                event.state_key != self.user_id or not event.is_direct:
            return

        # Join the direct message room.
        self.logger.info(f"Joining direct message room {event.room_id}")
        self.join_room(event.room_id)

    def handle_bridge(self, message: matrix.Event) -> None:
        # Ignore events that aren't for us.
        if message.sender.split(":")[-1] != self.server_name or \
                not message.body.startswith("!bridge"):
            return

        try:
            channel = (message.body.split()[1])
        except IndexError:
            return

        # Check if the given channel is valid.
        channel = self.discord.get_channel(channel)
        if not channel or channel.type != discord.ChannelType.GUILD_TEXT:
            return

        self.logger.info(f"Creating bridged room for channel {channel.id}")

        self.create_room(channel, message.sender)

    def handle_message(self, event: dict) -> None:
        message = self.get_event_object(event)
        user    = self.get_user_object(message.sender)

        if self.to_return(event) or not message.body:
            return

        # Handle bridging commands.
        self.handle_bridge(message)

        if message.channel_id:
            self.discord.send_webhook(message, user)

    def register(self, mxid: str) -> None:
        """
        Register a dummy user on the Matrix homeserver.
        """

        content = {"type": "m.login.application_service",
                   "username": mxid[1:-(len(self.app.plain_url) + 1)]}

        resp = self.send("POST", "/register", content)

        self.db.add_user(resp["user_id"])

    def create_room(self, channel: discord.Channel, sender: str) -> None:
        """
        Create a bridged room and invite the person who invoked the command.
        """

        content = {
            "room_alias_name": f"discord_{channel.id}", "name": channel.name,
            "topic": channel.topic, "is_direct": False, "visibility": "private",
            "invite": [sender], "creation_content": {"m.federate": True},
            "initial_state": [
                {"type": "m.room.join_rules",
                 "content": {"join_rule": "invite"}},
                {"type": "m.room.history_visibility",
                 "content": {"history_visibility": "shared"}}
            ], "power_level_content_override": {"users": {sender: 100}}
        }

        resp = self.send("POST", "/createRoom", content)

        self.db.add_room(resp["room_id"], channel.id)

    def get_profile(self, mxid: str) -> tuple:
        resp = self.send("GET", f"/profile/{mxid}")

        avatar_url = resp.get("avatar_url")
        avatar_url = avatar_url[6:].split("/")
        try:
            avatar_url = f"{self.base_url}/_matrix/media/r0/download/" \
                         f"{avatar_url[0]}/{avatar_url[1]}"
        except IndexError:
            avatar_url = None

        display_name = resp.get("displayname")

        return avatar_url, display_name

    def get_members(self, room_id: str) -> list:
        resp = self.send(
            "GET", f"/rooms/{room_id}/members",
            params={"membership": "join", "not_membership": "leave"}
        )

        return [
            content["sender"] for content in resp["chunk"]
            if content["content"]["membership"] == "join"
        ]

    def set_nick(self, username: str, mxid: str) -> None:
        resp = self.send(
            "PUT", f"/profile/{mxid}/displayname",
            {"displayname": nickname}, params={"user_id": mxid}
        )

        self.db.add_username(username, mxid)

    def set_avatar(self, avatar_url: str, mxid: str) -> None:
        avatar_uri = self.upload(avatar_url)

        self.send(
            "PUT", f"/profile/{mxid}/avatar_url", {"avatar_url": avatar_uri},
            params={"user_id": mxid}
        )

        self.db.add_avatar(avatar_url, mxid)

    def upload(self, url: str) -> str:
        """
        Upload a file to the Matrix homeserver.
        """

        resp = self.manager.request("GET", url)

        content_type, file = resp.headers.get("Content-Type"), resp.data

        resp = self.send(
            "POST", content=file, content_type=content_type,
            params={"filename": f"{uuid.uuid4()}"},
            endpoint="/_matrix/media/r0/upload"
        )

        return resp.get("content_uri")

    def get_room_id(self, alias: str) -> str:
        resp = self.send("GET", f"/directory/room/{urllib.parse.quote(alias)}")

        return resp.get("room_id")

    def join_room(self, room_id: str, mxid: str = "") -> str:
        params = {"user_id": mxid} if mxid else {}

        resp = self.send("POST", f"/join/{room_id}", params=params)

    def send_invite(self, room_id: str, mxid: str) -> None:
        self.logger.info(f"Inviting user {mxid} to room {room_id}")

        self.send("POST", f"/rooms/{room_id}/invite", {"user_id": mxid})

    def send_message(self, room_id: str, content: str, mxid: str) -> str:
        content = self.create_message_event(content)

        resp = self.send(
            "PUT", f"/rooms/{room_id}/send/m.room.message/{uuid.uuid4()}",
            content, params={"user_id": mxid}
        )

        return resp.get("event_id")

    def create_message_event(self, message: str) -> dict:
        content = {"body": message, "msgtype": "m.text"}

        return content


class DiscordClient(object):
    def __init__(self, appservice: AppService) -> None:
        self.app      = appservice
        self.logger   = logging.getLogger("discord")
        self.token    = config["discord_token"]
        self.Payloads = discord.Payloads(self.token)

    async def start(self) -> None:
        await self.gateway_handler(self.get_gateway_url())

    async def heartbeat_handler(self, websocket, interval_ms: int) -> None:
        while True:
            await asyncio.sleep(interval_ms / 1000)
            await websocket.send(json.dumps(self.Payloads.HEARTBEAT))

    async def gateway_handler(self, gateway_url: str) -> None:
        async with websockets.connect(f"{gateway_url}/?v=8&encoding=json") as websocket:
            async for message in websocket:
                data      = json.loads(message)
                data_dict = data.get("d")

                opcode = data.get("op")

                if opcode == discord.GatewayOpCodes.DISPATCH:
                    otype = data.get("t")
                    if otype == "READY":
                        self.logger.info("READY")

                    elif otype == "MESSAGE_CREATE":
                        self.handle_message(data_dict)

                    elif otype == "MESSAGE_DELETE":
                        self.handle_deletion(data_dict)

                    elif otype == "MESSAGE_UPDATE":
                        self.handle_edit(data_dict)

                    else:
                        self.logger.info(f"Unknown opcode: {otype}")

                elif opcode == discord.GatewayOpCodes.HELLO:
                    heartbeat_interval = data_dict.get("heartbeat_interval")
                    self.logger.info(f"Heartbeat Interval: {heartbeat_interval}")

                    # Send periodic hearbeats to gateway.
                    asyncio.ensure_future(self.heartbeat_handler(
                        websocket, heartbeat_interval
                    ))

                    await websocket.send(json.dumps(self.Payloads.IDENTIFY))

                elif opcode == discord.GatewayOpCodes.HEARTBEAT_ACK:
                    # NOP
                    pass

                else:
                    self.logger.info(f"Unknown event:\n{json.dumps(data, indent=4)}")

    def get_channel_object(self, channel: dict) -> discord.Channel:
        channel_id   = channel.get("id")
        channel_type = channel.get("type")

        name  = channel.get("name")
        topic = channel.get("topic")

        return discord.Channel(channel_id, name, topic, channel_type)

    def get_member_object(self, author: dict) -> discord.User:
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

        return discord.User(avatar_url, discriminator, author_id, username)

    def get_message_reference_object(self, reference: dict) -> discord.MessageReference:
        message_id = reference.get("message_id")

        return discord.MessageReference(message_id)

    def get_message_object(self, message: dict) -> discord.Message:
        embeds      = message.get("embeds")
        author      = self.get_member_object(message.get("author")) if not embeds else None
        attachments = message.get("attachments")
        content     = message.get("content")
        channel_id  = message.get("channel_id")
        edited      = True if message.get("edited_timestamp") else False
        message_id  = message.get("id")
        reference   = message.get("message_reference")
        reference   = self.get_message_reference_object(reference) if reference else None

        return discord.Message(
            attachments, author, content, channel_id, edited, embeds, message_id, reference
        )

    def to_return(self, message: discord.Message) -> bool:
        if message.embeds or message.channel_id not in self.app.db.list_channels() or \
                message.author.discriminator == "0000":
            return True

        return False

    def wrap(self, message: discord.Message) -> tuple:
        """
        Get the corresponding room ID and the puppet's mxid for
        a given channel ID and a Discord user.
        """

        mxid = f"@_discord_{message.author.id}:{self.app.plain_url}"
        room_alias = f"#discord_{message.channel_id}:{self.app.plain_url}"

        room_id = self.app.get_room_id(room_alias)  # TODO Cache

        if not self.app.db.query_user(mxid):
            self.logger.info(f"Creating dummy user for Discord user {message.author.id}")
            self.app.register(mxid)

            self.app.set_nick(
                f"{message.author.username}#{message.author.discriminator}", mxid
            )

            self.app.set_avatar(message.author.avatar_url, mxid)

        if mxid not in self.app.get_members(room_id):  # TODO cache
            self.app.send_invite(room_id, mxid)
            self.app.join_room(room_id, mxid)

        return mxid, room_id

    def handle_message(self, message: dict) -> None:
        message = self.get_message_object(message)

        if self.to_return(message):
            return

        mxid, room_id = self.wrap(message)

        self.app.send_message(room_id, message.content, mxid)

    def handle_deletion(self, message: dict) -> None:
        return
        # self.app.redact(message.get("id")) # message.get("channel_id")

    def handle_edit(self, message: dict) -> None:
        message = self.get_message_object(message)

        if self.to_return(message):
            return

    def send(self, method: str, path: str, content: dict = {}, params: dict = {}) -> dict:
        endpoint = f"https://discord.com/api/v8{path}?{urllib.parse.urlencode(params)}"
        headers  = {"Authorization": f"Bot {self.token}", "Content-Type": "application/json"}

        # 'body' being an empty dict breaks "GET" requests.
        content = json.dumps(content) if content else None

        resp = self.app.manager.request(method, f"{endpoint}{path}", body=content, headers=headers)

        return json.loads(resp.data)

    def get_gateway_url(self) -> str:
        resp = self.send("GET", "/gateway")

        return resp.get("url")

    def get_channel(self, channel_id: str) -> discord.Channel:
        """
        Get the corresponding `discord.Channel` object for a given channel ID.
        """

        resp = self.send("GET", f"/channels/{channel_id}")

        return self.get_channel_object(resp)

    def get_webhooks(self, channel_id: str) -> None:
        webhooks = self.send("GET", f"/channels/{channel_id}/webhooks")
        return [{webhook["name"]: webhook["token"]} for webhook in webhooks]

    def send_webhook(self, message: str, user: matrix.User) -> str:
        content = {"content": content, "username": user.display_name,
                   # Disable 'everyone' and 'role' mentions.
                   "allowed_mentions": {"parse": ["users"]}}

        return

        # self.send("POST", f"/webhooks/{webhook_id}/{webhook_token}?wait=True", content)
        # return resp.get("id")

    def edit_webhook(self, message: str) -> None:
        content = {"content": message}
        # self.send("PATCH", f"/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}", content)

    def delete_webhook(self, message: str) -> None:
        # self.send("DELETE", f"/webhooks/{webhook_id}/{webhook_token}/messages/{message_id})
        return

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

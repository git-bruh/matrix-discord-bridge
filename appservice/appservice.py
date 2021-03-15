import asyncio
import json
import logging
import os
import sys
import threading
import urllib.parse
import uuid
from typing import Optional, Union

import bottle
import urllib3
import websockets

import discord
import matrix
from db import DataBase


def config_gen(config_file: str) -> dict:
    try:
        basedir = sys.argv[1]
        if not os.path.exists(basedir):
            print("Path does not exist!")
            sys.exit(1)
    except IndexError:
        basedir = os.getcwd()

    config_file = f"{basedir}/{config_file}"

    config_dict = {
        "as_token": "my-secret-as-token",
        "hs_token": "my-secret-hs-token",
        "user_id": "appservice-discord",
        "homeserver": "http://127.0.0.1:8008",
        "server_name": "localhost",
        "discord_cmd_prefix": "/",
        "discord_token": "my-secret-discord-token",
        "database": f"{basedir}/bridge.db",
    }

    if not os.path.exists(config_file):
        with open(config_file, "w") as f:
            json.dump(config_dict, f, indent=4)
            print(f"Configuration dumped to '{config_file}'")
            sys.exit()

    with open(config_file, "r") as f:
        return json.loads(f.read())


config = config_gen("appservice.json")

message_cache = {}  # Used for edits and replies.
http = urllib3.PoolManager()  # Used for sending requests.


class AppService(bottle.Bottle):
    def __init__(self) -> None:
        super(AppService, self).__init__()

        self.as_token = config["as_token"]
        self.hs_token = config["hs_token"]
        self.base_url = config["homeserver"]
        self.server_name = config["server_name"]
        self.user_id = f"@{config['user_id']}:{self.server_name}"
        self.db = DataBase(config["database"])
        self.discord = DiscordClient(self)
        self.logger = logging.getLogger("appservice")

        # Add route for bottle.
        self.route(
            "/transactions/<transaction>",
            callback=self.receive_event,
            method="PUT",
        )

    def start(self):
        self.run(host="127.0.0.1", port=5000)

    def receive_event(self, transaction: str) -> dict:
        """
        Verify the homeserver's token and handle events.
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

            if event_type == "m.room.member":
                self.handle_member(event)
            elif event_type == "m.room.message":
                self.handle_message(event)
            elif event_type == "m.room.redaction":
                self.handle_redaction(event)

        return {}

    def send(
        self,
        method: str,
        path: str = "",
        content: Union[bytes, dict] = {},
        params: dict = {},
        content_type: str = "application/json",
        endpoint: str = "/_matrix/client/r0",
    ) -> dict:
        params["access_token"] = self.as_token
        headers = {"Content-Type": content_type}
        content = json.dumps(content) if type(content) == dict else content
        endpoint = (
            f"{self.base_url}{endpoint}{path}?"
            f"{urllib.parse.urlencode(params)}"
        )

        resp = http.request(method, endpoint, body=content, headers=headers)

        # TODO handle failure

        return json.loads(resp.data)

    def get_event_object(self, event: dict) -> matrix.Event:
        content = event.get("content")

        # Message edits.
        if content.get("m.relates_to", {}).get("rel_type") == "m.replace":
            relates_to = content.get("m.relates_to").get("event_id")
            new_body = content.get("m.new_content").get("body")
        else:
            relates_to = new_body = None

        room_id = event.get("room_id")

        return matrix.Event(
            author=self.get_user_object(event.get("sender")),
            body=content.get("body"),
            channel_id=self.db.get_channel(room_id),
            event_id=event.get("event_id"),
            is_direct=content.get("is_direct"),
            relates_to=relates_to,
            room_id=room_id,
            new_body=new_body,
            sender=event.get("sender"),
            state_key=event.get("state_key"),
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
        if (
            event.sender.split(":")[-1] != self.server_name
            or event.state_key != self.user_id
            or not event.is_direct
        ):
            return

        # Join the direct message room.
        self.logger.info(f"Joining direct message room {event.room_id}")
        self.join_room(event.room_id)

    def handle_bridge(self, message: matrix.Event) -> None:
        # Ignore events that aren't for us.
        if message.sender.split(":")[
            -1
        ] != self.server_name or not message.body.startswith("!bridge"):
            return

        try:
            channel = message.body.split()[1]
        except IndexError:
            return

        # Check if the given channel is valid.
        channel = self.discord.get_channel(channel)
        if channel.type != discord.ChannelType.GUILD_TEXT:
            return

        self.logger.info(f"Creating bridged room for channel {channel.id}")

        self.create_room(channel, message.sender)

    def handle_message(self, event: dict) -> None:
        message = self.get_event_object(event)

        if self.to_return(event) or not message.body:
            return

        # Handle bridging commands.
        self.handle_bridge(message)

        if not message.channel_id:
            return

        webhook = self.discord.get_webhook(message.channel_id, "matrix_bridge")

        message_cache[message.event_id] = {
            "message_id": self.discord.send_webhook(message, webhook),
            "webhook": webhook,
        }

    def handle_redaction(self, event: dict) -> None:
        redacts = event.get("redacts")

        event = message_cache.get(redacts)

        if event:
            self.discord.delete_webhook(event["message_id"], event["webhook"])
            message_cache.pop(redacts)

    def register(self, mxid: str) -> None:
        """
        Register a dummy user on the homeserver.
        """

        content = {
            "type": "m.login.application_service",
            # "@test:localhost" -> "test" (Can't register with a full mxid.)
            "username": mxid[1:].split(":")[0],
        }

        resp = self.send("POST", "/register", content)

        self.db.add_user(resp["user_id"])

    def create_room(self, channel: discord.Channel, sender: str) -> None:
        """
        Create a bridged room and invite the person who invoked the command.
        """

        content = {
            "room_alias_name": f"discord_{channel.id}",
            "name": channel.name,
            "topic": channel.topic,
            "visibility": "private",
            "invite": [sender],
            "creation_content": {"m.federate": True},
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "content": {"join_rule": "invite"},
                },
                {
                    "type": "m.room.history_visibility",
                    "content": {"history_visibility": "shared"},
                },
            ],
            "power_level_content_override": {"users": {sender: 100}},
        }

        resp = self.send("POST", "/createRoom", content)

        self.db.add_room(resp["room_id"], channel.id)

    def get_profile(self, mxid: str) -> tuple:
        resp = self.send("GET", f"/profile/{mxid}")

        avatar_url = resp.get("avatar_url")
        avatar_url = avatar_url[6:].split("/")
        try:
            avatar_url = (
                f"{self.base_url}/_matrix/media/r0/download/"
                f"{avatar_url[0]}/{avatar_url[1]}"
            )
        except IndexError:
            avatar_url = None

        display_name = resp.get("displayname")

        return avatar_url, display_name

    def get_members(self, room_id: str) -> list:
        resp = self.send(
            "GET",
            f"/rooms/{room_id}/members",
            params={"membership": "join", "not_membership": "leave"},
        )

        # TODO cache ?

        return [
            content["sender"]
            for content in resp["chunk"]
            if content["content"]["membership"] == "join"
        ]

    def set_nick(self, username: str, mxid: str) -> None:
        self.send(
            "PUT",
            f"/profile/{mxid}/displayname",
            {"displayname": username},
            params={"user_id": mxid},
        )

        self.db.add_username(username, mxid)

    def set_avatar(self, avatar_url: str, mxid: str) -> None:
        avatar_uri = self.upload(avatar_url)

        self.send(
            "PUT",
            f"/profile/{mxid}/avatar_url",
            {"avatar_url": avatar_uri},
            params={"user_id": mxid},
        )

        self.db.add_avatar(avatar_url, mxid)

    def upload(self, url: str) -> str:
        """
        Upload a file to the homeserver.
        """

        resp = http.request("GET", url)

        content_type, file = resp.headers.get("Content-Type"), resp.data

        resp = self.send(
            "POST",
            content=file,
            content_type=content_type,
            params={"filename": f"{uuid.uuid4()}"},
            endpoint="/_matrix/media/r0/upload",
        )

        return resp.get("content_uri")

    def get_room_id(self, alias: str) -> str:
        resp = self.send("GET", f"/directory/room/{urllib.parse.quote(alias)}")

        # TODO cache

        return resp.get("room_id")

    def join_room(self, room_id: str, mxid: str = "") -> str:
        self.send(
            "POST",
            f"/join/{room_id}",
            params={"user_id": mxid} if mxid else {},
        )

    def send_invite(self, room_id: str, mxid: str) -> None:
        self.logger.info(f"Inviting user {mxid} to room {room_id}")

        self.send("POST", f"/rooms/{room_id}/invite", {"user_id": mxid})

    def send_typing(self, room_id: str, mxid: str = "") -> None:
        self.send(
            "PUT",
            f"/rooms/{room_id}/typing/{mxid}",
            {"typing": True, "timeout": 8000},
            {"user_id": mxid} if mxid else {},
        )

    def redact(self, event_id: str, room_id: str, mxid: str = "") -> None:
        self.send(
            "PUT",
            f"/rooms/{room_id}/redact/{event_id}/{uuid.uuid4()}",
            params={"user_id": mxid} if mxid else {},
        )

    def send_message(self, room_id: str, content: str, mxid: str = "") -> str:
        resp = self.send(
            "PUT",
            f"/rooms/{room_id}/send/m.room.message/{uuid.uuid4()}",
            self.create_message_event(content),
            {"user_id": mxid} if mxid else {},
        )

        return resp.get("event_id")

    def create_message_event(self, message: str) -> dict:
        content = {"body": message, "msgtype": "m.text"}

        return content


class DiscordClient(object):
    def __init__(self, appservice: AppService) -> None:
        self.app = appservice
        self.logger = logging.getLogger("discord")
        self.token = config["discord_token"]
        self.Payloads = discord.Payloads(self.token)
        self.webhook_cache = {}

    async def start(self) -> None:
        await self.gateway_handler(self.get_gateway_url())

    async def heartbeat_handler(self, websocket, interval_ms: int) -> None:
        while True:
            await asyncio.sleep(interval_ms / 1000)
            await websocket.send(json.dumps(self.Payloads.HEARTBEAT))

    async def gateway_handler(self, gateway_url: str) -> None:
        gateway_url += "/?v=8&encoding=json"

        async with websockets.connect(gateway_url) as websocket:
            async for message in websocket:
                data = json.loads(message)
                data_dict = data.get("d")

                opcode = data.get("op")

                if opcode == discord.GatewayOpCodes.DISPATCH:
                    otype = data.get("t")

                    if otype == "READY":
                        self.logger.info("READY")

                    elif otype == "MESSAGE_CREATE":
                        self.handle_message(data_dict)

                    elif otype == "MESSAGE_DELETE":
                        self.handle_redaction(data_dict)

                    elif otype == "MESSAGE_UPDATE":
                        self.handle_edit(data_dict)

                    elif otype == "TYPING_START":
                        self.handle_typing(data_dict)

                elif opcode == discord.GatewayOpCodes.HELLO:
                    heartbeat_interval = data_dict.get("heartbeat_interval")
                    self.logger.info(
                        f"Heartbeat Interval: {heartbeat_interval}"
                    )

                    # Send periodic hearbeats to gateway.
                    asyncio.ensure_future(
                        self.heartbeat_handler(websocket, heartbeat_interval)
                    )

                    await websocket.send(json.dumps(self.Payloads.IDENTIFY))

                elif opcode == discord.GatewayOpCodes.HEARTBEAT_ACK:
                    # NOP
                    pass

    def get_gateway_url(self) -> str:
        resp = self.send("GET", "/gateway")

        return resp.get("url")

    def get_channel_object(self, channel: dict) -> discord.Channel:
        return discord.Channel(
            id=channel.get("id"),
            name=channel.get("name"),
            topic=channel.get("topic"),
            type=channel.get("type"),
        )

    def get_member_object(self, author: dict) -> discord.User:
        author_id = author.get("id")
        avatar = author.get("avatar")

        if not avatar:
            avatar_url = None
        else:
            avatar_ext = "gif" if avatar.startswith("a_") else "png"
            avatar_url = (
                "https://cdn.discordapp.com/avatars/"
                f"{author_id}/{avatar}.{avatar_ext}"
            )

        return discord.User(
            avatar_url=avatar_url,
            discriminator=author.get("discriminator"),
            id=author_id,
            username=author.get("username"),
        )

    def get_message_object(self, message: dict) -> discord.Message:
        return discord.Message(
            attachments=message.get("attachments"),
            author=self.get_member_object(message.get("author", {})),
            content=message.get("content"),
            channel_id=message.get("channel_id"),
            edited=True if message.get("edited_timestamp") else False,
            embeds=message.get("embeds"),
            id=message.get("id"),
            reference=message.get("message_reference", {}).get("message_id"),
            webhook_id=message.get("webhook_id"),
        )

    def matrixify(self, user: str = "", channel: str = "") -> str:
        if user:
            return f"@_discord_{user}:{self.app.server_name}"
        elif channel:
            return f"#discord_{channel}:{self.app.server_name}"

    def to_return(self, message: discord.Message) -> bool:
        if (
            message.channel_id not in self.app.db.list_channels()
            or message.embeds
            or message.author.discriminator == "0000"
        ):
            return True

        return False

    def wrap(self, message: discord.Message) -> tuple:
        """
        Get the corresponding room ID and the puppet's mxid for
        a given channel ID and a Discord user.
        """

        mxid = self.matrixify(user=message.author.id)
        room_id = self.app.get_room_id(
            self.matrixify(channel=message.channel_id)
        )

        if not self.app.db.query_user(mxid):
            self.logger.info(
                f"Creating dummy user for Discord user {message.author.id}"
            )
            self.app.register(mxid)

            self.app.set_nick(
                f"{message.author.username}#"
                f"{message.author.discriminator}",
                mxid,
            )

            if message.author.avatar_url:
                self.app.set_avatar(message.author.avatar_url, mxid)

        if mxid not in self.app.get_members(room_id):
            self.app.send_invite(room_id, mxid)
            self.app.join_room(room_id, mxid)

        return mxid, room_id

    def handle_message(self, message: dict) -> None:
        message = self.get_message_object(message)

        if self.to_return(message):
            return

        mxid, room_id = self.wrap(message)

        message_cache[message.id] = {
            "event_id": self.app.send_message(room_id, message.content, mxid),
            "mxid": mxid,
            "room_id": room_id,
        }

    def handle_redaction(self, message: dict) -> None:
        message_id = message["id"]

        event = message_cache.get(message_id)

        if event:
            self.app.redact(event["event_id"], event["room_id"], event["mxid"])
            message_cache.pop(message_id)

    def handle_edit(self, message: dict) -> None:
        message = self.get_message_object(message)

        if self.to_return(message):
            return

    def handle_typing(self, typing: dict) -> None:
        typing = discord.Typing(
            sender=typing.get("user_id"), channel_id=typing.get("channel_id")
        )

        if typing.channel_id not in self.app.db.list_channels():
            return

        mxid = self.matrixify(user=typing.sender)
        room_id = self.app.get_room_id(
            self.matrixify(channel=typing.channel_id)
        )

        if mxid not in self.app.get_members(room_id):
            return

        self.app.send_typing(room_id, mxid)

    def send(
        self, method: str, path: str, content: dict = {}, params: dict = {}
    ) -> Optional[dict]:
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

        resp = http.request(method, endpoint, body=content, headers=headers)

        # NO CONTENT.
        if resp.status == 204:
            return

        # TODO handle failure

        return json.loads(resp.data)

    def get_channel(self, channel_id: str) -> discord.Channel:
        """
        Get the corresponding `discord.Channel` object for a given channel ID.
        """

        resp = self.send("GET", f"/channels/{channel_id}")

        return self.get_channel_object(resp)

    def create_webhook(self, channel_id: str, name: str) -> tuple:
        """
        Create a webhook with the specified name in a given channel
        and get it's ID and token.
        """

        resp = self.send(
            "POST", f"/channels/{channel_id}/webhooks", {"name": name}
        )

        return resp["id"], resp["token"]

    def get_webhook(self, channel_id: str, name: str) -> discord.Webhook:
        """
        Get the webhook object for the first webhook that matches the specified
        name in a given channel, create the webhook if it doesn't exist.
        """

        # Check the cache first.
        webhook = self.webhook_cache.get(channel_id)
        if webhook:
            return webhook

        webhooks = self.send("GET", f"/channels/{channel_id}/webhooks")
        webhook = next(
            (
                (webhook["id"], webhook["token"])
                for webhook in webhooks
                if webhook["name"] == name
            ),
            None,
        )

        if not webhook:
            webhook = self.create_webhook(channel_id, name)

        webhook = discord.Webhook(id=webhook[0], token=webhook[1])

        self.webhook_cache[channel_id] = webhook

        return webhook

    def send_webhook(
        self, message: matrix.Event, webhook: discord.Webhook
    ) -> str:
        content = {
            "avatar_url": message.author.avatar_url,
            "content": message.body[:2000],
            "username": message.author.display_name,
            # Disable 'everyone' and 'role' mentions.
            "allowed_mentions": {"parse": ["users"]},
        }

        resp = self.send(
            "POST",
            f"/webhooks/{webhook.id}/{webhook.token}",
            content,
            {"wait": True},
        )

        return resp.get("id")

    def edit_webhook(
        self, message: matrix.Event, webhook: discord.Webhook
    ) -> None:
        message_id = message_cache.get(message.event_id)

        if not message_id:
            return

        content = {"content": message.body}

        self.send(
            "PATCH",
            f"/webhooks/{webhook.id}/{webhook.token}/messages/{message_id}",
            content,
        )

    def delete_webhook(
        self, message_id: str, webhook: discord.Webhook
    ) -> None:
        self.send(
            "DELETE",
            f"/webhooks/{webhook.id}/{webhook.token}/messages/{message_id}",
        )

    def send_message(self, message: str, channel_id: str) -> None:
        self.send(
            "POST", f"/channels/{channel_id}/messages", {"content": message}
        )


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

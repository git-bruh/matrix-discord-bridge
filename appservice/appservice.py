import asyncio
import json
import logging
import os
import re
import sys
import threading
import urllib.parse
import uuid
from typing import Dict, List, Tuple, Union

import bottle
import urllib3
import websockets

import discord
import matrix
from db import DataBase
from misc import RequestError, dict_cls


def config_gen(config_file: str) -> dict:
    global basedir

    try:
        basedir = sys.argv[1]
        if not os.path.exists(basedir):
            print(f"Path '{basedir}' does not exist!")
            sys.exit(1)
        basedir = os.path.abspath(basedir)
    except IndexError:
        basedir = os.getcwd()

    config_file = f"{basedir}/{config_file}"

    config_dict = {
        "as_token": "my-secret-as-token",
        "hs_token": "my-secret-hs-token",
        "user_id": "appservice-discord",
        "homeserver": "http://127.0.0.1:8008",
        "server_name": "localhost",
        "discord_token": "my-secret-discord-token",
        "port": 5000,
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

http = urllib3.PoolManager(maxsize=10)
message_cache: Dict[str, Union[discord.Webhook, str]] = {}


class AppService(bottle.Bottle):
    def __init__(self) -> None:
        super(AppService, self).__init__()

        self.as_token = config["as_token"]
        self.hs_token = config["hs_token"]
        self.base_url = config["homeserver"]
        self.port = int(config["port"])
        self.server_name = config["server_name"]
        self.user_id = f"@{config['user_id']}:{self.server_name}"
        self.db = DataBase(config["database"])
        self.discord = DiscordClient(self)
        self.emote_cache: Dict[str, str] = {}
        self.logger = logging.getLogger("appservice")
        self.format = "_discord_"  # "{@,#}_discord_1234:localhost"

        # Add route for bottle.
        self.route(
            "/transactions/<transaction>",
            callback=self.receive_event,
            method="PUT",
        )

    def start(self) -> None:
        self.run(host="127.0.0.1", port=self.port)

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

            try:
                if event_type == "m.room.member":
                    self.handle_member(event)
                elif event_type == "m.room.message":
                    self.handle_message(event)
                elif event_type == "m.room.redaction":
                    self.handle_redaction(event)
            except Exception:
                self.logger.exception("")
                bottle.response.status = 500

                break

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
        content = json.dumps(content) if isinstance(content, dict) else content
        endpoint = (
            f"{self.base_url}{endpoint}{path}?"
            f"{urllib.parse.urlencode(params)}"
        )

        try:
            resp = http.request(
                method, endpoint, body=content, headers=headers
            )
        except urllib3.exceptions.MaxRetryError as e:
            raise RequestError(
                f"Failed to connect to the homeserver: {e}"
            ) from None

        if resp.status < 200 or resp.status >= 300:
            raise RequestError(
                f"Failed to '{method}' '{resp.geturl()}':\n{resp.data}"
            )

        return json.loads(resp.data)

    def get_event_object(self, event: dict) -> matrix.Event:
        content = event["content"]

        # Message edits.
        if content.get("m.relates_to", {}).get("rel_type") == "m.replace":
            relates_to = content.get("m.relates_to").get("event_id")
            new_body = content.get("m.new_content").get("body")
        else:
            relates_to = new_body = None

        room_id = event["room_id"]

        return matrix.Event(
            author=self.get_user_object(event["sender"]),
            body=content.get("body"),
            channel_id=self.db.get_channel(room_id),
            event_id=event["event_id"],
            is_direct=content.get("is_direct", False),
            relates_to=relates_to,
            room_id=room_id,
            new_body=new_body,
            sender=event["sender"],
            state_key=event.get("state_key"),
        )

    def get_user_object(self, mxid: str) -> matrix.User:
        avatar_url, display_name = self.get_profile(mxid)

        return matrix.User(avatar_url, display_name)

    def to_return(self, event: dict) -> bool:
        if event["sender"].startswith(("@_discord", self.user_id)):
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
        self.logger.info(f"Joining direct message room {event.room_id}.")
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

        if (
            channel.type != discord.ChannelType.GUILD_TEXT
            or channel.id in self.db.list_channels()
        ):
            return

        self.logger.info(f"Creating bridged room for channel {channel.id}.")

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

        if message.relates_to:
            # The message was edited.
            relation = message_cache.get(message.relates_to)

            if relation:
                message.new_body = self.process_message(message.new_body)
                self.discord.edit_webhook(
                    message.new_body, relation["message_id"], webhook
                )
        else:
            message.body = self.process_message(message.body)
            message_cache[message.event_id] = {
                "message_id": self.discord.send_webhook(message, webhook),
                "webhook": webhook,
            }

    def handle_redaction(self, event: dict) -> None:
        redacts = event["redacts"]

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
            "room_alias_name": f"{self.format}{channel.id}",
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

    def get_profile(self, mxid: str) -> Tuple[str, str]:
        resp = self.send("GET", f"/profile/{mxid}")

        avatar_url = resp["avatar_url"][6:].split("/")
        try:
            avatar_url = (
                f"{self.base_url}/_matrix/media/r0/download/"
                f"{avatar_url[0]}/{avatar_url[1]}"
            )
        except IndexError:
            avatar_url = None

        display_name = resp.get("displayname")

        return avatar_url, display_name

    def get_members(self, room_id: str) -> List[str]:
        resp = self.send(
            "GET",
            f"/rooms/{room_id}/members",
            params={"membership": "join", "not_membership": "leave"},
        )

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
        Upload a file to the homeserver and get the MXC url.
        """

        resp = http.request("GET", url)

        resp = self.send(
            "POST",
            content=resp.data,
            content_type=resp.headers.get("Content-Type"),
            params={"filename": f"{uuid.uuid4()}"},
            endpoint="/_matrix/media/r0/upload",
        )

        return resp["content_uri"]

    def get_room_id(self, alias: str) -> str:
        resp = self.send("GET", f"/directory/room/{urllib.parse.quote(alias)}")

        # TODO cache

        return resp["room_id"]

    def join_room(self, room_id: str, mxid: str = "") -> None:
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

    def send_message(
        self,
        room_id: str,
        content: dict,
        mxid: str = "",
    ) -> str:
        resp = self.send(
            "PUT",
            f"/rooms/{room_id}/send/m.room.message/{uuid.uuid4()}",
            content,
            {"user_id": mxid} if mxid else {},
        )

        return resp["event_id"]

    def create_message_event(
        self, message: str, emotes: dict, edit: str = "", reply: str = ""
    ) -> dict:
        content = {
            "body": message,
            "format": "org.matrix.custom.html",
            "msgtype": "m.text",
            "formatted_body": self.get_fmt(message, emotes),
        }

        event = message_cache.get(reply)

        if event:
            content = {
                **content,
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": event["event_id"]}
                },
                "formatted_body": f"""<mx-reply><blockquote>\
<a href='https://matrix.to/#/{event["room_id"]}/{event["event_id"]}'>\
In reply to</a><a href='https://matrix.to/#/{event["mxid"]}'>\
{event["mxid"]}</a><br>{event["body"]}</blockquote></mx-reply>\
{content["formatted_body"]}""",
            }

        if edit:
            content = {
                **content,
                "body": f" * {content['body']}",
                "formatted_body": f" * {content['formatted_body']}",
                "m.relates_to": {"event_id": edit, "rel_type": "m.replace"},
                "m.new_content": {**content},
            }

        return content

    def get_fmt(self, message: str, emotes: dict) -> str:
        replace = [
            # Bold.
            ("**", "<strong>", "</strong>"),
            # Code blocks.
            ("```", "<pre><code>", "</code></pre>"),
            # Spoilers.
            ("||", "<span data-mx-spoiler>", "</span>"),
            # Strikethrough.
            ("~~", "<del>", "</del>"),
        ]

        for replace_ in replace:
            for i in range(1, message.count(replace_[0]) + 1):
                if i % 2:
                    message = message.replace(replace_[0], replace_[1], 1)
                else:
                    message = message.replace(replace_[0], replace_[2], 1)

        # Upload emotes in multiple threads so that we don't
        # block the Discord bot for too long.
        upload_threads = [
            threading.Thread(
                target=self.upload_emote, args=(emote, emotes[emote])
            )
            for emote in emotes
        ]

        [thread.start() for thread in upload_threads]
        [thread.join() for thread in upload_threads]

        for emote in emotes:
            emote_ = self.emote_cache.get(emote)

            if emote_:
                emote = f":{emote}:"
                message = message.replace(
                    emote,
                    f"""<img alt=\"{emote}\" title=\"{emote}\" \
height=\"32\" src=\"{emote_}\" data-mx-emoticon />""",
                )

        return message

    def process_message(self, message: str) -> str:
        message = message[:2000]  # Discord limit.

        emotes = re.findall(r":(\w*):", message)

        added_emotes = []
        for emote in emotes:
            # Don't replace emote names with IDs multiple times.
            if emote not in added_emotes:
                added_emotes.append(emote)
                emote_ = self.discord.emote_cache.get(emote)
                if emote_:
                    message = message.replace(f":{emote}:", emote_)

        return message

    def upload_emote(self, emote_name: str, emote_id: str) -> None:
        # There won't be a race condition here, since only a unique
        # set of emotes are uploaded at a time.
        if emote_name in self.emote_cache:
            return

        emote_url = f"{self.discord.cdn_url}/emojis/{emote_id}"

        # We don't want the message to be dropped entirely if an emote
        # fails to upload for some reason.
        try:
            self.emote_cache[emote_name] = self.upload(emote_url)
        except RequestError as e:
            self.logger.warning(f"Failed to upload emote {emote_id}: {e}")


class DiscordClient(object):
    def __init__(self, appservice: AppService) -> None:
        self.app = appservice
        self.logger = logging.getLogger("discord")
        self.token = config["discord_token"]
        self.emote_cache: Dict[str, str] = {}
        self.webhook_cache: Dict[str, discord.Webhook] = {}
        self.cdn_url = "https://cdn.discordapp.com"
        self.heartbeat_task = self.resume = self.seq = self.session = None

    async def start(self) -> None:
        asyncio.ensure_future(self.sync())

        while True:
            try:
                await self.gateway_handler(self.get_gateway_url())
            except websockets.ConnectionClosedError:
                # TODO try to reconnect.
                self.logger.critical("Connection lost, quitting.")
                break

            # Stop sending heartbeats until we reconnect.
            if self.heartbeat_task and not self.heartbeat_task.cancelled():
                self.heartbeat_task.cancel()

    async def sync(self) -> None:
        """
        Periodically compare the usernames and avatar URLs with Discord
        and update if they differ. Also synchronise emotes.
        """

        async def sync_emotes(guilds: set):
            # We could store the emotes once and update according
            # to gateway events but we're too lazy for that.
            emotes = []

            for guild in guilds:
                [emotes.append(emote) for emote in (self.get_emotes(guild))]

            self.emote_cache.clear()  # Clears deleted/renamed emotes.

            for emote in emotes:
                self.emote_cache[f"{emote.name}"] = (
                    f"<{'a' if emote.animated else ''}:"
                    f"{emote.name}:{emote.id}>"
                )

        async def sync_users(guilds: set):
            users = []

            for guild in guilds:
                [users.append(member) for member in self.get_members(guild)]

            db_users = self.app.db.list_users()

            # Convert a list of dicts:
            # [ { "avatar_url": ... } ]
            # to a dict that is indexable by Discord IDs:
            # { "discord_id": { "avatar_url": ... } }
            users_ = {}

            for user in db_users:
                users_[user["mxid"].split("_")[-1].split(":")[0]] = {**user}

            for user in users:
                user_ = users_.get(user.id)

                if not user_:
                    continue

                mxid = user_["mxid"]
                username = f"{user.username}#{user.discriminator}"

                if user.avatar_url != user_["avatar_url"]:
                    self.logger.info(
                        f"Updating avatar for Discord user {user.id}."
                    )
                    self.app.set_avatar(user.avatar_url, mxid)

                if username != user_["username"]:
                    self.logger.info(
                        f"Updating username for Discord user {user.id}."
                    )
                    self.app.set_nick(username, mxid)

        while True:
            guilds = set()  # Avoid duplicates.

            for channel in self.app.db.list_channels():
                guilds.add(self.get_channel(channel).guild_id)

            await sync_emotes(guilds)
            await sync_users(guilds)

            await asyncio.sleep(120)  # Check every 2 minutes.

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
                        try:
                            if otype == "MESSAGE_CREATE":
                                self.handle_message(data_dict)

                            elif otype == "MESSAGE_DELETE":
                                self.handle_redaction(data_dict)

                            elif otype == "MESSAGE_UPDATE":
                                self.handle_edit(data_dict)

                            elif otype == "TYPING_START":
                                self.handle_typing(data_dict)
                        except Exception:
                            self.logger.exception("")

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

    def get_gateway_url(self) -> str:
        resp = self.send("GET", "/gateway")

        return resp["url"]

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

    def matrixify(self, id: str, user: bool = False) -> str:
        return (
            f"{'@' if user else '#'}{self.app.format}{id}:"
            f"{self.app.server_name}"
        )

    def to_return(self, message: discord.Message) -> bool:
        if (
            message.channel_id not in self.app.db.list_channels()
            or message.author.discriminator == "0000"
        ):
            return True

        return False

    def wrap(self, message: discord.Message) -> Tuple[str, str]:
        """
        Get the corresponding room ID and the puppet's mxid for
        a given channel ID and a Discord user.
        """

        mxid = self.matrixify(message.author.id, user=True)
        room_id = self.app.get_room_id(self.matrixify(message.channel_id))

        if not self.app.db.query_user(mxid):
            self.logger.info(
                f"Creating dummy user for Discord user {message.author.id}."
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

        content, emotes = self.process_message(message)

        content = self.app.create_message_event(
            content, emotes, reply=message.reference
        )

        message_cache[message.id] = {
            "body": content["body"],
            "event_id": self.app.send_message(room_id, content, mxid),
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

        event = message_cache.get(message.id)

        content, emotes = self.process_message(message)

        if event:
            content = self.app.create_message_event(
                content, emotes, edit=event["event_id"]
            )
            self.app.send_message(event["room_id"], content, event["mxid"])

    def handle_typing(self, typing: dict) -> None:
        typing = dict_cls(typing, discord.Typing)

        if typing.channel_id not in self.app.db.list_channels():
            return

        mxid = self.matrixify(typing.user_id, user=True)
        room_id = self.app.get_room_id(self.matrixify(typing.channel_id))

        if mxid not in self.app.get_members(room_id):
            return

        self.app.send_typing(room_id, mxid)

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

        resp = http.request(method, endpoint, body=content, headers=headers)

        if resp.status < 200 or resp.status >= 300:
            raise RequestError(
                f"Failed to '{method}' '{resp.geturl()}':\n{resp.data}"
            )

        return {} if resp.status == 204 else json.loads(resp.data)

    def get_channel(self, channel_id: str) -> discord.Channel:
        """
        Get the corresponding `discord.Channel` object for a given channel ID.
        """

        resp = self.send("GET", f"/channels/{channel_id}")

        return discord.Channel(resp)

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

        return [self.get_user_object(member["user"]) for member in resp]

    def create_webhook(self, channel_id: str, name: str) -> discord.Webhook:
        """
        Create a webhook with the specified name in a given channel
        and get it's ID and token.
        """

        resp = self.send(
            "POST", f"/channels/{channel_id}/webhooks", {"name": name}
        )

        return dict_cls(resp, discord.Webhook)

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
                dict_cls(webhook, discord.Webhook)
                for webhook in webhooks
                if webhook["name"] == name
            ),
            None,
        )

        if not webhook:
            webhook = self.create_webhook(channel_id, name)

        self.webhook_cache[channel_id] = webhook

        return webhook

    def send_webhook(
        self, message: matrix.Event, webhook: discord.Webhook
    ) -> str:
        content = {
            "avatar_url": message.author.avatar_url,
            "content": message.body,
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

        return resp["id"]

    def edit_webhook(
        self, content: str, message_id: str, webhook: discord.Webhook
    ) -> None:
        try:
            self.send(
                "PATCH",
                f"/webhooks/{webhook.id}/{webhook.token}/messages/"
                f"{message_id}",
                {"content": content},
            )
        except RequestError as e:
            self.logger.warning(
                f"Failed to edit webhook message {message_id}: {e}"
            )

    def delete_webhook(
        self, message_id: str, webhook: discord.Webhook
    ) -> None:
        try:
            self.send(
                "DELETE",
                f"/webhooks/{webhook.id}/{webhook.token}/messages/"
                f"{message_id}",
            )
        except RequestError as e:
            self.logger.warning(
                f"Failed to delete webhook message {message_id}: {e}"
            )

    def send_message(self, message: str, channel_id: str) -> None:
        self.send(
            "POST", f"/channels/{channel_id}/messages", {"content": message}
        )

    def process_message(self, message: discord.Message) -> Tuple[str, str]:
        content = message.content
        regex = r"<a?:(\w+):(\d+)>"
        emotes = {}

        # { "emote_name": "emote_id" }
        for emote in re.findall(regex, message.content):
            emotes[emote[0]] = emote[1]

        # Replace emote IDs with names.
        content = re.sub(regex, r":\g<1>:", content)

        # Append attachments to message.
        for attachment in message.attachments:
            content += f"\n{attachment['url']}"

        return content, emotes


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s:%(levelname)s:%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(f"{basedir}/appservice.log"),
            logging.StreamHandler(),
        ],
    )

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

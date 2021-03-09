import asyncio
import json
import logging
import os
import threading
import sys
import uuid
import urllib3
import bottle
import db
import discord
import websockets
import matrix
from typing import Union
from db import DataBase


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

    def get_event_object(self, event: dict) -> matrix.Event:
        content = event.get("content")

        body       = content.get("body")
        event_id   = event.get("event_id")
        homeserver = event.get("sender").split(":")[-1]
        is_direct  = content.get("is_direct")
        room_id    = event.get("room_id")
        sender     = event.get("sender")
        channel_id = self.db.get_channel(room_id)

        return matrix.Event(
            body, channel_id, event_id, is_direct, homeserver, room_id, sender
        )

    async def get_user_object(self, mxid: str) -> matrix.User:
        avatar_url, display_name = await self.get_profile(mxid)

        return matrix.User(avatar_url, display_name)

    async def handle_member(self, event: dict) -> None:
        event = self.get_event_object(event)

        # Ignore invites from other homeservers.
        if event.sender.split(":")[-1] != self.plain_url:
            return

        # Join the direct message room.
        if event.is_direct:
            logging.info(f"Joining direct message room {event.room_id}")
            await self.join_room(event.room_id)

    async def handle_bridge(self, message: matrix.Event) -> None:
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

        self.Payloads = discord.Payloads(self.token)

    async def start(self):
        await self.gateway_handler(self.get_gateway_url())

    async def heartbeat_handler(self, websocket, interval_ms: int) -> None:
        while True:
            await asyncio.sleep(interval_ms / 1000)
            await websocket.send(json.dumps(self.Payloads.HEARTBEAT))

    async def gateway_handler(self, gateway_url: str) -> None:
        gateway_url += "/?v=8&encoding=json"
        async with websockets.connect(gateway_url) as websocket:
            async for message in websocket:
                data      = json.loads(message)
                data_dict = data.get("d")

                opcode = data.get("op")

                if opcode == discord.GatewayOpCodes.DISPATCH:
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

                elif opcode == discord.GatewayOpCodes.HELLO:
                    heartbeat_interval = data_dict.get("heartbeat_interval")
                    logging.info(f"Heartbeat Interval: {heartbeat_interval}")

                    # Send periodic hearbeats to gateway.
                    asyncio.ensure_future(self.heartbeat_handler(
                        websocket, heartbeat_interval
                    ))

                    await websocket.send(json.dumps(self.Payloads.IDENTIFY))

                elif opcode == discord.GatewayOpCodes.HEARTBEAT_ACK:
                    # NOP
                    pass

                else:
                    logging.info(f"Unknown event:\n{json.dumps(data, indent=4)}")

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

    def get_message_object(self, message: dict) -> discord.Message:
        author = self.get_member_object(message.get("author"))

        attachments = message.get("attachments")
        content     = message.get("content")
        channel_id  = message.get("channel_id")
        message_id  = message.get("id")
        edited      = True if message.get("edited_timestamp") else False

        return discord.Message(attachments, author, content, channel_id, edited, message_id)

    def to_return(self, message: discord.Message) -> bool:
        if message.author.discriminator == "0000":
            return True

        return False

    def handle_message(self, message: dict) -> None:
        message = self.get_message_object(message)

        if self.to_return(message):
            return

        if message.content.startswith("test"):
            print(message.content)
            self.send_webhook()

    def handle_deletion(self, message: dict) -> None:
        return
        # self.app.redact(message.get("id")) # message.get("channel_id")

    def handle_edit(self, message: dict) -> None:
        message = self.get_message_object(message)

        if self.to_return(message):
            return

    def send(self, method: str, path: str, content: dict = {}, params: dict = {}) -> dict:
        endpoint = "https://discord.com/api/v8"
        headers  = {"Authorization": f"Bot {self.token}", "Content-Type": "application/json"}

        # 'body' being an empty dict breaks "GET" requests.
        content = json.dumps(content) if content else None

        resp = self.app.manager.request(method, f"{endpoint}{path}", body=content,
                                        fields=params, headers=headers)
        print(resp.status)
        print(resp.data)

        return json.loads(resp.data)

    def get_gateway_url(self) -> str:
        resp = self.send("GET", "/gateway")

        return resp.get("url")

    def get_webhooks(self, channel_id: str) -> None:
        webhooks = self.send("GET", f"/channels/{channel_id}/webhooks")
        return [ {webhook["name"]: webhook["token"]} for webhook in webhooks ]

    def send_webhook(self) -> str:
        content = {"content": "Webhook testing", "username": "a2z",
                   # Disable 'everyone' and 'role' mentions.
                   "allowed_mentions": {"parse": ["users"]}}

        # self.send("POST", f"/webhooks/{webhook_id}/{webhook_token}?wait=True", content)
        # return resp.get("id")

    def edit_webhook(self, message: str) -> None:
        content = {"content": message}
        # self.send("PATCH", f"/webhooks/{webhook_id}/{webhook_token}/messages/{message_id}", content)

    def delete_webhook(self, message: str) -> None:
        # self.send("DELETE", f"/webhooks/{webhook_id}/{webhook_token}/messages/{message_id})
        pass

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

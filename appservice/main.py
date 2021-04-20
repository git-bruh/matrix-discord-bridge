import asyncio
import json
import logging
import os
import re
import sys
import threading
from typing import Dict, Tuple, Union

import urllib3

import discord
import matrix
from appservice import AppService
from db import DataBase
from errors import RequestError
from gateway import Gateway
from misc import dict_cls, except_deleted, hash_str

# TODO should this be cleared periodically ?
message_cache: Dict[str, Union[discord.Webhook, str]] = {}


class MatrixClient(AppService):
    def __init__(self, config: dict, http: urllib3.PoolManager) -> None:
        super().__init__(config, http)

        self.db = DataBase(config["database"])
        self.discord = DiscordClient(self, config, http)
        self.emote_cache: Dict[str, str] = {}
        self.format = "_discord_"  # "{@,#}_discord_1234:localhost"

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
        try:
            channel = self.discord.get_channel(channel)
        except RequestError as e:
            # The channel can be invalid or we may not have permission.
            self.logger.warning(f"Failed to fetch channel {channel}: {e}")
            return

        if (
            channel.type != discord.ChannelType.GUILD_TEXT
            or channel.id in self.db.list_channels()
        ):
            return

        self.logger.info(f"Creating bridged room for channel {channel.id}.")

        self.create_room(channel, message.sender)

    def on_member(self, event: matrix.Event) -> None:
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

    def on_message(self, message: matrix.Event) -> None:
        if (
            message.sender.startswith((f"@{self.format}", self.user_id))
            or not message.body
        ):
            return

        # Handle bridging commands.
        self.handle_bridge(message)

        channel_id = self.db.get_channel(message.room_id)

        if not channel_id:
            return

        webhook = self.discord.get_webhook(channel_id, "matrix_bridge")

        if message.relates_to and message.reltype == "m.replace":
            relation = message_cache.get(message.relates_to)

            if not message.new_body or not relation:
                return

            message.new_body = self.process_message(
                channel_id, message.new_body
            )

            except_deleted(self.discord.edit_webhook)(
                message.new_body, relation["message_id"], webhook
            )

        else:
            message.body = (
                f"`{message.body}`: {self.mxc_url(message.attachment)}"
                if message.attachment
                else self.process_message(channel_id, message.body)
            )

            message_cache[message.event_id] = {
                "message_id": self.discord.send_webhook(
                    webhook,
                    avatar_url=message.author.avatar_url,
                    content=message.body,
                    username=message.author.displayname,
                ),
                "webhook": webhook,
            }

    @except_deleted
    def on_redaction(self, event: dict) -> None:
        redacts = event["redacts"]

        event = message_cache.get(redacts)

        if not event:
            return

        self.discord.delete_webhook(event["message_id"], event["webhook"])

        message_cache.pop(redacts)

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
                    "content": {"join_rule": "public"},
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

    def process_message(self, channel_id: str, message: str) -> str:
        message = message[:2000]  # Discord limit.

        emotes = re.findall(r":(\w*):", message)
        mentions = re.findall(r"(@(\w*))", message)

        # Remove the puppet user's username from replies.
        message = re.sub(f"<@{self.format}.+?>", "", message)

        added_emotes = []
        for emote in emotes:
            # Don't replace emote names with IDs multiple times.
            if emote not in added_emotes:
                added_emotes.append(emote)
                emote_ = self.discord.emote_cache.get(emote)
                if emote_:
                    message = message.replace(f":{emote}:", emote_)

        # Don't unnecessarily fetch the channel.
        if mentions:
            guild_id = self.discord.get_channel(channel_id).guild_id

        # TODO this can block for too long if a long list is to be fetched.
        for mention in mentions:
            if not mention[1]:
                continue

            try:
                member = self.discord.query_member(guild_id, mention[1])
            except (asyncio.TimeoutError, RuntimeError):
                continue

            if member:
                message = message.replace(mention[0], member.mention)

        return message

    def upload_emote(self, emote_name: str, emote_id: str) -> None:
        # There won't be a race condition here, since only a unique
        # set of emotes are uploaded at a time.
        if emote_name in self.emote_cache:
            return

        emote_url = f"{discord.CDN_URL}/emojis/{emote_id}"

        # We don't want the message to be dropped entirely if an emote
        # fails to upload for some reason.
        try:
            self.emote_cache[emote_name] = self.upload(emote_url)
        except RequestError as e:
            self.logger.warning(f"Failed to upload emote {emote_id}: {e}")

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

    def set_avatar(self, avatar_url: str, mxid: str) -> None:
        avatar_uri = self.upload(avatar_url)

        self.send(
            "PUT",
            f"/profile/{mxid}/avatar_url",
            {"avatar_url": avatar_uri},
            params={"user_id": mxid},
        )

        self.db.add_avatar(avatar_url, mxid)

    def set_nick(self, username: str, mxid: str) -> None:
        self.send(
            "PUT",
            f"/profile/{mxid}/displayname",
            {"displayname": username},
            params={"user_id": mxid},
        )

        self.db.add_username(username, mxid)


class DiscordClient(Gateway):
    def __init__(
        self, appservice: MatrixClient, config: dict, http: urllib3.PoolManager
    ) -> None:
        super().__init__(http, config["discord_token"])

        self.app = appservice
        self.emote_cache: Dict[str, str] = {}
        self.webhook_cache: Dict[str, discord.Webhook] = {}

    async def sync(self) -> None:
        """
        Periodically compare the usernames and avatar URLs with Discord
        and update if they differ. Also synchronise emotes.
        """

        # TODO use websocket events and requests.

        def sync_emotes(guilds: set):
            emotes = []

            for guild in guilds:
                [emotes.append(emote) for emote in (self.get_emotes(guild))]

            self.emote_cache.clear()  # Clear deleted/renamed emotes.

            for emote in emotes:
                self.emote_cache[f"{emote.name}"] = (
                    f"<{'a' if emote.animated else ''}:"
                    f"{emote.name}:{emote.id}>"
                )

        def sync_users(guilds: set):
            for guild in guilds:
                [
                    self.sync_profile(user, self.matrixify(user.id, user=True))
                    for user in self.get_members(guild)
                ]

        while True:
            guilds = set()  # Avoid duplicates.

            try:
                for channel in self.app.db.list_channels():
                    guilds.add(self.get_channel(channel).guild_id)

                sync_emotes(guilds)
                sync_users(guilds)
            # Don't let the background task die.
            except RequestError:
                self.logger.exception(
                    "Ignoring exception during background sync:"
                )

            await asyncio.sleep(120)  # Check every 2 minutes.

    async def start(self) -> None:
        asyncio.ensure_future(self.sync())

        await self.run()

    def to_return(self, message: discord.Message) -> bool:
        return (
            message.channel_id not in self.app.db.list_channels()
            or not message.content
            or not message.author  # Embeds can be weird sometimes.
            or message.webhook_id
            in [hook.id for hook in self.webhook_cache.values()]
        )

    def matrixify(self, id: str, user: bool = False) -> str:
        return (
            f"{'@' if user else '#'}{self.app.format}{id}:"
            f"{self.app.server_name}"
        )

    def sync_profile(self, user: discord.User, mxid: str) -> None:
        """
        Sync the avatar and username for a puppeted user.
        """

        profile = self.app.db.fetch_user(mxid)

        # User doesn't exist.
        if not profile:
            return

        username = f"{user.username}#{user.discriminator}"

        if user.avatar_url != profile["avatar_url"]:
            self.logger.info(f"Updating avatar for Discord user {user.id}")
            self.app.set_avatar(user.avatar_url, mxid)
        if username != profile["username"]:
            self.logger.info(f"Updating username for Discord user {user.id}")
            self.app.set_nick(username, mxid)

    def wrap(self, message: discord.Message) -> Tuple[str, str]:
        """
        Get the room ID and the puppet's mxid for a given channel ID and a
        Discord user.
        """

        if message.webhook_id:
            hashed = hash_str(message.author.username)
            message.author.id = str(int(message.author.id) + hashed)

        mxid = self.matrixify(message.author.id, user=True)
        room_id = self.app.get_room_id(self.matrixify(message.channel_id))

        if not self.app.db.fetch_user(mxid):
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
            self.logger.info(f"Inviting user {mxid} to room {room_id}.")

            self.app.send_invite(room_id, mxid)
            self.app.join_room(room_id, mxid)

        if message.webhook_id:
            # Sync webhooks here as they can't be accessed like guild members.
            self.sync_profile(message.author, mxid)

        return mxid, room_id

    def on_message_create(self, message: discord.Message) -> None:
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

    def on_message_delete(self, message: discord.DeletedMessage) -> None:
        event = message_cache.get(message.id)

        if not event:
            return

        self.app.redact(event["event_id"], event["room_id"], event["mxid"])

        message_cache.pop(message.id)

    def on_message_update(self, message: discord.Message) -> None:
        if self.to_return(message):
            return

        event = message_cache.get(message.id)

        if not event:
            return

        content, emotes = self.process_message(message)

        content = self.app.create_message_event(
            content, emotes, edit=event["event_id"]
        )

        self.app.send_message(event["room_id"], content, event["mxid"])

    def on_typing_start(self, typing: discord.Typing) -> None:
        if typing.channel_id not in self.app.db.list_channels():
            return

        mxid = self.matrixify(typing.user_id, user=True)
        room_id = self.app.get_room_id(self.matrixify(typing.channel_id))

        if mxid not in self.app.get_members(room_id):
            return

        self.app.send_typing(room_id, mxid)

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

    def process_message(self, message: discord.Message) -> Tuple[str, str]:
        content = message.content
        emotes = {}
        regex = r"<a?:(\w+):(\d+)>"

        # Mentions can either be in the form of `<@1234>` or `<@!1234>`.
        for char in ("", "!"):
            for member in message.mentions:
                content = content.replace(
                    f"<@{char}{member.id}>", f"@{member.username}"
                )

        # `except_deleted` for invalid channels.
        for channel in re.findall(r"<#([0-9]+)>", content):
            channel_ = except_deleted(self.get_channel)(channel)
            content = content.replace(
                f"<#{channel}>",
                f"#{channel_.name}" if channel_ else "deleted-channel",
            )

        # { "emote_name": "emote_id" }
        for emote in re.findall(regex, content):
            emotes[emote[0]] = emote[1]

        # Replace emote IDs with names.
        content = re.sub(regex, r":\g<1>:", content)

        # Append attachments to message.
        for attachment in message.attachments:
            content += f"\n{attachment['url']}"

        return content, emotes


def config_gen(basedir: str, config_file: str) -> dict:
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


def main() -> None:
    try:
        basedir = sys.argv[1]
        if not os.path.exists(basedir):
            print(f"Path '{basedir}' does not exist!")
            sys.exit(1)
        basedir = os.path.abspath(basedir)
    except IndexError:
        basedir = os.getcwd()

    config = config_gen(basedir, "appservice.json")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s:%(levelname)s:%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(f"{basedir}/appservice.log"),
        ],
    )

    http = urllib3.PoolManager(maxsize=10)

    app = MatrixClient(config, http)

    # Start the bottle app in a separate thread.
    app_thread = threading.Thread(
        target=app.run, kwargs={"port": int(config["port"])}, daemon=True
    )
    app_thread.start()

    try:
        asyncio.run(app.discord.start())
    except KeyboardInterrupt:
        sys.exit()


if __name__ == "__main__":
    main()

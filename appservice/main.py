import asyncio
import json
import logging
import os
import re
import sys
import threading
import urllib.parse
from typing import Dict, List, Tuple, Optional

import markdown
import urllib3

import discord
import matrix
from appservice import AppService
from cache import Cache
from db import DataBase
from errors import RequestError
from gateway import Gateway
from message_parser import MatrixParser, escape_markdown
from misc import dict_cls, except_deleted, hash_str


class MatrixClient(AppService):
    def __init__(self, config: dict, http: urllib3.PoolManager) -> None:
        super().__init__(config, http)

        self.db = DataBase(config["database"])
        self.discord = DiscordClient(self, config, http)
        self.format = "_discord_"  # "{@,#}_discord_1234:localhost"
        self.id_regex = "[0-9]+"  # Snowflakes may have variable length

        # TODO Find a cleaner way to use these keys.
        for k in ("m_emotes", "m_members", "m_messages"):
            Cache.cache[k] = {}

    def handle_bridge(self, message: matrix.Event) -> None:
        # Ignore events that aren't for us.
        if message.sender.split(":")[
            -1
        ] != self.server_name or not message.body.startswith("!bridge"):
            return

        # Get the channel ID.
        try:
            channel = message.body.split()[1]
        except IndexError:
            return

        # Check if the given channel is valid.
        try:
            channel = self.discord.get_channel(channel)
        except RequestError as e:
            # The channel can be invalid or we may not have permissions.
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
        with Cache.lock:
            # Just lazily clear the whole member cache on
            # membership update events.
            if event.room_id in Cache.cache["m_members"]:
                self.logger.info(
                    f"Clearing member cache for room '{event.room_id}'."
                )
                del Cache.cache["m_members"][event.room_id]

        if (
            event.sender.split(":")[-1] != self.server_name
            or event.state_key != self.user_id
            or not event.is_direct
        ):
            return

        # Join the direct message room.
        self.logger.info(f"Joining direct message room '{event.room_id}'.")
        self.join_room(event.room_id)

    def append_replied_to_msg(self, message: matrix.Event) -> str:
        def escape_urls(message_: str):
            return re.sub(r"(?<!<)http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+", "<\g<0>>", message_)
        if message.reply and message.reply.get("event_id"):
            replied_to_body: Optional[matrix.Event] = except_deleted(self.get_event)(message.reply["event_id"], message.room_id)
            if replied_to_body and not replied_to_body.redacted_because:
                return "> " + escape_urls(self.parse_message(replied_to_body, limit=600, generate_link=False).replace("\n", "\n> ").strip()) + "\n"
            else:
                return "> 🗑️💬\n"  # I really don't want to add translatable strings to this project
        return ""

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

        author = self.get_members(message.room_id)[message.sender]

        webhook = self.discord.get_webhook(
            channel_id, self.discord.webhook_name
        )

        # Let's take a few scenarios that can happen. We should handle at least 2 special message cases: replies and edits
        # Replies should ask for replied to message event, parse that event, limit output to maybe like 500 characters
        # and prepend it to main message in form of a quote, we can't just use Discord's reply because Discord being dumb
        # https://github.com/discord/discord-api-docs/discussions/3282
        # Edits should look at previously edited message, if it was a reply they need to handle all that reply logic again
        # However edits lose replied to field so we have to fetch original message (wooho?) and get it from that instead

        content = ""

        if message.relates_to and message.reltype == "m.replace":
            with Cache.lock:
                message_id = Cache.cache["m_messages"].get(message.relates_to)

            original_message: Optional[matrix.Event] = except_deleted(self.get_event)(message.relates_to, message.room_id)

            if not message_id or not message.new_body:
                return

            if original_message:
                if message.sender != original_message.sender:
                    return
                content += self.append_replied_to_msg(original_message)
            # If new body has formatted form, use that
            message.body = message.new_body.get("body", "")
            message.formatted_body = message.new_body.get("formatted_body", "")
            content += self.parse_message(message)

            except_deleted(self.discord.edit_webhook)(
                content[:discord.MESSAGE_LIMIT], message_id, webhook
            )
        else:
            content += self.append_replied_to_msg(message)
            content += (
                f"`{message.body}`: {self.mxc_url(message.attachment)}"
                if message.attachment
                else self.parse_message(message)
            )
            if not content or content.isspace():
                return
            message_id = self.discord.send_webhook(
                webhook,
                self.mxc_url(author.avatar_url) if author.avatar_url else None,
                content[:discord.MESSAGE_LIMIT],
                author.display_name if author.display_name else message.sender,
            ).id

            with Cache.lock:
                Cache.cache["m_messages"][message.id] = message_id

    @staticmethod
    def create_msg_link(room_id: str, event: str) -> str:
        return f"[[…]](<https://matrix.to/#/{room_id}/{event}>)"

    def parse_message(self, message: matrix.Event, limit: int = discord.MESSAGE_LIMIT, generate_link: bool = True):
        if message.formatted_body:
            msg_link = self.create_msg_link(message.room_id, message.id) if generate_link else ""
            parser = MatrixParser(self.db, self.mention_regex(False, True), self.mxc_url, limit=limit-len(msg_link))
            try:
                parser.feed(message.formatted_body)
            except StopIteration:
                self.logger.debug("Message has exceeded maximum allowed character limit, processing what we already have")
                message.body = parser.message
                # Create a link to message for Discord side to spread the word about Matrix superior character limit
                if generate_link:
                    message.body += msg_link
            else:
                message.body = parser.message
        else:
            # if we escape : in protocol prefix of a link is going to be plaintext on Discord, we don't want that
            # but we still have to escape : for emojis so this is a measure for that
            message.body = escape_markdown(message.body).replace("\\://", "://")
        return message.body

    def on_redaction(self, event: matrix.Event) -> None:
        with Cache.lock:
            message_id = Cache.cache["m_messages"].get(event.redacts)

        if not message_id:
            return

        webhook = self.discord.get_webhook(
            self.db.get_channel(event.room_id), self.discord.webhook_name
        )

        except_deleted(self.discord.delete_webhook)(message_id, webhook)

        with Cache.lock:
            del Cache.cache["m_messages"][event.redacts]

    def get_members(self, room_id: str) -> Dict[str, matrix.User]:
        with Cache.lock:
            cached = Cache.cache["m_members"].get(room_id)

        if cached:
            return cached

        resp = self.send("GET", f"/rooms/{room_id}/joined_members")

        joined = resp["joined"]

        for k, v in joined.items():
            joined[k] = dict_cls(v, matrix.User)

        with Cache.lock:
            Cache.cache["m_members"][room_id] = joined

        return joined

    def create_room(self, channel: discord.Channel, sender: str) -> None:
        """
        Create a bridged room and invite the person who invoked the command.
        """

        content = {
            "room_alias_name": f"{self.format}{channel.id}",
            "name": channel.name,
            "topic": channel.topic if channel.topic else channel.name,
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
            "power_level_content_override": {
                "users": {sender: 100, self.user_id: 100}
            },
        }

        resp = self.send("POST", "/createRoom", content)

        self.db.add_room(resp["room_id"], channel.id)

    def create_message_event(
        self,
        message: str,
        emotes: dict,
        edit: str = "",
        reference: discord.Message = None,
    ) -> dict:
        content = {
            "body": message,
            "msgtype": "m.text",
        }

        fmt = self.get_fmt(message, emotes)

        if len(fmt) != len(message):
            content = {
                **content,
                "format": "org.matrix.custom.html",
                "formatted_body": fmt,
            }

        ref_id = None

        if reference:
            # Reply to a Discord message.
            with Cache.lock:
                ref_id = Cache.cache["d_messages"].get(reference.id)

            # Reply to a Matrix message. (maybe)
            if not ref_id:
                with Cache.lock:
                    ref_id = [
                        k
                        for k, v in Cache.cache["m_messages"].items()
                        if v == reference.id
                    ]
                    ref_id = next(iter(ref_id), "")

        if ref_id:
            event = except_deleted(self.get_event)(
                ref_id,
                self.get_room_id(self.discord.matrixify(reference.channel_id)),
            )
            if event:
                # Content with the reply fallbacks stripped.
                tmp = ""
                # We don't want to strip lines starting with "> " after
                # encountering a regular line, so we use this variable.
                got_fallback = True
                for line in event.body.split("\n"):
                    if not line.startswith("> "):
                        got_fallback = False
                    if not got_fallback:
                        tmp += line

                event.body = tmp
                event.formatted_body = (
                    re.sub("<mx-reply>.*</mx-reply>", "", event.formatted_body)
                    if event.formatted_body
                    else event.body
                )

                content = {
                    **content,
                    "body": (
                        f"> <{event.sender}> {event.body}\n{content['body']}"
                    ),
                    "m.relates_to": {"m.in_reply_to": {"event_id": event.id}},
                    "format": "org.matrix.custom.html",
                    "formatted_body": f"""<mx-reply><blockquote>\
<a href="https://matrix.to/#/{event.room_id}/{event.id}">\
In reply to</a><a href="https://matrix.to/#/{event.sender}">\
{event.sender}</a><br>\
{event.formatted_body if event.formatted_body else event.body}\
</blockquote></mx-reply>\
{content.get("formatted_body", content['body'])}""",
                }

        if edit:
            content = {
                **content,
                "body": f" * {content['body']}",
                "formatted_body": f" * {content.get('formatted_body', content['body'])}",
                "m.relates_to": {"event_id": edit, "rel_type": "m.replace"},
                "m.new_content": {**content},
            }

        return content

    def get_fmt(self, message: str, emotes: dict) -> str:
        message = (
            markdown.markdown(message).replace("<p>", "").replace("</p>", "")
        )

        # Upload emotes in multiple threads so that we don't
        # block the Discord bot for too long.
        upload_threads = [
            threading.Thread(
                target=self.upload_emote, args=(emote, emotes[emote])
            )
            for emote in emotes
        ]

        # Acquire the lock before starting the threads to avoid resource
        # contention by tens of threads at once.
        with Cache.lock:
            for thread in upload_threads:
                thread.start()
            for thread in upload_threads:
                thread.join()

        with Cache.lock:
            for emote in emotes:
                emote_ = Cache.cache["m_emotes"].get(emote)

                if emote_:
                    emote = f":{emote}:"
                    message = message.replace(
                        emote,
                        f"""<img alt=\"{emote}\" title=\"{emote}\" \
height=\"32\" src=\"{emote_}\" data-mx-emoticon />""",
                    )

        return message

    def mention_regex(self, encode: bool, id_as_group: bool) -> str:
        mention = "@"
        colon = ":"
        snowflake = self.id_regex

        if encode:
            mention = urllib.parse.quote(mention)
            colon = urllib.parse.quote(colon)

        if id_as_group:
            snowflake = f"({snowflake})"

        hashed = f"(?:-{snowflake})?"

        return f"{mention}{self.format}{snowflake}{hashed}{colon}{re.escape(self.server_name)}"

    def upload_emote(self, emote_name: str, emote_id: str) -> None:
        # There won't be a race condition here, since only a unique
        # set of emotes are uploaded at a time.
        if emote_name in Cache.cache["m_emotes"]:
            return

        emote_url = f"{discord.CDN_URL}/emojis/{emote_id}"

        # We don't want the message to be dropped entirely if an emote
        # fails to upload for some reason.
        try:
            # TODO This is not thread safe, but we're protected by the GIL.
            Cache.cache["m_emotes"][emote_name] = self.upload(emote_url)
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
        self.webhook_name = "matrix_bridge"

        # TODO Find a cleaner way to use these keys.
        for k in ("d_emotes", "d_messages", "d_webhooks"):
            Cache.cache[k] = {}

    def to_return(self, message: discord.Message) -> bool:
        with Cache.lock:
            hook_ids = [hook.id for hook in Cache.cache["d_webhooks"].values()]

        return (
            message.channel_id not in self.app.db.list_channels()
            or not message.author  # Embeds can be weird sometimes.
            or message.webhook_id in hook_ids
        )

    def matrixify(self, id: str, user: bool = False, hashed: str = "") -> str:
        return (
            f"{'@' if user else '#'}{self.app.format}"
            f"{id}{'-' + hashed if hashed else ''}:"
            f"{self.app.server_name}"
        )

    def sync_profile(self, user: discord.User, hashed: str = "") -> None:
        """
        Sync the avatar and username for a puppeted user.
        """

        mxid = self.matrixify(user.id, user=True, hashed=hashed)

        profile = self.app.db.fetch_user(mxid)

        # User doesn't exist.
        if not profile:
            return

        username = f"{user.username}#{user.discriminator}"

        if user.avatar_url != profile["avatar_url"]:
            self.logger.info(f"Updating avatar for Discord user '{user.id}'")
            self.app.set_avatar(user.avatar_url, mxid)
        if username != profile["username"]:
            self.logger.info(f"Updating username for Discord user '{user.id}'")
            self.app.set_nick(username, mxid)

    def wrap(self, message: discord.Message) -> Tuple[str, str]:
        """
        Get the room ID and the puppet's mxid for a given channel ID and a
        Discord user.
        """

        hashed = ""
        if message.webhook_id and message.webhook_id != message.application_id:
            hashed = str(hash_str(message.author.username))

        mxid = self.matrixify(message.author.id, user=True, hashed=hashed)
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
            self.logger.info(f"Inviting user '{mxid}' to room '{room_id}'.")

            self.app.send_invite(room_id, mxid)
            self.app.join_room(room_id, mxid)

        if message.webhook_id:
            # Sync webhooks here as they can't be accessed like guild members.
            self.sync_profile(message.author, hashed=hashed)

        return mxid, room_id

    def cache_emotes(self, emotes: List[discord.Emote]):
        # TODO maybe "namespace" emotes by guild in the cache ?
        with Cache.lock:
            for emote in emotes:
                Cache.cache["d_emotes"][emote.name] = (
                    f"<{'a' if emote.animated else ''}:"
                    f"{emote.name}:{emote.id}>"
                )

    def on_guild_create(self, guild: discord.Guild) -> None:
        for member in guild.members:
            self.sync_profile(member)

        self.cache_emotes(guild.emojis)

    def on_guild_emojis_update(
        self, update: discord.GuildEmojisUpdate
    ) -> None:
        self.cache_emotes(update.emojis)

    def on_guild_member_update(
        self, update: discord.GuildMemberUpdate
    ) -> None:
        self.sync_profile(update.user)

    def on_message_create(self, message: discord.Message) -> None:
        if self.to_return(message):
            return

        mxid, room_id = self.wrap(message)

        content_, emotes = self.process_message(message)

        content = self.app.create_message_event(
            content_, emotes, reference=message.referenced_message
        )

        with Cache.lock:
            Cache.cache["d_messages"][message.id] = self.app.send_message(
                room_id, content, mxid
            )

    def on_message_delete(self, message: discord.Message) -> None:
        with Cache.lock:
            event_id = Cache.cache["d_messages"].get(message.id)

        if not event_id:
            return

        room_id = self.app.get_room_id(self.matrixify(message.channel_id))
        event = except_deleted(self.app.get_event)(event_id, room_id)

        if event:
            self.app.redact(event.id, event.room_id, event.sender)

        with Cache.lock:
            del Cache.cache["d_messages"][message.id]

    def on_message_update(self, message: discord.Message) -> None:
        if self.to_return(message):
            return

        with Cache.lock:
            event_id = Cache.cache["d_messages"].get(message.id)

        if not event_id:
            return

        room_id = self.app.get_room_id(self.matrixify(message.channel_id))
        mxid = self.matrixify(message.author.id, user=True)

        # It is possible that a webhook edit's it's own old message
        # after changing it's name, hence we generate a new mxid from
        # the hashed username, but that mxid hasn't been registered before,
        # so the request fails with:
        # M_FORBIDDEN: Application service has not registered this user
        if not self.app.db.fetch_user(mxid):
            return

        content_, emotes = self.process_message(message)

        content = self.app.create_message_event(
            content_, emotes, edit=event_id
        )

        self.app.send_message(room_id, content, mxid)

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
        with Cache.lock:
            webhook = Cache.cache["d_webhooks"].get(channel_id)

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

        with Cache.lock:
            Cache.cache["d_webhooks"][channel_id] = webhook

        return webhook

    def process_message(self, message: discord.Message) -> Tuple[str, Dict]:
        content = message.content
        emotes = {}
        regex = r"<a?:(\w+):(\d+)>"

        # Mentions can either be in the form of `<@1234>` or `<@!1234>`.
        for member in message.mentions:
            for char in ("", "!"):
                content = content.replace(
                    f"<@{char}{member.id}>", f"@{member.username}"
                )

        # Replace channel IDs with names.
        channels = re.findall("<#([0-9]+)>", content)
        if channels:
            if not message.guild_id:
                self.logger.warning(
                    f"Message '{message.id}' in channel '{message.channel_id}' does not have a guild_id!"
                )
            else:
                discord_channels = self.get_channels(message.guild_id)
                for channel in channels:
                    discord_channel = discord_channels.get(channel)
                    name = (
                        discord_channel.name
                        if discord_channel
                        else "deleted-channel"
                    )
                    content = content.replace(f"<#{channel}>", f"#{name}")

        # { "emote_name": "emote_id" }
        for emote in re.findall(regex, content):
            emotes[emote[0]] = emote[1]

        # Replace emote IDs with names.
        content = re.sub(regex, r":\g<1>:", content)

        # Append attachments to message.
        for attachment in message.attachments:
            content += f"\n{attachment['url']}"

        # Append stickers to message.
        for sticker in message.stickers:
            if sticker.format_type != 3:  # 3 == Lottie format.
                content += f"\n{discord.CDN_URL}/stickers/{sticker.id}.png"

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


def excepthook(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    logging.critical(
        "Unknown exception:", exc_info=(exc_type, exc_value, exc_traceback)
    )


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

    sys.excepthook = excepthook

    app = MatrixClient(config, urllib3.PoolManager(maxsize=10))

    # Start the bottle app in a separate thread.
    app_thread = threading.Thread(
        target=app.run, kwargs={"port": int(config["port"])}, daemon=True
    )
    app_thread.start()

    try:
        asyncio.run(app.discord.run())
    except KeyboardInterrupt:
        sys.exit()


if __name__ == "__main__":
    main()

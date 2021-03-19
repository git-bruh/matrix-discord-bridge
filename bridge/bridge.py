import asyncio
import json
import logging
import os
import re
import sys
import traceback
import uuid

import aiofiles
import aiofiles.os
import aiohttp
import discord
import discord.ext.commands
import nio


def config_gen(config_file):
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
        "homeserver": "https://matrix.org",
        "username": "@name:matrix.org",
        "password": "my-secret-password",
        "token": "my-secret-token",
        "discord_cmd_prefix": "my-command-prefix",
        "bridge": {"channel_id": "room_id"},
    }

    if not os.path.exists(config_file):
        with open(config_file, "w") as f:
            json.dump(config_dict, f, indent=4)
            print(f"Example configuration dumped to {config_file}")
            sys.exit()

    with open(config_file, "r") as f:
        config = json.loads(f.read())

    return config


config = config_gen("config.json")
message_store = {}


class MatrixClient(nio.AsyncClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.logger = logging.getLogger("matrix_logger")

        self.listen = False
        self.uploaded_emotes = {}
        self.ready = asyncio.Event()
        self.loop = asyncio.get_event_loop()

        self.start_discord()
        self.add_callbacks()

    def start_discord(self):
        # Intents to fetch members from guild.
        intents = discord.Intents.default()
        intents.members = True

        self.discord_client = DiscordClient(
            self,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False
            ),
            command_prefix=config["discord_cmd_prefix"],
            intents=intents,
        )

        self.bg_task = self.loop.create_task(
            self.discord_client.start(config["token"])
        )

    def add_callbacks(self):
        callbacks = Callbacks(self.discord_client, self)

        self.add_event_callback(
            callbacks.message_callback,
            (nio.RoomMessageText, nio.RoomMessageMedia, nio.RoomMessageEmote),
        )

        self.add_event_callback(
            callbacks.redaction_callback, nio.RedactionEvent
        )

        self.add_ephemeral_callback(
            callbacks.typing_callback, nio.EphemeralEvent
        )

    async def upload_emote(self, emote_id):
        if emote_id in self.uploaded_emotes.keys():
            return self.uploaded_emotes[emote_id]

        emote_url = f"https://cdn.discordapp.com/emojis/{emote_id}"

        emote_file = f"/tmp/{str(uuid.uuid4())}"

        async with aiohttp.ClientSession() as session:
            async with session.get(emote_url) as resp:
                emote = await resp.read()
                content_type = resp.content_type

        async with aiofiles.open(emote_file, "wb") as f:
            await f.write(emote)

        async with aiofiles.open(emote_file, "rb") as f:
            resp, maybe_keys = await self.upload(f, content_type=content_type)

        await aiofiles.os.remove(emote_file)

        if type(resp) != nio.UploadResponse:
            self.logger.warning(f"Failed to upload emote {emote_id}")
            return

        self.uploaded_emotes[emote_id] = resp.content_uri

        return resp.content_uri

    async def get_fmt_body(self, body, emotes):
        replace_ = [
            # Code blocks
            ("```", "<pre><code>", "</code></pre>"),
            # Spoilers
            ("||", "<span data-mx-spoiler>", "</span>"),
            # Strikethrough
            ("~~", "<del>", "</del>"),
        ]

        for replace in replace_:
            for i in range(1, body.count(replace[0]) + 1):
                if i % 2:
                    body = body.replace(replace[0], replace[1], 1)
                else:
                    body = body.replace(replace[0], replace[2], 1)

        for emote in emotes.keys():
            emote_ = await self.upload_emote(emotes[emote])
            if emote_:
                emote = f":{emote}:"
                body = body.replace(
                    emote,
                    f"""<img alt=\"{emote}\" title=\"{emote}\" \
height=\"32\" src=\"{emote_}\" data-mx-emoticon />""",
                )

        return body

    async def message_send(
        self, message, channel_id, emotes, reply_id=None, edit_id=None
    ):
        room_id = config["bridge"][str(channel_id)]

        content = {
            "body": message,
            "format": "org.matrix.custom.html",
            "formatted_body": await self.get_fmt_body(message, emotes),
            "msgtype": "m.text",
        }

        if reply_id:
            reply_event = await self.room_get_event(room_id, reply_id)
            reply_event = reply_event.event

            content = {
                **content,
                "m.relates_to": {"m.in_reply_to": {"event_id": reply_id}},
                "formatted_body": f"""<mx-reply><blockquote>\
<a href="https://matrix.to/#/{room_id}/{reply_id}">In reply to</a>\
<a href="https://matrix.to/#/{reply_event.sender}">{reply_event.sender}</a>\
<br>{reply_event.body}</blockquote></mx-reply>{content["formatted_body"]}""",
            }

        if edit_id:
            content = {
                **content,
                "body": f" * {content['body']}",
                "formatted_body": f" * {content['formatted_body']}",
                "m.relates_to": {"event_id": edit_id, "rel_type": "m.replace"},
                "m.new_content": {**content},
            }

        message = await self.room_send(
            room_id=room_id, message_type="m.room.message", content=content
        )

        return message.event_id

    async def message_redact(self, message, channel_id):
        await self.room_redact(
            room_id=config["bridge"][str(channel_id)], event_id=message
        )

    async def webhook_send(
        self, author, avatar, message, event_id, channel_id, embed=None
    ):
        channel = self.discord_client.channel_store[channel_id]

        hook_name = "matrix_bridge"

        hook = self.discord_client.webhook_cache.get(str(channel.id))

        if not hook:
            hooks = await channel.webhooks()
            hook = discord.utils.get(hooks, name=hook_name)

        if not hook:
            hook = await channel.create_webhook(name=hook_name)

        self.discord_client.webhook_cache[str(channel.id)] = hook

        # Username must be between 1 and 80 characters in length,
        # 'wait=True' allows us to store the sent message.
        try:
            hook = await hook.send(
                username=author[:80],
                avatar_url=avatar,
                content=message,
                embed=embed,
                wait=True,
            )

            message_store[event_id] = hook
            message_store[hook.id] = event_id
        except discord.errors.HTTPException as e:
            self.logger.warning(f"Failed to send message {event_id}: {e}")


class DiscordClient(discord.ext.commands.Bot):
    def __init__(self, matrix_client, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.channel_store = {}

        self.webhook_cache = {}

        self.ready = asyncio.Event()

        self.add_cogs()

        self.matrix_client = matrix_client

    def add_cogs(self):
        cogs_dir = "./cogs"

        if not os.path.isdir(cogs_dir):
            return

        for cog in os.listdir(cogs_dir):
            if cog.endswith(".py"):
                cog = f"cogs.{cog[:-3]}"
                self.load_extension(cog)

    async def to_return(self, channel_id, message=None):
        await self.matrix_client.ready.wait()

        if str(channel_id) not in config["bridge"].keys() or (
            message
            and message.webhook_id
            in [hook.id for hook in self.webhook_cache.values()]
        ):
            return True

    async def on_ready(self):
        for channel in config["bridge"].keys():
            channel_ = self.get_channel(int(channel))
            self.channel_store[channel] = channel_

        self.ready.set()

    async def on_message(self, message):
        # Process other stuff like cogs before ignoring the message.
        await self.process_commands(message)

        if await self.to_return(message.channel.id, message):
            return

        content = await self.process_message(message)

        matrix_message = await self.matrix_client.message_send(
            content[0],
            message.channel.id,
            reply_id=content[1],
            emotes=content[2],
        )

        message_store[message.id] = matrix_message

    async def on_message_edit(self, before, after):
        if await self.to_return(after.channel.id, after):
            return

        content = await self.process_message(after)

        # Edit message only if it can be looked up in the cache.
        if before.id in message_store:
            await self.matrix_client.message_send(
                content[0],
                after.channel.id,
                edit_id=message_store[before.id],
                emotes=content[2],
            )

    async def on_message_delete(self, message):
        # Delete message only if it can be looked up in the cache.
        if message.id in message_store:
            await self.matrix_client.message_redact(
                message_store[message.id], message.channel.id
            )

    async def on_typing(self, channel, user, when):
        if await self.to_return(channel.id) or user == self.user:
            return

        # Send typing event
        await self.matrix_client.room_typing(
            config["bridge"][str(channel.id)], timeout=0
        )

    async def process_message(self, message):
        content = message.clean_content

        regex = r"<a?:(\w+):(\d+)>"
        emotes = {}

        # Store all emotes in a dict to upload and insert into formatted body.
        # { "emote_name": "emote_id" }
        for emote in re.findall(regex, content):
            emotes[emote[0]] = emote[1]

        # Get message reference for replies.
        replied_event = None
        if message.reference:
            replied_message = await message.channel.fetch_message(
                message.reference.message_id
            )
            # Try to get the corresponding event from the message cache.
            try:
                replied_event = message_store[replied_message.id]
            except KeyError:
                pass

        # Replace emote IDs with names.
        content = re.sub(regex, r":\g<1>:", content)

        # Append attachments to message.
        for attachment in message.attachments:
            content += f"\n{attachment.url}"

        content = f"[{message.author.display_name}] {content}"

        return content, replied_event, emotes


class Callbacks(object):
    def __init__(self, discord_client, matrix_client):
        self.discord_client = discord_client
        self.matrix_client = matrix_client

    def get_channel(self, room):
        channel_id = next(
            (
                channel_id
                for channel_id, room_id in config["bridge"].items()
                if room_id == room.room_id
            ),
            None,
        )

        return channel_id

    async def to_return(self, room, event):
        await self.matrix_client.discord_client.ready.wait()

        if (
            room.room_id not in config["bridge"].values()
            or event.sender == self.matrix_client.user
            or not self.matrix_client.listen
        ):
            return True

    async def message_callback(self, room, event):
        message = event.body

        # Ignore messages having an empty body.
        if await self.to_return(room, event) or not message:
            return

        content_dict = event.source.get("content")

        # Get the corresponding Discord channel.
        channel_id = self.get_channel(room)

        author = room.user_name(event.sender)
        avatar = None

        homeserver = event.sender.split(":")[-1]
        url = "https://matrix.org/_matrix/media/r0/download"

        try:
            if content_dict["m.relates_to"]["rel_type"] == "m.replace":
                # Get the original message's event ID.
                edited_event = content_dict["m.relates_to"]["event_id"]
                edited_content = await self.process_message(
                    content_dict["m.new_content"]["body"], channel_id
                )

                # Get the corresponding Discord message.
                webhook_message = message_store[edited_event]

                try:
                    await webhook_message.edit(content=edited_content)
                # Handle exception if edited message was deleted on Discord.
                except (
                    discord.errors.NotFound,
                    discord.errors.HTTPException,
                ) as e:
                    self.matrix_client.logger.warning(
                        f"Failed to edit message {edited_event}: {e}"
                    )

                return
        except KeyError:
            pass

        try:
            if (
                content_dict["m.relates_to"]["m.in_reply_to"]["event_id"]
                in message_store.values()
            ):
                # Remove the first occurance of our bot's username if replying.
                # > <@discordbridge:something.org> [discord user]
                message = message.replace(f"<{config['username']}>", "", 1)
        except KeyError:
            pass

        # _testuser waves_ (Italics)
        if content_dict["msgtype"] == "m.emote":
            message = f"_{author} {message}_"

        message = await self.process_message(message, channel_id)

        embed = None

        # Get attachments.
        try:
            attachment = event.url.split("/")[-1]
            # TODO: Fix URL for attachments forwarded from other rooms.
            attachment = f"{url}/{homeserver}/{attachment}"

            embed = discord.Embed(colour=discord.Colour.blue(), title=message)
            embed.set_image(url=attachment)

            # Send attachment URL in message along with embed,
            # Just in-case the attachment is not an image.
            message = attachment
        except AttributeError:
            pass

        # Get avatar.
        for user in room.users.values():
            if user.user_id == event.sender:
                if user.avatar_url:
                    avatar = user.avatar_url.split("/")[-1]
                    avatar = f"{url}/{homeserver}/{avatar}"
                    break

        await self.matrix_client.webhook_send(
            author, avatar, message, event.event_id, channel_id, embed=embed
        )

    async def redaction_callback(self, room, event):
        if await self.to_return(room, event):
            return

        # Try to fetch the message from cache.
        try:
            message = message_store[event.redacts]
            await message.delete()
        # Handle exception if message was already deleted on Discord.
        except discord.errors.NotFound as e:
            self.matrix_client.logger.warning(
                f"Failed to delete message {event.event_id}: {e}"
            )
        except KeyError:
            pass

    async def typing_callback(self, room, event):
        if (
            len(room.typing_users) == 1
            and self.matrix_client.user in room.typing_users
        ) or room.room_id not in config["bridge"].values():
            return

        # Get the corresponding Discord channel.
        channel_id = self.get_channel(room)

        # Send typing event.
        async with self.discord_client.channel_store[channel_id].typing():
            return

    async def process_message(self, message, channel_id):
        mentions = re.findall(r"(^|\s)(@(\w*))", message)
        emotes = re.findall(r":(\w*):", message)

        # Get the guild from channel ID.
        guild = self.discord_client.channel_store[channel_id].guild

        added_emotes = []
        for emote in emotes:
            # Don't replace emote names with IDs multiple times.
            # :emote: becomes <:emote:emote_id>
            if emote not in added_emotes:
                added_emotes.append(emote)
                emote_ = discord.utils.get(guild.emojis, name=emote)
                if emote_:
                    message = message.replace(f":{emote}:", str(emote_))

        # mentions = [('', '@name', 'name'), (' ', '@', '')]
        for mention in mentions:
            # Don't fetch member if mention is empty.
            # Single "@" without any name.
            if mention[2]:
                member = await guild.query_members(query=mention[2])
                if member:
                    # Get first result.
                    message = message.replace(mention[1], member[0].mention)

        return message


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s:%(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(f"{basedir}/bridge.log"),
            logging.StreamHandler(),
        ],
    )

    retry = 2

    matrix_client = MatrixClient(config["homeserver"], config["username"])

    while True:
        resp = await matrix_client.login(config["password"])

        if type(resp) == nio.LoginError:
            matrix_client.logger.error(f"Failed to login: {resp}")
            return False

        # Login successful.
        matrix_client.logger.info(resp)

        try:
            await matrix_client.sync(full_state=True)
        except Exception:
            matrix_client.logger.exception("Initial sync failed!")
            return False

        try:
            matrix_client.ready.set()
            matrix_client.listen = True

            matrix_client.logger.info("Clients ready!")

            await matrix_client.sync_forever(timeout=30000, full_state=True)
        except Exception:
            matrix_client.logger.error(
                f"Unknown exception occured\n{traceback.format_exc()}\n"
                f"Retrying in {retry} seconds..."
            )

            # Clear "ready" status.
            matrix_client.ready.clear()

            await matrix_client.close()
            await asyncio.sleep(retry)

            matrix_client.listen = False
        finally:
            if matrix_client.listen:
                await matrix_client.close()
                return False


if __name__ == "__main__":
    asyncio.run(main())

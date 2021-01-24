import json
import logging
import os
import re
import sys
import uuid
import aiofiles
import aiofiles.os
import aiohttp
import discord
import discord.ext.commands
import nio


def config_gen(config_file):
    config_dict = {
        "homeserver": "https://matrix.org",
        "username": "@name:matrix.org",
        "password": "my-secret-password",
        "token": "my-secret-token",
        "discord_prefix": "my-command-prefix",
        "bridge": {"channel_id": "room_id"}
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

message_store, channel_store = {}, {}


class MatrixClient(nio.AsyncClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.logger = logging.getLogger("matrix_logger")
        self.uploaded_emotes = {}

    async def start(self, discord_client):
        password = config["password"]
        timeout = 30000

        self.logger.info(await self.login(password))

        self.logger.info("Doing initial sync.")
        await self.sync(timeout)

        # Set up event callbacks after syncing once to ignore old messages.
        callbacks = Callbacks(self)

        self.add_event_callback(
            callbacks.message_callback,
            (nio.RoomMessageText, nio.RoomMessageMedia,
             nio.RoomMessageEmote)
        )

        self.add_event_callback(
            callbacks.redaction_callback, nio.RedactionEvent
        )

        self.add_ephemeral_callback(
            callbacks.typing_callback, nio.EphemeralEvent
        )

        # Wait for Discord client...
        await discord_client.wait_until_ready()

        self.logger.info("Syncing forever.")
        await self.sync_forever(timeout=timeout)

        # Logout
        await self.logout()
        await self.close()

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
            resp, maybe_keys = await self.upload(
                f, content_type=content_type
            )

        await aiofiles.os.remove(emote_file)

        if type(resp) != nio.UploadResponse:
            self.logger.warning(
                f"Failed to upload emote {emote_id}"
            )
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
                ("~~", "<del>", "</del>")
            ]

        for replace in replace_:
            for i in range(body.count(replace[0])):
                i += 1

                if i % 2:
                    body = body.replace(replace[0], replace[1], 1)
                else:
                    body = body.replace(replace[0], replace[2], 1)

        for emote in emotes.keys():
            emote_ = await self.upload_emote(emotes[emote])
            if emote_:
                emote = f":{emote}:"
                body = body.replace(
                    emote, f"""<img alt=\"{emote}\" title=\"{emote}\" \
height=\"32\" src=\"{emote_}\" data-mx-emoticon />"""
                )

        return body

    async def message_send(self, message, channel_id, emotes,
                           reply_id=None, edit_id=None):
        room_id = config["bridge"][str(channel_id)]

        content = {
            "body": message,
            "format": "org.matrix.custom.html",
            "formatted_body": await self.get_fmt_body(message, emotes),
            "msgtype": "m.text"
        }

        if reply_id:
            reply_event = await self.room_get_event(
                room_id, reply_id
            )
            reply_event = reply_event.event

            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_id}}

            content["formatted_body"] = f"""<mx-reply><blockquote>\
<a href="https://matrix.to/#/{room_id}/{reply_id}">In reply to</a>\
<a href="https://matrix.to/#/{reply_event.sender}">{reply_event.sender}</a>\
<br>{reply_event.body}</blockquote></mx-reply>{content["formatted_body"]}"""

        if edit_id:
            content["body"] = f" * {content['body']}"

            content["m.relates_to"] = {
                "event_id": edit_id, "rel_type": "m.replace"
            }

            content["m.new_content"] = {
                    "body": content["body"],
                    "formatted_body": content["formatted_body"],
                    "format": content["format"],
                    "msgtype": content["msgtype"]
            }

        message = await self.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content
        )

        return message.event_id

    async def message_redact(self, message, channel_id):
        await self.room_redact(
            room_id=config["bridge"][str(channel_id)],
            event_id=message
        )

    async def webhook_send(self, author, avatar, message,
                           event_id, channel_id, embed=None):
        channel = channel_store[channel_id]

        hook_name = "matrix_bridge"

        hooks = await channel.webhooks()

        # Create webhook if it doesn't exist.
        hook = discord.utils.get(hooks, name=hook_name)
        if not hook:
            hook = await channel.create_webhook(name=hook_name)

        # Username must be between 1 and 80 characters in length,
        # 'wait=True' allows us to store the sent message.
        try:
            hook = await hook.send(
                username=author[:80], avatar_url=avatar,
                content=message, embed=embed, wait=True
            )

            message_store[event_id] = hook
            message_store[hook.id] = event_id
        except discord.errors.HTTPException as e:
            self.logger.warning(f"Failed to send message {event_id}: {e}")


class DiscordClient(discord.ext.commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.matrix_client = MatrixClient(
            config["homeserver"], config["username"]
        )

        self.bg_task = self.loop.create_task(
            self.log_exceptions(self.matrix_client)
        )

        self.add_cogs()

    def add_cogs(self):
        cogs_dir = "./cogs"

        if not os.path.isdir(cogs_dir):
            return

        for cog in os.listdir(cogs_dir):
            if cog.endswith(".py"):
                cog = f"cogs.{cog[:-3]}"
                self.load_extension(cog)

    def to_return(self, channel_id, user):
        if user.discriminator == "0000" \
                or str(channel_id) not in config["bridge"].keys():
            return True

    async def log_exceptions(self, matrix_client):
        try:
            return await matrix_client.start(self)
        except Exception as e:
            matrix_client.logger.warning(f"Unknown exception occurred: {e}")

        await matrix_client.close()

    async def on_ready(self):
        for channel in config["bridge"].keys():
            channel_store[channel] = self.get_channel(int(channel))

    async def on_message(self, message):
        # Process other stuff like cogs before ignoring the message.
        await self.process_commands(message)

        if self.to_return(message.channel.id, message.author):
            return

        content = await self.process_message(message)

        matrix_message = await self.matrix_client.message_send(
            content[0], message.channel.id,
            reply_id=content[1], emotes=content[2]
        )

        message_store[message.id] = matrix_message

    async def on_message_edit(self, before, after):
        if self.to_return(after.channel.id, after.author):
            return

        content = await self.process_message(after)

        # Edit message only if it can be looked up in the cache.
        if before.id in message_store:
            await self.matrix_client.message_send(
                content[0], after.channel.id,
                edit_id=message_store[before.id], emotes=content[2]
            )

    async def on_message_delete(self, message):
        # Delete message only if it can be looked up in the cache.
        if message.id in message_store:
            await self.matrix_client.message_redact(
                message_store[message.id], message.channel.id
            )

    async def on_typing(self, channel, user, when):
        if self.to_return(channel.id, user) or user == self.user:
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

        # Escape stuff
        for replace in ("<", ">"):
            content = content.replace(replace, f"\\{replace}")

        # Append attachments to message.
        for attachment in message.attachments:
            content += f"\n{attachment.url}"

        content = f"[{message.author.display_name}] {content}"

        return content, replied_event, emotes


class Callbacks(object):
    def __init__(self, matrix_client):
        self.matrix_client = matrix_client

    def get_channel(self, room):
        channel_id = next(
            (channel_id for channel_id, room_id in config["bridge"].items()
                if room_id == room.room_id), None
        )

        return channel_id

    def to_return(self, room, event):
        if room.room_id not in config["bridge"].values() or \
                event.sender == self.matrix_client.user:
            return True

    async def message_callback(self, room, event):
        message = event.body

        # Ignore messages having an empty body.
        if self.to_return(room, event) or not message:
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
                    discord.errors.NotFound, discord.errors.HTTPException
                ) as e:
                    self.matrix_client.logger.warning(
                        f"Failed to edit message {edited_event}: {e}"
                    )

                return
        except KeyError:
            pass

        try:
            if content_dict["m.relates_to"]["m.in_reply_to"]["event_id"] in \
                    message_store.values():
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
        if self.to_return(room, event):
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
        if not room.typing_users \
                or room.room_id not in config["bridge"].values():
            return

        # Return if the event is sent by our bot.
        if len(room.typing_users) == 1 and \
                self.matrix_client.user in room.typing_users:
            return

        # Get the corresponding Discord channel.
        channel_id = self.get_channel(room)

        # Send typing event.
        async with channel_store[channel_id].typing():
            return

    async def process_message(self, message, channel_id):
        mentions = re.findall(r"(^|\s)(@(\w*))", message)
        emotes = re.findall(r":(\w*):", message)

        # Get the guild from channel ID.
        guild = channel_store[channel_id].guild

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


def main():
    logging.basicConfig(level=logging.INFO)

    # Disable everyone and role mentions.
    allowed_mentions = discord.AllowedMentions(everyone=False, roles=False)
    # Set command prefix for Discord bot.
    command_prefix = config["discord_prefix"]
    # Intents to fetch members from guild.
    intents = discord.Intents.default()
    intents.members = True

    # Start Discord bot.
    DiscordClient(
        allowed_mentions=allowed_mentions,
        command_prefix=command_prefix, intents=intents
    ).run(config["token"])


if __name__ == "__main__":
    main()

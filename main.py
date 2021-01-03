import discord.ext.commands
import discord
import json
import logging
import nio
import os
import re


def config_gen(config_file):
    config_dict = {
        "homeserver": "https://matrix.org",
        "username": "@name:matrix.org",
        "password": "my-secret-password",
        "token": "my-secret-token",
        "discord_prefix": "my-command-prefix",
        "bridge": {"channel_id": "room_id", }
    }

    if not os.path.exists(config_file):
        with open(config_file, "w") as f:
            json.dump(config_dict, f, indent=4)
            print(f"Example configuration dumped to {config_file}")
            exit()

    with open(config_file, "r") as f:
        config = json.loads(f.read())

    return config


config = config_gen("config.json")

message_store, channel_store = {}, {}


class MatrixClient(nio.AsyncClient):
    async def start(self, discord_client):
        self.logger = logging.getLogger("matrix_logger")

        password = config["password"]
        timeout = 30000

        self.logger.info(await self.login(password))

        self.logger.info("Doing initial sync.")
        await self.sync(timeout)

        # Set up event callbacks
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

        await discord_client.wait_until_ready()

        self.logger.info("Syncing forever.")
        await self.sync_forever(timeout=timeout)

        await self.close()

    async def message_send(self, message, channel_id,
                           reply_id=None, edit_id=None):
        room_id = config["bridge"][str(channel_id)]

        content = {
            "msgtype": "m.text",
            "body": message,
        }

        if reply_id:
            reply_event = await self.room_get_event(
                room_id, reply_id
            )
            reply_event = reply_event.event

            content["m.relates_to"] = {
                "m.in_reply_to": {"event_id": reply_id},
            }

            content["format"] = "org.matrix.custom.html"

            content["formatted_body"] = f"""<mx-reply><blockquote>
<a href="https://matrix.to/#/{room_id}/{reply_id}">In reply to</a>
<a href="https://matrix.to/#/{reply_event.sender}">{reply_event.sender}</a><br>
{reply_event.body}</blockquote></mx-reply>{message}"""

        if edit_id:
            content["body"] = f" * {message}"

            content["m.new_content"] = {
                    "body": message,
                    "msgtype": "m.text"
            }

            content["m.relates_to"] = {
                    "event_id": edit_id,
                    "rel_type": "m.replace",
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
                           event_id, channel_id):
        channel = channel_store[channel_id]

        # Create webhook if it doesn't exist
        hook_name = "matrix_bridge"
        hooks = await channel.webhooks()
        hook = discord.utils.get(hooks, name=hook_name)
        if not hook:
            hook = await channel.create_webhook(name=hook_name)

        # Username must be between 1 and 80 characters in length
        # 'wait=True' allows us to store the sent message
        try:
            hook = await hook.send(username=author[:80], avatar_url=avatar,
                                   content=message, wait=True)
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

        self.bg_task = self.loop.create_task(self.matrix_client.start(self))

        self.add_cogs()

    def add_cogs(self):
        for cog in os.listdir("./cogs"):
            if cog.endswith(".py"):
                cog = f"cogs.{cog[:-3]}"
                self.load_extension(cog)

    def to_return(self, channel_id, user):
        if user.discriminator == "0000" \
                or str(channel_id) not in config["bridge"].keys():
            return True

    async def on_ready(self):
        for channel in config["bridge"].keys():
            channel_store[channel] = self.get_channel(int(channel))

    async def on_message(self, message):
        await self.process_commands(message)

        if self.to_return(message.channel.id, message.author):
            return

        content = await self.process_message(message)

        matrix_message = await self.matrix_client.message_send(
            content[0], message.channel.id, reply_id=content[1]
        )

        message_store[message.id] = matrix_message

    async def on_message_edit(self, before, after):
        if self.to_return(after.channel.id, after.author):
            return

        content = await self.process_message(after)

        if before.id in message_store:
            await self.matrix_client.message_send(
                content[0], after.channel.id, edit_id=message_store[before.id]
            )

    async def on_message_delete(self, message):
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

        replied_event = None
        if message.reference:
            replied_message = await message.channel.fetch_message(
                message.reference.message_id
            )
            try:
                replied_event = message_store[replied_message.id]
            except KeyError:
                pass

        # Replace emote IDs with names
        content = re.sub(r"<a?(:\w+:)\d*>", r"\g<1>", content)

        # Append attachments to message
        for attachment in message.attachments:
            content += f"\n{attachment.url}"

        content = f"[{message.author.name}] {content}"

        return content, replied_event


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

        if self.to_return(room, event) or not message:
            return

        content_dict = event.source.get("content")

        channel_id = self.get_channel(room)

        author = event.sender.split(":")[0][1:]
        avatar = None

        homeserver = event.sender.split(":")[-1]
        url = "https://matrix.org/_matrix/media/r0/download"

        try:
            if content_dict["m.relates_to"]["rel_type"] == "m.replace":
                edited_event = content_dict["m.relates_to"]["event_id"]
                edited_content = await self.process_message(
                    content_dict["m.new_content"]["body"], channel_id
                )
                webhook_message = message_store[edited_event]

                try:
                    await webhook_message.edit(content=edited_content)
                except discord.errors.NotFound as e:
                    self.matrix_client.logger.warning(
                        f"Failed to edit message {edited_event}: {e}"
                    )

                return
        except KeyError:
            pass

        try:
            if content_dict["m.relates_to"]["m.in_reply_to"]["event_id"] in \
                    message_store.values():
                message = message.replace(f"<{config['username']}>", "", 1)
        except KeyError:
            pass

        if content_dict["msgtype"] == "m.emote":
            message = f"_{author} {message}_"

        message = await self.process_message(message, channel_id)

        # Get attachments
        try:
            attachment = event.url.split("/")[-1]

            # Highlight attachment name
            message = f"`{message}`"

            message += f"\n{url}/{homeserver}/{attachment}"
        except AttributeError:
            pass

        # Get avatar
        for user in room.users.values():
            if user.user_id == event.sender:
                if user.avatar_url:
                    avatar = user.avatar_url.split("/")[-1]
                    avatar = f"{url}/{homeserver}/{avatar}"
                    break

        await self.matrix_client.webhook_send(
            author, avatar, message, event.event_id, channel_id
        )

    async def redaction_callback(self, room, event):
        if self.to_return(room, event):
            return

        # Redact webhook message
        try:
            message = message_store[event.redacts]
            await message.delete()
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

        if len(room.typing_users) == 1 and \
                self.matrix_client.user in room.typing_users:
            return

        channel_id = self.get_channel(room)

        async with channel_store[channel_id].typing():
            return

    async def process_message(self, message, channel_id):
        mentions = re.findall(r"(^|\s)(@(\w*))", message)
        emotes = re.findall(r":(\w*):", message)

        guild = channel_store[channel_id].guild

        added_emotes = []
        for emote in emotes:
            if emote not in added_emotes:
                added_emotes.append(emote)
                emote_ = discord.utils.get(guild.emojis, name=emote)
                if emote_:
                    message = message.replace(f":{emote}:", str(emote_))

        for mention in mentions:
            if mention[2] != "":
                member = await guild.query_members(query=mention[2])
                if member:
                    message = message.replace(mention[1], member[0].mention)

        return message


def main():
    logging.basicConfig(level=logging.INFO)

    allowed_mentions = discord.AllowedMentions(everyone=False, roles=False)
    command_prefix = config["discord_prefix"]
    intents = discord.Intents.default()
    intents.members = True

    DiscordClient(
        allowed_mentions=allowed_mentions,
        command_prefix=command_prefix, intents=intents
    ).run(config["token"])


if __name__ == "__main__":
    main()

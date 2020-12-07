import discord
import json
import logging
import nio
import os
import re


def config_gen(config_file):
    config_dict = {
        "homeserver": "https://matrix.org",
        "room_id": "room:matrix.org",
        "username": "@name:matrix.org",
        "password": "my-secret-password",
        "channel_id": "channel",
        "token": "my-secret-token"
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

logging.basicConfig(level=logging.INFO)
matrix_logger = logging.getLogger("matrix_logger")

message_store = {}


class MatrixClient(nio.AsyncClient):
    async def create(self, discord_client):
        password = config["password"]
        timeout = 30000

        matrix_logger.info(await self.login(password))

        matrix_logger.info("Doing initial sync.")
        await self.sync(timeout)

        # Set up event callbacks
        callbacks = Callbacks(self, self.process_message)
        self.add_event_callback(
            callbacks.message_callback,
            (nio.RoomMessageText, nio.RoomMessageMedia,
             nio.RoomMessageEmote))

        self.add_event_callback(
            callbacks.redaction_callback, nio.RedactionEvent)

        self.add_ephemeral_callback(
            callbacks.typing_callback, nio.EphemeralEvent)

        await discord_client.wait_until_ready()

        matrix_logger.info("Syncing forever.")
        await self.sync_forever(timeout=timeout)

        await self.close()

    async def message_send(self, message, reply_id=None, edit_id=None):
        content = {
            "msgtype": "m.text",
            "body": message,
        }

        if reply_id:
            reply_event = await self.room_get_event(
                    config["room_id"], reply_id
            )

            reply_event = reply_event.event.source["content"]["body"]

            content["m.relates_to"] = {
                "m.in_reply_to": {"event_id": reply_id},
            }

            content["format"] = "org.matrix.custom.html"

            content["formatted_body"] = f"""<mx-reply><blockquote>
<a href="https://matrix.to/#/{config["room_id"]}/{reply_id}">In reply to</a>
<a href="https://matrix.to/#/{config["username"]}">{config["username"]}</a><br>
{reply_event}</blockquote></mx-reply>{message}"""

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
            room_id=config["room_id"],
            message_type="m.room.message",
            content=content
        )

        return message.event_id

    async def message_redact(self, message):
        await self.room_redact(
            room_id=config["room_id"],
            event_id=message
        )

    async def webhook_send(self, author, avatar, message, event_id):
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
        except discord.errors.HTTPException as e:
            matrix_logger.warning(f"Failed to send message {event_id}: {e}")

    async def process_message(self, message):
        mentions = re.findall(r"(^|\s)(@(\w*))", message)
        emotes = re.findall(r":(.*?):", message)

        guild = channel.guild

        for emote in emotes:
            emote_ = discord.utils.get(guild.emojis, name=emote)
            if emote_:
                message = message.replace(f":{emote}:", str(emote_))

        for mention in mentions:
            if mention[2] != "":
                member = await guild.query_members(query=mention[2])
                if member:
                    message = message.replace(mention[1], member[0].mention)

        return message


class DiscordClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.matrix_client = MatrixClient(
            config["homeserver"], config["username"])

        self.bg_task = self.loop.create_task(self.matrix_client.create(self))

    async def on_ready(self):
        print(f"Logged in as {self.user}")

        global channel
        channel = int(config["channel_id"])
        channel = self.get_channel(channel)

    async def on_message(self, message):
        if message.author.bot or str(message.channel.id) != \
                config["channel_id"]:
            return

        content = await self.process_message(message)

        matrix_message = await self.matrix_client.message_send(
            content[0], content[1])

        message_store[message.id] = matrix_message

    async def on_message_edit(self, before, after):
        if after.author.bot or str(after.channel.id) != \
                config["channel_id"]:
            return

        content = await self.process_message(after)

        await self.matrix_client.message_send(
            content[0], edit_id=message_store[before.id])

    async def on_message_delete(self, message):
        if message.id in message_store:
            await self.matrix_client.message_redact(message_store[message.id])

    async def on_typing(self, channel, user, when):
        if user.bot or str(channel.id) != config["channel_id"]:
            return

        # Send typing event
        await self.matrix_client.room_typing(config["room_id"], timeout=0)

    async def process_message(self, message):
        content = message.clean_content

        replied_event = None
        if message.reference:
            replied_message = await message.channel.fetch_message(
                message.reference.message_id)
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
    def __init__(self, client, process_message):
        self.client = client
        self.process_message = process_message

    async def message_callback(self, room, event):
        # Ignore messages from ourselves or other rooms
        if room.room_id != config["room_id"] or \
                event.sender == self.client.user:
            return

        message = event.body

        if not message:
            return

        content_dict = event.source.get("content")

        try:
            if content_dict["m.relates_to"]["m.in_reply_to"]["event_id"] in \
                    message_store.values():
                message = message.replace(f"<{config['username']}>", "", 1)
        except KeyError:
            pass

        author = event.sender[1:]
        avatar = None

        homeserver = author.split(":")[-1]
        url = "https://matrix.org/_matrix/media/r0/download"

        message = await self.process_message(message)

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

        await self.client.webhook_send(
            author, avatar, message, event.event_id)

    async def redaction_callback(self, room, event):
        # Ignore messages from ourselves or other rooms
        if room.room_id != config["room_id"] or \
                event.sender == self.client.user:
            return

        # Redact webhook message
        try:
            message = message_store[event.redacts]
            await message.delete()
        except KeyError:
            pass

    async def typing_callback(self, room, event):
        # Ignore events from other rooms
        if room.room_id != config["room_id"]:
            return

        if room.typing_users:
            # Ignore events from ourselves
            if len(room.typing_users) == 1 \
                    and room.typing_users[0] == self.client.user:
                return

            # Send typing event
            async with channel.typing():
                pass


def main():
    intents = discord.Intents.default()
    intents.members = True

    allowed_mentions = discord.AllowedMentions(everyone=False, roles=False)

    DiscordClient(intents=intents, allowed_mentions=allowed_mentions).run(
        config["token"])


if __name__ == "__main__":
    main()

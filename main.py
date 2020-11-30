import asyncio
import discord
import re
import json
import logging
from nio import (
    AsyncClient,
    RoomMessageText,
    RoomMessageMedia,
    RedactionEvent,
    EphemeralEvent
)
import os


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


class MatrixClient(object):
    async def create(self):
        homeserver = config["homeserver"]
        username = config["username"]
        password = config["password"]

        global matrix_client

        matrix_client = AsyncClient(homeserver, username)

        matrix_logger.info(await matrix_client.login(password))

        # Sync once to avoid acting on old messages
        matrix_logger.info("Doing initial sync.")
        await matrix_client.sync(30000)

        # Set up event callbacks
        callbacks = Callbacks()
        matrix_client.add_event_callback(
            callbacks.message_callback,
            (RoomMessageText, RoomMessageMedia))

        matrix_client.add_event_callback(
            callbacks.redaction_callback, RedactionEvent)

        matrix_client.add_ephemeral_callback(
            callbacks.typing_callback, EphemeralEvent)

    async def message_send(self, message, reply_id=None, edit_id=None):
        content = {
            "msgtype": "m.text",
            "body": message,
        }

        if reply_id:
            reply_event = await matrix_client.room_get_event(
                    config["room_id"], reply_id
            )

            reply_event = reply_event.event.source["content"]["body"]

            content["m.relates_to"] = {
                "m.in_reply_to": {"event_id": reply_id},
            }

            content["format"] = "org.matrix.custom.html"

            content["formatted_body"] = (f"""<mx-reply><blockquote>
<a href="https://matrix.to/#/{config["room_id"]}/{reply_id}">In reply to</a>
<a href="https://matrix.to/#/{config["username"]}">{config["username"]}</a><br>
{reply_event}</blockquote></mx-reply>{message}
""")

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

        message = await matrix_client.room_send(
            room_id=config["room_id"],
            message_type="m.room.message",
            content=content
        )

        return message.event_id

    async def message_redact(self, message):
        await matrix_client.room_redact(
            room_id=config["room_id"],
            event_id=message
        )


class DiscordClient(discord.Client):
    async def on_ready(self):
        print(f"Logged in as {self.user}")

        global channel
        channel = int(config["channel_id"])
        channel = self.get_channel(channel)
        matrix_logger.info("Syncing forever.")
        await matrix_client.sync_forever(timeout=30000)

    async def on_message(self, message):
        if message.author.bot or str(message.channel.id) != \
                config["channel_id"]:
            return

        content = await Process().discord(message)

        matrix_message = await MatrixClient().message_send(
            content[0], content[1])

        message_store[message.id] = matrix_message

    async def on_message_edit(self, before, after):
        if after.author.bot or str(after.channel.id) != \
                config["channel_id"]:
            return

        content = await Process().discord(after)

        await MatrixClient().message_send(
            content[0], edit_id=message_store[before.id])

    async def on_message_delete(self, message):
        if message.id in message_store:
            await MatrixClient().message_redact(message_store[message.id])

    async def on_typing(self, channel, user, when):
        if user.bot or str(channel.id) != config["channel_id"]:
            return

        # Send typing event
        await matrix_client.room_typing(config["room_id"], timeout=0)

    async def webhook_send(self, author, avatar, message, event_id):
        # Create webhook if it doesn't exist
        hook_name = "matrix_bridge"
        hooks = await channel.webhooks()
        hook = discord.utils.get(hooks, name=hook_name)
        if not hook:
            hook = await channel.create_webhook(name=hook_name)

        # 'wait=True' allows us to store the sent message
        try:
            hook = await hook.send(username=author, avatar_url=avatar,
                                   content=message, wait=True)
            message_store[event_id] = hook
        except discord.errors.HTTPException as e:
            matrix_logger.warning(f"Failed to send message {event_id}: {e}")


class Callbacks(object):
    async def message_callback(self, room, event):
        # Don't act on activities in other rooms
        if room.room_id != config["room_id"]:
            return

        # https://github.com/Rapptz/discord.py/issues/6058
        # content_dict = event.source.get("content")
        # try:
        #     if content_dict["m.relates_to"]["rel_type"] == "m.replace":
        #         edited_event = content_dict["m.relates_to"]["event_id"]
        #         edited_content = content_dict["m.new_content"]["body"]
        #         webhook_message = message_cache[edited_event]
        #         await something_edit_webhook(webhook_message, edited_content)
        #         return
        # except KeyError:
        #     pass

        message = event.body

        if not message:
            return

        # Don't act on ourselves
        if event.sender == matrix_client.user:
            return

        author = event.sender[1:]
        avatar = None

        homeserver = author.split(":")[-1]
        url = "https://matrix.org/_matrix/media/r0/download"

        message = await Process().matrix(message)

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

        await DiscordClient().webhook_send(
            author, avatar, message, event.event_id)

    async def redaction_callback(self, room, event):
        # Don't act on activities in other rooms
        if room.room_id != config["room_id"]:
            return

        # Don't act on ourselves
        if event.sender == matrix_client.user:
            return

        # Redact webhook message
        try:
            message = message_store[event.redacts]
            await message.delete()
        except KeyError:
            pass

    async def typing_callback(self, room, event):
        # Don't act on activities in other rooms
        if room.room_id != config["room_id"]:
            return

        if room.typing_users:
            # Don't act on ourselves
            if len(room.typing_users) == 1 \
                    and room.typing_users[0] == matrix_client.user:
                return

            # Send typing event
            async with channel.typing():
                pass


class Process(object):
    async def discord(self, message):
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

        content = f"<{message.author.name}> {content}"

        return content, replied_event

    async def matrix(self, message):
        message = message.replace("@everyone", "@\u200Beveryone")
        message = message.replace("@here", "@\u200Bhere")

        mentions = re.findall(r"(^|\s)(@(\w*))", message)
        emotes = re.findall(r":(.*?):", message)

        guild = channel.guild

        for emote in emotes:
            emote_ = discord.utils.get(guild.emojis, name=emote)
            if emote_:
                message = message.replace(f":{emote}:", str(emote_))

        for mention in mentions:
            member = await guild.query_members(query=mention[2])
            if member:
                message = message.replace(mention[1], member[0].mention)

        return message


async def main():
    intents = discord.Intents.default()
    intents.members = True

    await MatrixClient().create()
    await DiscordClient(intents=intents).start(config["token"])

if __name__ == "__main__":
    asyncio.run(main())

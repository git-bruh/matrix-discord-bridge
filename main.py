import discord
import json
import logging
import nio
import re
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

intents = discord.Intents.default()
intents.members = True
discord_client = discord.Client(intents=intents)
logging.basicConfig(level=logging.INFO)

message_cache = {}


@discord_client.event
async def on_ready():
    print(f"Logged in as {discord_client.user}")

    # Start Matrix bot
    await create_matrix_client()


@discord_client.event
async def on_message(message):
    if message.author.bot or str(message.channel.id) != config["channel_id"]:
        return

    content = await process_discord(message)

    matrix_message = await message_send(content)
    message_cache[message.id] = matrix_message


@discord_client.event
async def on_message_edit(before, after):
    if after.author.bot or str(after.channel.id) != config["channel_id"]:
        return

    content = await process_discord(after) + " (edited)"

    await message_redact(message_cache[before.id], "Message edited")

    matrix_message = await message_send(content)
    message_cache[after.id] = matrix_message


@discord_client.event
async def on_message_delete(message):
    if message.id in message_cache:
        await message_redact(message_cache[message.id], "Message deleted")


@discord_client.event
async def on_typing(channel, user, when):
    if user.bot or str(channel.id) != config["channel_id"]:
        return

    # Send typing event
    await matrix_client.room_typing(config["room_id"], timeout=0)


async def get_channel():
    channel = int(config["channel_id"])
    channel = discord_client.get_channel(channel)

    return channel


async def process_discord(message):
    content = message.clean_content

    # Replace emote IDs with names
    content = re.sub(r"<a?(:\w+:)\d*>", r"\g<1>", content)

    # Append attachments to message
    for attachment in message.attachments:
        content += f"\n{attachment.url}"

    content = f"<{message.author.name}> {content}"

    return content


async def process_matrix(message):
    # Don't mention @everyone or @here
    message = message.replace("@everyone", "@\u200Beveryone")
    message = message.replace("@here", "@\u200Bhere")

    mentions = re.findall(r"(^|\s)(@(\w*))", message)

    channel = await get_channel()
    guild = channel.guild

    for emote in message.split():
        if emote[0] == emote[-1] == ":":
            emote_ = discord.utils.get(guild.emojis, name=emote[1:-1])
            if emote_:
                message = message.replace(emote, str(emote_))

    for mention in mentions:
        member =  await guild.query_members(query=mention[2])
        if member:
            message = message.replace(mention[1], member[0].mention)

    return message


async def webhook_send(author, avatar, message, event_id):
    channel = await get_channel()

    # Create webhook if it doesn't exist
    hook_name = "matrix_bridge"
    hooks = await channel.webhooks()
    hook = discord.utils.get(hooks, name=hook_name)
    if not hook:
        hook = await channel.create_webhook(name=hook_name)

    # 'wait=True' allows us to store the sent message
    hook = await hook.send(username=author, avatar_url=avatar, content=message,
                           wait=True)

    message_cache[event_id] = hook


async def create_matrix_client():
    homeserver = config["homeserver"]
    username = config["username"]
    password = config["password"]

    timeout = 30000

    global matrix_client

    matrix_client = nio.AsyncClient(homeserver, username)
    print(await matrix_client.login(password))

    # Sync once before adding callback to avoid acting on old messages
    await matrix_client.sync(timeout)

    matrix_client.add_event_callback(message_callback, (nio.RoomMessageText,
                                                        nio.RoomMessageMedia))

    matrix_client.add_event_callback(redaction_callback, nio.RedactionEvent)

    matrix_client.add_ephemeral_callback(typing_callback, nio.EphemeralEvent)

    # Sync forever
    await matrix_client.sync_forever(timeout=timeout)

    await matrix_client.logout()
    await matrix_client.close()


async def message_send(message):
    message = await matrix_client.room_send(
        room_id=config["room_id"],
        message_type="m.room.message",
        content={
            "msgtype": "m.text",
            "body": message
        }
    )

    return message.event_id


async def message_redact(message, reason):
    await matrix_client.room_redact(
        room_id=config["room_id"],
        event_id=message,
        reason=reason
    )


async def message_callback(room, event):
    # Don't act on activities in other rooms
    if room.room_id != config["room_id"]:
        return

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

    # Replace Discord mentions and emotes with IDs
    message = await process_matrix(message)

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

    await webhook_send(author, avatar, message, event.event_id)


async def redaction_callback(room, event):
    # Don't act on activities in other rooms
    if room.room_id != config["room_id"]:
        return

    # Don't act on ourselves
    if event.sender == matrix_client.user:
        return

    # Redact webhook message
    try:
        message = message_cache[event.redacts]
        await message.delete()
    except KeyError:
        pass


async def typing_callback(room, event):
    channel = await get_channel()

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


def main():
    # Start Discord bot
    discord_client.run(config["token"])


if __name__ == "__main__":
    main()

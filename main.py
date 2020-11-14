import logging
import json
import os
import nio
import discord


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


@discord_client.event
async def on_ready():
    print(f"Logged in as {discord_client.user}")

    global channel_
    channel_ = int(config["channel_id"])
    channel_ = discord_client.get_channel(channel_)

    # Start Matrix bot
    await create_matrix_client()


@discord_client.event
async def on_message(message):
    # Don't respond to bots/webhooks
    if message.author.bot:
        return

    # Replace mention/emote IDs with names
    content = await process(message.content, "emote_")
    content = await process(content, "mention")

    content = f"<{message.author.name}> {content}"

    # Append attachments to message
    for attachment in message.attachments:
        content += f"\n{attachment.url}"

    if str(message.channel.id) == config["channel_id"]:
        await message_send(content)


@discord_client.event
async def on_typing(channel, user, when):
    # Don't act on bots
    if user.bot:
        return

    if channel == channel_:
        # Send typing event
        await matrix_client.room_typing(config["room_id"], timeout=0)


async def process(message, category):
    # Replace emote names with emote IDs (Matrix -> Discord)
    if category == "emote":
        start = end = ":"
        start_ = 1

    # Replace emote IDs with emote names (Discord -> Matrix)
    elif category == "emote_":
        start = "<:"
        start_ = 2
        end = ">"

    # Replace mentioned user IDs with names (Discord -> Matrix)
    elif category == "mention":
        start = "<@!"
        start_ = 3
        end = ">"

    for item in message.split():
        if item.startswith(start) and item.endswith(end):
            item_ = item[start_:-1]

            if category == "emote":
                emote = discord.utils.get(discord_client.emojis, name=item_)
                if emote is not None:
                    message = message.replace(item, str(emote))

            elif category == "emote_":
                emote_name = item_.split(":")[0]
                message = message.replace(item, f":{emote_name}:")

            elif category == "mention":
                user = discord_client.get_user(int(item_))
                message = message.replace(item, f"@{user.name}")

    return message


async def webhook_send(author, avatar, message):
    # Create webhook if it doesn't exist
    hook_name = "matrix_bridge"
    hooks = await channel_.webhooks()
    hook = discord.utils.get(hooks, name=hook_name)
    if hook is None:
        hook = await channel_.create_webhook(name=hook_name)

    # Replace emote names
    message = await process(message, "emote")

    await hook.send(username=author, avatar_url=avatar, content=message)


async def partial_mention(user):
    # Get guild to parse member list
    guild = channel_.guild

    # Remove "@"
    user = user[1:]

    for member in await guild.query_members(query=user):
        user_mention = f"<@!{member.id}>"
        return user_mention

    return None


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

    matrix_client.add_ephemeral_callback(typing_callback, nio.EphemeralEvent)

    # Sync forever
    await matrix_client.sync_forever(timeout=timeout)

    await matrix_client.logout()
    await matrix_client.close()


async def message_send(message):
    await matrix_client.room_send(
        room_id=config["room_id"],
        message_type="m.room.message",
        content={
            "msgtype": "m.text",
            "body": message
        }
    )


async def message_callback(room, event):
    # Don't act on activities in other rooms
    if room.room_id != config["room_id"]:
        return

    message = event.body

    if not message:
        return

    # Don't reply to ourselves
    if event.sender == matrix_client.user:
        return

    author = event.sender[1:]
    avatar = None

    homeserver = author.split(":")[-1]
    url = "https://matrix.org/_matrix/media/r0/download"

    # Don't mention @everyone or @here
    message = message.replace("@everyone", "@\u200Beveryone")
    message = message.replace("@here", "@\u200Bhere")

    # Replace partial mention of Discord user with ID
    if message.startswith("@"):
        user = message.split()[0]
        user_mention = await partial_mention(user)

        if user_mention is not None:
            message = message.replace(user, user_mention)

    # Get attachments
    try:
        attachment = event.url.split("/")[-1]
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

    await webhook_send(author, avatar, message)


async def typing_callback(room, event):
    # Don't act on activities in other rooms
    if room.room_id != config["room_id"]:
        return

    if room.typing_users:
        # Don't act on ourselves
        if len(room.typing_users) == 1 \
                and room.typing_users[0] == matrix_client.user:
            return

        # Send typing event
        async with channel_.typing():
            pass


def main():
    # Start Discord bot
    discord_client.run(config["token"])


if __name__ == "__main__":
    main()

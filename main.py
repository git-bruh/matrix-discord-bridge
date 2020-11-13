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

discord_client = discord.Client()

logging.basicConfig(level=logging.INFO)


@discord_client.event
async def on_ready():
    print(f"Logged in as {discord_client.user}")

    # Start Matrix bot
    await create_matrix_client()


@discord_client.event
async def on_message(message):
    # Don't respond to bots/webhooks
    if message.author.bot:
        return

    # Replace IDs in mentions with the tagged user's name
    content = await process(message.content, "emote_")
    content = await process(content, "mention")

    message_ = f"<{message.author.name}> {content}"

    if str(message.channel.id) == config["channel_id"]:
        await message_send(message_)


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
    # Get Discord channel from channel ID
    channel = int(config["channel_id"])
    channel = discord_client.get_channel(channel)

    # Create webhook if it doesn't exist
    hook_name = "matrix_bridge"
    hooks = await channel.webhooks()
    hook = discord.utils.get(hooks, name=hook_name)
    if hook is None:
        hook = await channel.create_webhook(name=hook_name)

    # Replace emote names
    message = await process(message, "emote")

    await hook.send(username=author, avatar_url=avatar, content=message)


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

    matrix_client.add_event_callback(message_callback, nio.RoomMessageText)

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
    message = event.body

    if not message:
        return

    # Don't reply to ourselves
    if event.sender == matrix_client.user:
        return

    author = event.sender[1:]
    avatar = None

    # Get avatar
    for user in room.users.values():
        if user.user_id == event.sender:
            if user.avatar_url:
                homeserver = author.split(":")[-1]

                avatar = user.avatar_url.split("/")[-1]
                avatar = "https://matrix.org/_matrix/media/r0/download/" \
                         f"{homeserver}/{avatar}"
                break

    await webhook_send(author, avatar, message)


def main():
    # Start Discord bot
    discord_client.run(config["token"])


if __name__ == "__main__":
    main()

import asyncio
import logging
import json
import os
import discord
from nio import AsyncClient, MatrixRoom, RoomMessageText


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
    await asyncio.create_task(create_matrix_client())


@discord_client.event
async def on_message(message):
    if message.author.bot:
        return


async def webhook_send(author, message):
    hook_name = "matrix_bridge"

    channel = int(config["channel_id"])
    channel = discord_client.get_channel(channel)

    hooks = await channel.webhooks()
    hook = discord.utils.get(hooks, name=hook_name)
    if hook is None:
        hook = await channel.create_webhook(name=hook_name)

    await hook.send(content=message, username=author)


async def create_matrix_client():
    homeserver = config["homeserver"]
    username = config["username"]
    password = config["password"]

    client = AsyncClient(homeserver, username)
    print(await client.login(password))

    client.add_event_callback(message_callback, RoomMessageText)
    await client.sync_forever(timeout=30000)


async def message_callback(room: MatrixRoom, event: RoomMessageText):
    if room.room_id == config["room_id"]:
        author = room.user_name(event.sender)
        message = event.body
        await webhook_send(author, message)


def main():
    discord_client.run(config["token"])


if __name__ == "__main__":
    main()

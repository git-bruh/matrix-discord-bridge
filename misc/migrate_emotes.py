import asyncio
import json
import logging
import os
import sys
import uuid
import aiofiles
import aiofiles.os
import aiohttp
import discord
import nio


def config_gen(config_file):
    config_dict = {
        "homeserver": "https://matrix.org",
        "username": "@name:matrix.org",
        "password": "my-secret-password",
        "token": "my-secret-token",
        "migrate": {"guild_id": "room_id"},
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


class MatrixClient(nio.AsyncClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.logger = logging.getLogger("matrix_logger")
        self.uploaded_emotes = {}

    async def start(self, discord_client):
        timeout = 30000

        self.logger.info(await self.login(config["password"]))

        self.logger.info("Syncing...")
        await self.sync(timeout)

        await discord_client.wait_until_ready()
        await discord_client.migrate()

    async def upload_emote(self, emote):
        emote_name = f":{emote.name}:"
        emote_file = f"/tmp/{str(uuid.uuid4())}"

        async with aiohttp.ClientSession() as session:
            async with session.get(str(emote.url)) as resp:
                emote_ = await resp.read()
                content_type = resp.content_type

        async with aiofiles.open(emote_file, "wb") as f:
            await f.write(emote_)

        async with aiofiles.open(emote_file, "rb") as f:
            resp, maybe_keys = await self.upload(f, content_type=content_type)

        await aiofiles.os.remove(emote_file)

        if type(resp) != nio.UploadResponse:
            self.logger.warning(f"Failed to upload {emote_name}")
            return

        self.logger.info(f"Uploaded {emote_name}")

        url = resp.content_uri

        self.uploaded_emotes[emote_name] = {}
        self.uploaded_emotes[emote_name]["url"] = url

    async def send_emote_state(self, room_id, emote_dict):
        event_type = "im.ponies.room_emotes"

        emotes = {}

        emotes_ = await self.room_get_state_event(room_id, event_type)

        # Get previous emotes from room
        if type(emotes_) != nio.RoomGetStateEventError:
            emotes = emotes_.content.get("emoticons")

        content = {"emoticons": {**emotes, **emote_dict}}

        resp = await self.room_put_state(room_id, event_type, content)

        if type(resp) == nio.RoomPutStateError:
            self.logger.warning(f"Failed to send emote state: {resp}")


class DiscordClient(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.matrix_client = MatrixClient(
            config["homeserver"], config["username"]
        )

        self.bg_task = self.loop.create_task(
            self.log_exceptions(self.matrix_client)
        )

        self.logger = logging.getLogger("discord_logger")

    async def log_exceptions(self, matrix_client):
        try:
            return await matrix_client.start(self)
        except Exception as e:
            matrix_client.logger.warning(f"Unknown exception occurred: {e}")

        await matrix_client.close()

    async def migrate(self):
        for guild in config["migrate"].keys():
            emote_guild = self.get_guild(int(guild))
            emote_room = config["migrate"][guild]

            if emote_guild:
                self.logger.info(
                    f"Guild: {emote_guild.name} Room: {emote_room}"
                )

                await asyncio.gather(
                    *map(self.matrix_client.upload_emote, emote_guild.emojis)
                )

                self.logger.info("Sending state event to room...")

                await self.matrix_client.send_emote_state(
                    emote_room, self.matrix_client.uploaded_emotes
                )

        self.logger.info("Finished uploading emotes")

        await self.matrix_client.logout()
        await self.matrix_client.close()

        await self.close()


def main():
    logging.basicConfig(level=logging.INFO)

    DiscordClient().run(config["token"])


if __name__ == "__main__":
    main()

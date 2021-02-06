# matrix-discord-bridge

A simple non-puppeting bridge between Matrix and Discord written in Python.

## Installation

`pip install -r requirements.txt`

## Usage

* Run `main.py` to generate `config.json`

* Edit `config.json`

```
{
    "homeserver": "https://matrix.org",
    "username": "@name:matrix.org",
    "password": "my-secret-password",
    "token": "my-secret-token",
    "discord_prefix": "my-command-prefix", # Prefix for Discord commands
    "bridge": {
        "channel_id": "room_id",  # Bridge multiple channels and rooms
        "channel_id2": "room_id2"
    }
}
```

* Logs are saved to the `bot.log` file in `$PWD`.

* Normal Discord bot functionality like commands can be added to the bot via [cogs](https://discordpy.readthedocs.io/en/latest/ext/commands/cogs.html), example [here](https://gist.github.com/EvieePy/d78c061a4798ae81be9825468fe146be).

* Replace `guild.emojis` with `self.discord_client.emojis` (`Callbacks()`, `process_message()`) to make the Discord bot use emojis from ALL it's guilds.

NOTE: [Privileged Intents](https://discordpy.readthedocs.io/en/latest/intents.html#privileged-intents) must be enabled for your Discord bot.

## Screenshots
TODO

## What Works

- [x] Sending messages
- [x] Discord webhooks (with avatars)
- [x] Attachments (Converted to URLs)
- [x] Typing status (Not very accurate)
- [x] Redacting messages
- [x] Editing messages
- [x] Replies
- [x] Bridging multiple channels/rooms
- [x] `:emote:` in Matrix message converted to Discord emotes
- [x] Discord emotes bridged as inline images (Works on Element Web, Fluffychat)

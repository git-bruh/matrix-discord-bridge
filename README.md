# matrix-discord-bridge

A simple bridge between Matrix and Discord written in Python.

## Installation

`pip install -r requirements.txt`

## Usage

* Run `main.py` to generate `config.json`

* Edit `config.json`

NOTE: [Privileged Intents](https://discordpy.readthedocs.io/en/latest/intents.html#privileged-intents) must be enabled for your Discord bot.

## What Works

- [x] Sending messages
- [x] Discord webhooks (with avatars)
- [x] Attachments (Converted to URLs)
- [x] Typing status (Not very accurate)
- [x] Redacting messages
- [x] Editing messages (Partial, see [this issue](https://github.com/Rapptz/discord.py/issues/6058))
- [x] Replies

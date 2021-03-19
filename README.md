# matrix-discord-bridge

A simple bridge between Matrix and Discord written in Python.

This repository contains two bridges:
* The `appservice` directory contains the puppeting appservice written without any helper libraries and minimal dependencies. Running this requires a self-hosted homeserver.

* The `bridge` directory contains the non-puppeting bridge written with `matrix-nio` and `discord.py`, most people would want to use this one.

Check their respective `README.md`'s for more information.

NOTE: [Privileged Intents](https://discordpy.readthedocs.io/en/latest/intents.html#privileged-intents) must be enabled for your Discord bot.

## What Works

- [x] Puppeting (Appservice only, regular bridge only uses webhooks on Discord.)
- [x] Attachments (Converted to URLs.)
- [x] Typing Indicators
- [x] Message redaction
- [x] Replies
- [x] Bridging multiple channels
- [x] Discord emojis displayed as inline images
- [x] Sending Discord emotes from Matrix (`:emote_name:`)
- [x] Mentioning Discord users via partial username (`@partialname`)

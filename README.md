# matrix-discord-bridge

A simple bridge between Matrix and Discord written in Python.

This repository contains two bridges:
* A [puppeting appservice](appservice) (experimental-ish): The puppeting bridge written with minimal dependencies. Running this requires a self-hosted homeserver.

* A [non-puppeting bridge](bridge): The non-puppeting bridge written with `matrix-nio` and `discord.py`, most people would want to use this one.

Check their READMEs for specific information.

## What Works

- [x] Puppeting (Appservice only, regular bridge only uses webhooks on Discord.)
- [x] Attachments (Converted to URLs.)
- [x] Typing Indicators (Per-user indicators on Appservice, otherwise sent as bot user.)
- [x] Message redaction
- [x] Replies
- [x] Bridging multiple channels
- [x] Discord emojis displayed as inline images
- [x] Sending Discord emotes from Matrix (`:emote_name:`)
- [x] Mentioning Discord users via partial username (`@partialname`)

## Installation

`pip install -r requirements.txt`

## Usage

**Disclaimer:** This was one of the author's first non-trivial projects with Python, so code quality is not too good. The appservice has much better code.

* Run `main.py` to generate `config.json`

* Edit `config.json`:

```
{
    "homeserver": "https://matrix.org",
    "username": "@name:matrix.org",
    "password": "my-secret-password",  # Matrix password.
    "token": "my-secret-token",  # Discord bot token.
    "discord_cmd_prefix": "my-command-prefix",
    "bridge": {
                "channel_id": "room_id",
                "channel_id2": "room_id2",  # Bridge multiple rooms.
     },
}
```

This bridge does not use databases for keeping track of bridged rooms to avoid a dependency on persistent storage. This makes it easy to host on something like Heroku with the free tier.

* Logs are saved to the `bridge.log` file in `$PWD`.

* Normal Discord bot functionality like commands can be added to the bot via [cogs](https://discordpy.readthedocs.io/en/latest/ext/commands/cogs.html), example [here](https://gist.github.com/EvieePy/d78c061a4798ae81be9825468fe146be).

**NOTE:** [Privileged Intents](https://discordpy.readthedocs.io/en/latest/intents.html#privileged-intents) must be enabled for your Discord bot.

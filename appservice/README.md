## Installation

`pip install -r requirements.txt`

## Usage

* Run `main.py` to generate `appservice.json`

* Edit `appservice.json`:

```
{
    "as_token": "my-secret-as-token",
    "hs_token": "my-secret-hs-token",
    "user_id": "appservice-discord",
    # Homeserver running on the same machine, listening on port 8008.
    "homeserver": "http://127.0.0.1:8008",
    # Change "localhost" to your server_name.
    # Eg. "kde.org" is the server_name in "@testuser:kde.org".
    "server_name": "localhost",
    "discord_token": "my-secret-discord-token",
    "port": 5000,  # Port to run the bottle app on.
    "database": "/path/to/bridge.db"
}
```

* Create `appservice.yaml` and add it to your homeserver configuration:

```
id: "discord"
url: "http://127.0.0.1:5000"
as_token: "my-secret-as-token"
hs_token: "my-secret-hs-token"
sender_localpart: "appservice-discord"
namespaces:
  users:
    - exclusive: true
      regex: "@_discord.*"
    # Work around for temporary bug in dendrite.
    - regex: "@appservice-discord"
  aliases:
    - exclusive: false
      regex: "#_discord.*"
  rooms: []
```

A path can optionally be passed as the first argument to `main.py`. This path will be used as the base directory for the database and log file.

Eg. Running `python3 main.py /path/to/my/dir` will store the database and logs in `/path/to/my/dir`.
`$PWD` is used by default if no path is specified.

After setting up the bridge, send a direct message to `@appservice-discord:domain.tld` containing the channel ID to be bridged (`!bridge 123456`).

This bridge is written with:
* `bottle`: Receiving events from the homeserver.
* `urllib3`: Sending requests, thread safety.
* `websockets`: Connecting to Discord. (Big thanks to an anonymous person "nesslersreagent" for figuring out the initial connection mess.)

## NOTES

* A basic sqlite database is used for keeping track of bridged rooms.

* Logs are saved to the `appservice.log` file in `$PWD` or the specified directory.

* For avatars to show up on Discord, you must have a [reverse proxy](https://github.com/matrix-org/dendrite/blob/master/docs/nginx/monolith-sample.conf) set up on your homeserver as the bridge does not specify the homeserver port when passing the avatar url.

* It is not possible to add "normal" Discord bot functionality like commands as this bridge does not use `discord.py`.

* [Privileged Intents](https://discordpy.readthedocs.io/en/latest/intents.html#privileged-intents) for members and presence must be enabled for your Discord bot.

* This Appservice might not work well for bridging a large number of rooms since it is mostly synchronous. However, it wouldn't take much effort to port it to `asyncio` and `aiohttp` if desired.

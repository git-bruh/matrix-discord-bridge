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

This bridge is written with:
* `bottle`: Receiving events from the homeserver.
* `urllib3`: Sending requests, thread safety.
* `websockets`: Connecting to Discord. (Big thanks to an anonymous person "nesslersreagent" for figuring out the initial connection mess.)

* A basic sqlite database is used for keeping track of bridged rooms.

* Logs are saved to the `appservice.log` file in `$PWD` or the specified directory.

* For avatars to show up on Discord, you must have a [reverse proxy](https://github.com/matrix-org/dendrite/blob/master/docs/nginx/monolith-sample.conf) set up on your homeserver as the bridge does not specify the homeserver port when passing the avatar url.

* It is not possible to add normal Discord bot functionality like commands as this bridge does not use `discord.py`.

NOTE: [Privileged Intents](https://discordpy.readthedocs.io/en/latest/intents.html#privileged-intents) must be enabled for your Discord bot.

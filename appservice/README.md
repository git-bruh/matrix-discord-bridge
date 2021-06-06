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
    "homeserver": "http://127.0.0.1:8008",
    "server_name": "localhost",
    "discord_token": "my-secret-discord-token",
    "port": 5000,
    "database": "/path/to/bridge.db"
}
```

`as_token`: The token sent by the appservice to the homeserver with events.

`hs_token`: The token sent by the homeserver to the appservice with events.

`user_id`: The username of the appservice user, it should match the `sender_localpart` in `appservice.yaml`.

`homeserver`: A URL including the port where the homeserver is listening on. The default should work in most cases where the homeserver is running locally and listening for non-TLS connections on port `8008`.

`server_name`: The server's name, it is the part after `:` in MXIDs. As an example, `kde.org` is the server name in `@testuser:kde.org`.

`discord_token`: The Discord bot's token.

`port`: The port where `bottle` will listen for events.

`database`: Full path to the bridge's database.

Both `as_token` and `hs_token` MUST be the same as their values in `appservice.yaml`. Their value can be set to anything, refer to the [spec](https://matrix.org/docs/spec/application_service/r0.1.2#registration).

* Create `appservice.yaml` and add it to your homeserver:

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
    - exclusive: true
      regex: "@appservice-discord"
  aliases:
    - exclusive: true
      regex: "#_discord.*"
  rooms: []
```

The following lines should be added to the homeserver configuration. The full path to `appservice.yaml` might be required:

* `synapse`:

```
# A list of application service config files to use
#
app_service_config_files:
  - appservice.yaml
```

* `dendrite`:

```
app_service_api:
  internal_api:
    # ...
  database:
    # ...
  config_files: [appservice.yaml]
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

* Discord users can be tagged only by mentioning the dummy Matrix user, which requires the client to send a formatted body containing HTML. Partial mentions are not used to avoid unreliable queries to the websocket.

* Logs are saved to the `appservice.log` file in `$PWD` or the specified directory.

* For avatars to show up on Discord, you must have a [reverse proxy](https://github.com/matrix-org/dendrite/blob/master/docs/nginx/monolith-sample.conf) set up on your homeserver as the bridge does not specify the homeserver port when passing the avatar url.

* It is not possible to add "normal" Discord bot functionality like commands as this bridge does not use `discord.py`.

* [Privileged Intents](https://discordpy.readthedocs.io/en/latest/intents.html#privileged-intents) for members and presence must be enabled for your Discord bot.

* This Appservice might not work well for bridging a large number of rooms since it is mostly synchronous. However, it wouldn't take much effort to port it to `asyncio` and `aiohttp` if desired.

from dataclasses import dataclass

CDN_URL = "https://cdn.discordapp.com"


@dataclass
class Channel(object):
    id: str
    type: str
    guild_id: str = ""
    name: str = ""
    topic: str = ""


@dataclass
class Emote(object):
    animated: bool
    id: str
    name: str


class User(object):
    def __init__(self, user: dict) -> None:
        self.discriminator = user["discriminator"]
        self.id = user["id"]
        self.username = user["username"]

        avatar = user["avatar"]

        if not avatar:
            # https://discord.com/developers/docs/reference#image-formatting
            self.avatar_url = (
                f"{CDN_URL}/embed/avatars/{int(self.discriminator) % 5}.png"
            )
        else:
            ext = "gif" if avatar.startswith("a_") else "png"
            self.avatar_url = f"{CDN_URL}/avatars/{self.id}/{avatar}.{ext}"


class Message(object):
    def __init__(self, message: dict) -> None:
        self.attachments = message.get("attachments", [])
        self.channel_id = message["channel_id"]
        self.content = message.get("content", "")
        self.id = message["id"]
        self.reference = message.get("reference", {}).get("message_id", "")
        self.webhook_id = message.get("webhook_id", "")

        author = message.get("author")

        self.author = User(author) if author else None


@dataclass
class DeletedMessage(object):
    channel_id: str
    id: str


@dataclass
class Typing(object):
    user_id: str
    channel_id: str


@dataclass
class Webhook(object):
    id: str
    token: str


class ChannelType(object):
    GUILD_TEXT = 0
    DM = 1
    GUILD_VOICE = 2
    GROUP_DM = 3
    GUILD_CATEGORY = 4
    GUILD_NEWS = 5
    GUILD_STORE = 6


class InteractionResponseType(object):
    PONG = 0
    ACKNOWLEDGE = 1
    CHANNEL_MESSAGE = 2
    CHANNEL_MESSAGE_WITH_SOURCE = 4
    ACKNOWLEDGE_WITH_SOURCE = 5


class GatewayIntents(object):
    def bitmask(bit: int) -> int:
        return 1 << bit

    GUILDS = bitmask(0)
    GUILD_MEMBERS = bitmask(1)
    GUILD_BANS = bitmask(2)
    GUILD_EMOJIS = bitmask(3)
    GUILD_INTEGRATIONS = bitmask(4)
    GUILD_WEBHOOKS = bitmask(5)
    GUILD_INVITES = bitmask(6)
    GUILD_VOICE_STATES = bitmask(7)
    GUILD_PRESENCES = bitmask(8)
    GUILD_MESSAGES = bitmask(9)
    GUILD_MESSAGE_REACTIONS = bitmask(10)
    GUILD_MESSAGE_TYPING = bitmask(11)
    DIRECT_MESSAGES = bitmask(12)
    DIRECT_MESSAGE_REACTIONS = bitmask(13)
    DIRECT_MESSAGE_TYPING = bitmask(14)


class GatewayOpCodes(object):
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    PRESENCE_UPDATE = 3
    VOICE_STATE_UPDATE = 4
    RESUME = 6
    RECONNECT = 7
    REQUEST_GUILD_MEMBERS = 8
    INVALID_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11


class Payloads(object):
    def __init__(self, token: str, seq: int, session_id: str) -> None:
        self.HEARTBEAT = {"op": GatewayOpCodes.HEARTBEAT, "d": seq}

        self.IDENTIFY = {
            "op": GatewayOpCodes.IDENTIFY,
            "d": {
                "token": token,
                "intents": GatewayIntents.GUILDS
                | GatewayIntents.GUILD_MESSAGES
                | GatewayIntents.GUILD_MESSAGE_TYPING,
                "properties": {
                    "$os": "discord",
                    "$browser": "discord",
                    "$device": "discord",
                },
            },
        }

        self.RESUME = {
            "op": GatewayOpCodes.RESUME,
            "d": {"token": token, "session_id": session_id, "seq": seq},
        }

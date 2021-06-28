from dataclasses import dataclass

from misc import dict_cls

CDN_URL = "https://cdn.discordapp.com"
ID_LEN = 18
MESSAGE_LIMIT = 2000


def bitmask(bit: int) -> int:
    return 1 << bit


@dataclass
class Channel:
    id: str
    type: str
    guild_id: str = ""
    name: str = ""
    topic: str = ""


@dataclass
class Emote:
    animated: bool
    id: str
    name: str


@dataclass
class MessageReference:
    message_id: str
    channel_id: str
    guild_id: str


@dataclass
class Typing:
    user_id: str
    channel_id: str


@dataclass
class Webhook:
    id: str
    token: str


class User:
    def __init__(self, user: dict) -> None:
        self.discriminator = user["discriminator"]
        self.id = user["id"]
        self.mention = f"<@{self.id}>"
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


class Guild:
    def __init__(self, guild: dict) -> None:
        self.guild_id = guild["id"]
        self.channels = [dict_cls(c, Channel) for c in guild["channels"]]
        self.emojis = [dict_cls(e, Emote) for e in guild["emojis"]]
        members = [member["user"] for member in guild["members"]]
        self.members = [User(m) for m in members]


class GuildEmojisUpdate:
    def __init__(self, update: dict) -> None:
        self.guild_id = update["guild_id"]
        self.emojis = [dict_cls(e, Emote) for e in update["emojis"]]


class GuildMembersChunk:
    def __init__(self, chunk: dict) -> None:
        self.chunk_index = chunk["chunk_index"]
        self.chunk_count = chunk["chunk_count"]
        self.guild_id = chunk["guild_id"]
        self.members = [User(m) for m in chunk["members"]]


class GuildMemberUpdate:
    def __init__(self, update: dict) -> None:
        self.guild_id = update["guild_id"]
        self.user = User(update["user"])


class Message:
    def __init__(self, message: dict) -> None:
        self.attachments = message.get("attachments", [])
        self.channel_id = message["channel_id"]
        self.content = message.get("content", "")
        self.id = message["id"]
        self.webhook_id = message.get("webhook_id", "")

        self.mentions = [
            User(mention) for mention in message.get("mentions", [])
        ]

        ref = message.get("referenced_message")

        self.referenced_message = Message(ref) if ref else None

        author = message.get("author")

        self.author = User(author) if author else None


class ChannelType:
    GUILD_TEXT = 0
    DM = 1
    GUILD_VOICE = 2
    GROUP_DM = 3
    GUILD_CATEGORY = 4
    GUILD_NEWS = 5
    GUILD_STORE = 6


class InteractionResponseType:
    PONG = 0
    ACKNOWLEDGE = 1
    CHANNEL_MESSAGE = 2
    CHANNEL_MESSAGE_WITH_SOURCE = 4
    ACKNOWLEDGE_WITH_SOURCE = 5


class GatewayIntents:
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


class GatewayOpCodes:
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


class Payloads:
    def __init__(self, token: str) -> None:
        self.seq = self.session = None
        self.token = token

    def HEARTBEAT(self) -> dict:
        return {"op": GatewayOpCodes.HEARTBEAT, "d": self.seq}

    def IDENTIFY(self) -> dict:
        return {
            "op": GatewayOpCodes.IDENTIFY,
            "d": {
                "token": self.token,
                "intents": GatewayIntents.GUILDS
                | GatewayIntents.GUILD_EMOJIS
                | GatewayIntents.GUILD_MEMBERS
                | GatewayIntents.GUILD_MESSAGES
                | GatewayIntents.GUILD_MESSAGE_TYPING
                | GatewayIntents.GUILD_PRESENCES,
                "properties": {
                    "$os": "discord",
                    "$browser": "Discord Client",
                    "$device": "discord",
                },
            },
        }

    def RESUME(self) -> dict:
        return {
            "op": GatewayOpCodes.RESUME,
            "d": {
                "token": self.token,
                "session_id": self.session,
                "seq": self.seq,
            },
        }

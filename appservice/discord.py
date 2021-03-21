from dataclasses import dataclass
from typing import Optional


def bitmask(bit: int) -> int:
    return 1 << bit


class Channel(object):
    def __init__(self, channel: dict) -> None:
        self.guild_id = channel.get("guild_id")
        self.id = channel["id"]
        self.name = channel.get("name")
        self.topic = channel.get("topic")
        self.type = channel["type"]


@dataclass
class Emote(object):
    animated: bool
    id: str
    name: str


@dataclass
class User(object):
    avatar_url: Optional[str]
    discriminator: str
    id: str
    username: str


@dataclass
class Message(object):
    attachments: list
    author: User
    content: str
    channel_id: str
    id: str
    reference: Optional[str]
    webhook_id: Optional[str]


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

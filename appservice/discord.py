from dataclasses import dataclass
from typing import Optional


def bitmask(bit: int) -> int:
    return 1 << bit


@dataclass
class Channel(object):
    id: str
    name: str
    topic: str
    type: int


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
    sender: str
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


class Payloads(GatewayIntents, GatewayOpCodes):
    def __init__(self, token):
        # TODO: Use updated seqnum
        self.HEARTBEAT = {"op": self.HEARTBEAT, "d": 0}

        self.IDENTIFY = {
            "op": self.IDENTIFY,
            "d": {
                "token": token,
                "intents": self.GUILDS
                | self.GUILD_MESSAGES
                | self.GUILD_MESSAGE_TYPING,
                "properties": {
                    "$os": "discord",
                    "$browser": "discord",
                    "$device": "discord",
                },
            },
        }

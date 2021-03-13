from dataclasses import dataclass


@dataclass
class Event(object):
    body: str
    channel_id: int
    event_id: str
    is_direct: bool
    homeserver: str
    room_id: str
    sender: str
    state_key: str


@dataclass
class User(object):
    avatar_url: str
    display_name: str

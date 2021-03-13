from dataclasses import dataclass


@dataclass
class User(object):
    avatar_url: str
    display_name: str


@dataclass
class Event(object):
    author: User
    body: str
    channel_id: int
    event_id: str
    is_direct: bool
    redacts: str
    relates_to: str
    room_id: str
    new_body: str
    sender: str
    state_key: str

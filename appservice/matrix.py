from dataclasses import dataclass
from typing import Optional


@dataclass
class User(object):
    avatar_url: str
    display_name: str


@dataclass
class Event(object):
    author: User
    body: str
    channel_id: str
    event_id: str
    is_direct: bool
    relates_to: str
    room_id: str
    new_body: str
    sender: str
    state_key: Optional[str]

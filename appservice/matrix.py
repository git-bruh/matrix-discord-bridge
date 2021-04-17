from dataclasses import dataclass


@dataclass
class User(object):
    avatar_url: str
    displayname: str


class Event(object):
    def __init__(self, event: dict):
        content = event["content"]

        self.author = event["author"]
        self.body = content.get("body", "")
        self.event_id = event["event_id"]
        self.is_direct = content.get("is_direct", False)
        self.room_id = event["room_id"]
        self.sender = event["sender"]
        self.state_key = event.get("state_key", "")

        rel = content.get("m.relates_to", {})

        self.relates_to = rel.get("event_id")
        self.reltype = rel.get("rel_type")
        self.new_body = content.get("m.new_content", {}).get("body", "")

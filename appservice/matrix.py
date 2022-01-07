from dataclasses import dataclass


@dataclass
class User:
    avatar_url: str = ""
    display_name: str = ""


class Event:
    def __init__(self, event: dict):
        content = event.get("content", {})

        self.attachment = content.get("url")
        self.body = content.get("body", "").strip()
        self.formatted_body = content.get("formatted_body", "")
        self.id = event["event_id"]
        self.is_direct = content.get("is_direct", False)
        self.redacts = event.get("redacts", "")
        self.room_id = event["room_id"]
        self.sender = event["sender"]
        self.state_key = event.get("state_key", "")
        self.redacted_because = event.get("redacted_because", {})
        rel = content.get("m.relates_to", {})

        self.relates_to = rel.get("event_id")
        self.reltype = rel.get("rel_type")
        self.reply: dict = rel.get("m.in_reply_to")
        self.new_body = content.get("m.new_content", {})

import json
import logging
import urllib.parse
import uuid
from typing import Union

import bottle
import urllib3

import matrix
from cache import Cache
from misc import log_except, request


class AppService(bottle.Bottle):
    def __init__(self, config: dict, http: urllib3.PoolManager) -> None:
        super(AppService, self).__init__()

        self.as_token = config["as_token"]
        self.hs_token = config["hs_token"]
        self.base_url = config["homeserver"]
        self.server_name = config["server_name"]
        self.user_id = f"@{config['user_id']}:{self.server_name}"
        self.http = http
        self.logger = logging.getLogger("appservice")

        # Map events to functions.
        self.mapping = {
            "m.room.member": "on_member",
            "m.room.message": "on_message",
            "m.room.redaction": "on_redaction",
        }

        # Add route for bottle.
        self.route(
            "/transactions/<transaction>",
            callback=self.receive_event,
            method="PUT",
        )

        Cache.cache["m_rooms"] = {}

    def handle_event(self, event: dict) -> None:
        event_type = event.get("type")

        if event_type in (
            "m.room.member",
            "m.room.message",
            "m.room.redaction",
        ):
            obj = matrix.Event(event)
        else:
            self.logger.info(f"Unknown event type: {event_type}")
            return

        func = getattr(self, self.mapping[event_type], None)

        if not func:
            self.logger.warning(
                f"Function '{func}' not defined, ignoring event."
            )
            return

        # We don't catch exceptions here as the homeserver will re-send us
        # the event in case of a failure.
        func(obj)

    @log_except
    def receive_event(self, transaction: str) -> dict:
        """
        Verify the homeserver's token and handle events.
        """

        hs_token = bottle.request.query.getone("access_token")

        if not hs_token:
            bottle.response.status = 401
            return {"errcode": "APPSERVICE_UNAUTHORIZED"}

        if hs_token != self.hs_token:
            bottle.response.status = 403
            return {"errcode": "APPSERVICE_FORBIDDEN"}

        events = bottle.request.json.get("events")

        for event in events:
            self.handle_event(event)

        return {}

    def mxc_url(self, mxc: str) -> str:
        try:
            homeserver, media_id = mxc.replace("mxc://", "").split("/")
        except ValueError:
            return ""

        return (
            f"https://{self.server_name}/_matrix/media/r0/download/"
            f"{homeserver}/{media_id}"
        )

    def join_room(self, room_id: str, mxid: str = "") -> None:
        self.send(
            "POST",
            f"/join/{room_id}",
            params={"user_id": mxid} if mxid else {},
        )

    def redact(self, event_id: str, room_id: str, mxid: str = "") -> None:
        self.send(
            "PUT",
            f"/rooms/{room_id}/redact/{event_id}/{uuid.uuid4()}",
            params={"user_id": mxid} if mxid else {},
        )

    def get_room_id(self, alias: str) -> str:
        with Cache.lock:
            room = Cache.cache["m_rooms"].get(alias)
        if room:
            return room

        resp = self.send("GET", f"/directory/room/{urllib.parse.quote(alias)}")

        room_id = resp["room_id"]

        with Cache.lock:
            Cache.cache["m_rooms"][alias] = room_id

        return room_id

    def get_event(self, event_id: str, room_id: str) -> matrix.Event:
        resp = self.send("GET", f"/rooms/{room_id}/event/{event_id}")

        return matrix.Event(resp)

    def upload(self, url: str) -> str:
        """
        Upload a file to the homeserver and get the MXC url.
        """

        resp = self.http.request("GET", url)

        resp = self.send(
            "POST",
            content=resp.data,
            content_type=resp.headers.get("Content-Type"),
            params={"filename": f"{uuid.uuid4()}"},
            endpoint="/_matrix/media/r0/upload",
        )

        return resp["content_uri"]

    def send_message(
        self,
        room_id: str,
        content: dict,
        mxid: str = "",
    ) -> str:
        resp = self.send(
            "PUT",
            f"/rooms/{room_id}/send/m.room.message/{uuid.uuid4()}",
            content,
            {"user_id": mxid} if mxid else {},
        )

        return resp["event_id"]

    def send_typing(
        self, room_id: str, mxid: str = "", timeout: int = 8000
    ) -> None:
        self.send(
            "PUT",
            f"/rooms/{room_id}/typing/{mxid}",
            {"typing": True, "timeout": timeout},
            {"user_id": mxid} if mxid else {},
        )

    def send_invite(self, room_id: str, mxid: str) -> None:
        self.send("POST", f"/rooms/{room_id}/invite", {"user_id": mxid})

    @request
    def send(
        self,
        method: str,
        path: str = "",
        content: Union[bytes, dict] = {},
        params: dict = {},
        content_type: str = "application/json",
        endpoint: str = "/_matrix/client/r0",
    ) -> dict:
        params["access_token"] = self.as_token
        headers = {"Content-Type": content_type}
        payload = json.dumps(content) if isinstance(content, dict) else content
        endpoint = (
            f"{self.base_url}{endpoint}{path}?"
            f"{urllib.parse.urlencode(params)}"
        )

        return self.http.request(
            method, endpoint, body=payload, headers=headers
        )

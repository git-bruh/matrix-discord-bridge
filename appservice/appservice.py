import json
import logging
import urllib.parse
from typing import Union

import bottle
import urllib3

import matrix
from errors import RequestError
from misc import dict_cls, log_except


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

        # TODO better method.
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

    def handle_event(self, event: dict) -> None:
        event_type = event.get("type")

        if event_type == "m.room.member" or event_type == "m.room.message":
            obj = self.get_event_object(event)
        elif event_type == "m.room.redaction":
            obj = event
        else:
            self.logger.info(f"Unknown event type: {event_type}")
            return

        func = self.mapping[event_type]

        try:
            getattr(self, func)(obj)
        except AttributeError:
            self.logger.warning(
                f"Function '{func}' not defined, ignoring event."
            )
        except Exception:
            self.logger.exception(f"Ignoring exception in '{func}':")

    @log_except
    def receive_event(self, transaction: str) -> dict:
        """
        Verify the homeserver's token and handle events.
        """

        hs_token = bottle.request.query.getone("access_token")

        if not hs_token:
            bottle.response.status = 401
            return {"errcode": "DISCORD.APPSERVICE_UNAUTHORIZED"}

        if hs_token != self.hs_token:
            bottle.response.status = 403
            return {"errcode": "DISCORD.APPSERVICE_FORBIDDEN"}

        events = bottle.request.json.get("events")

        for event in events:
            self.handle_event(event)

        return {}

    def get_event_object(self, event: dict) -> matrix.Event:
        event["author"] = dict_cls(
            self.get_profile(event["sender"]), matrix.User
        )

        return matrix.Event(event)

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
        content = json.dumps(content) if isinstance(content, dict) else content
        endpoint = (
            f"{self.base_url}{endpoint}{path}?"
            f"{urllib.parse.urlencode(params)}"
        )

        try:
            resp = self.http.request(
                method, endpoint, body=content, headers=headers
            )
        except urllib3.exceptions.HTTPError as e:
            raise RequestError(
                f"Failed to connect to the homeserver: {e}"
            ) from None

        if resp.status < 200 or resp.status >= 300:
            raise RequestError(
                f"Failed to '{method}' '{resp.geturl()}':\n{resp.data}"
            )

        return json.loads(resp.data)

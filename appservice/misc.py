import asyncio
import json
from dataclasses import fields
from typing import Any

import urllib3

from errors import RequestError


def dict_cls(dict_var: dict, cls: Any) -> Any:
    """
    Create a dataclass from a dictionary.
    """

    field_names = set(f.name for f in fields(cls))
    filtered_dict = {k: v for k, v in dict_var.items() if k in field_names}

    return cls(**filtered_dict)


def log_except(fn):
    """
    Log unhandled exceptions to a logger instead of `stderr`.
    """

    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            self.logger.exception(f"Exception in '{fn.__name__}':")
            raise

    return wrapper


def wrap_async(fn):
    """
    Call an asynchronous function from a synchronous one.
    """

    def wrapper(self, *args, **kwargs):
        loop = self.loop

        if not loop:
            return

        return asyncio.run_coroutine_threadsafe(
            fn(self, *args, **kwargs), loop=loop
        ).result()

    return wrapper


def request(fn):
    """
    Either return json data or raise a `RequestError` if the request was
    unsuccessful.
    """

    def wrapper(*args, **kwargs):
        try:
            resp = fn(*args, **kwargs)
        except urllib3.exceptions.HTTPError as e:
            raise RequestError(None, f"Failed to connect: {e}") from None

        if resp.status < 200 or resp.status >= 300:
            raise RequestError(
                resp.status,
                f"Failed to get response from '{resp.geturl()}':\n{resp.data}",
            )

        return {} if resp.status == 204 else json.loads(resp.data)

    return wrapper


def except_deleted(fn):
    """
    Ignore the `RequestError` on 404s, the message might have been
    deleted by someone else already.
    """

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RequestError as e:
            if e.status != 404:
                raise

    return wrapper

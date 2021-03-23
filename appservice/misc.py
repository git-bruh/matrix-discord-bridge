import asyncio
from dataclasses import fields
from typing import Any


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
    Requires the class to have a `logger` variable.
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
    Class of the asynchronous function must have the `self.loop` variable.
    """

    def wrapper(self, *args, **kwargs):
        loop = self.loop

        if not loop:
            return

        return asyncio.run_coroutine_threadsafe(
            fn(self, *args, **kwargs), loop=loop
        ).result()

    return wrapper

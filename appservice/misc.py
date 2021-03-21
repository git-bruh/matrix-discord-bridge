from dataclasses import fields
from typing import Any


class RequestError(Exception):
    pass


def dict_cls(dict_var: dict, cls: Any) -> Any:
    """
    Return a dataclass from a dictionary.
    """

    field_names = set(f.name for f in fields(cls))
    filtered_dict = {k: v for k, v in dict_var.items() if k in field_names}

    return cls(**filtered_dict)

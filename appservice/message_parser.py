import re
import logging
from html.parser import HTMLParser
from typing import Optional, Tuple, List, Callable

from db import DataBase
from cache import Cache

htmltomarkdown = {"strong": "**", "ins": "__", "u": "__", "b": "**", "em": "*", "i": "*", "del": "~~", "strike": "~~", "s": "~~"}
headers = {"h1": "***__", "h2": "**__", "h3": "**", "h4": "__", "h5": "*", "h6": ""}

logger = logging.getLogger("message_parser")


def search_attr(attrs: List[Tuple[str, Optional[str]]], searched: str) -> Optional[str]:
    for attr in attrs:
        if attr[0] == searched:
            return attr[1] or ""
    return None


def escape_markdown(to_escape: str):
    to_escape.replace("\\", "\\\\")
    return re.sub(r"([`_*~:<>{}@|(])", r"\\\1", to_escape)


class Tags(object):
    def __init__(self):
        self.c_tags = []
        self.length = 0

    @staticmethod
    def _gauge_length(tag: str) -> int:
        if tag in htmltomarkdown:
            return len(htmltomarkdown.get(tag))
        elif tag == "spoiler":
            return 2
        elif tag == "pre":
            return 3
        elif tag == "code":
            return 1
        return 0

    def append(self, tag: str):
        self.c_tags.append(tag)
        self.length += self._gauge_length(tag)

    def pop(self) -> Optional[str]:
        try:
            last_tag = self.c_tags.pop()
            self.length -= self._gauge_length(last_tag)
            return last_tag
        except IndexError:
            return None

    def get_last(self) -> Optional[str]:
        try:
            return self.c_tags[-1]
        except IndexError:
            return None

    def get_size(self) -> int:
        return self.length

    def __reversed__(self):
        return iter(self.c_tags[::-1])

    def __len__(self):
        return len(self.c_tags)

    def __iter__(self):
        return iter(self.c_tags)

    def __bool__(self):
        return bool(self.c_tags)


class MatrixParser(HTMLParser):
    def __init__(self, db: DataBase, mention_regex: str, mxc_img: Callable, limit: int = 0):
        super().__init__()
        self.message: str = ""
        self.current_link: str = ""
        self.tags: Tags = Tags()
        self.list_num: int = 1
        self.db: DataBase = db
        self.snowflake_regex: str = mention_regex
        self.limit: int = limit
        self.overflow: bool = False
        self.mxc_to_img: Callable = mxc_img

    def search_for_feature(self, acceptable_features: Tuple[str, ...]) -> Optional[str]:
        """Searches for certain feature in opened HTML tags for given text, if found returns the tag, if not returns None"""
        for tag in reversed(self.tags):
            if tag in acceptable_features:
                return tag
        return None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]):
        if "mx-reply" in self.tags:
            return
        self.tags.append(tag)

        if tag in htmltomarkdown:
            self.expand_message(htmltomarkdown[tag])
        elif tag == "code":
            if self.search_for_feature(("pre",)):
                self.expand_message("```" + (search_attr(attrs, "class") or "")[9:] + "\n")
            else:
                self.expand_message("`")
        elif tag == "span":
            spoiler = search_attr(attrs, "data-mx-spoiler")
            if spoiler is not None:
                if spoiler:  # Spoilers can have a reason https://github.com/matrix-org/matrix-doc/pull/2010
                    self.expand_message(f"({spoiler})")
                self.expand_message("||")
                self.tags.append("spoiler")  # Always after span tag
        elif tag == "li":
            list_type = self.search_for_feature(("ul", "ol"))
            if list_type == "ol":
                self.expand_message("\n{}. ".format(self.list_num))
                self.list_num += 1
            else:
                self.expand_message("\n• ")
        elif tag in ("br", "p"):
            if not self.message.endswith('\n'):
                self.expand_message("\n")
            if self.search_for_feature(("blockquote",)):
                self.expand_message("> ")
        elif tag == "a":
            self.parse_mentions(attrs)
        elif tag == "mx-reply":  # we handle replies separately for best effect
            return
        elif tag == "img":
            if search_attr(attrs, "data-mx-emoticon") is not None:
                emote_name = search_attr(attrs, "title")
                if emote_name is None:
                    return
                emote_ = Cache.cache["d_emotes"].get(emote_name.strip(":"))
                if emote_:
                    self.expand_message(emote_)
                else:
                    self.expand_message(emote_name)
            else:
                image_link = search_attr(attrs, "src")
                if image_link and image_link.startswith("mxc://"):
                    self.expand_message(f"[{search_attr(attrs, 'title') or image_link}]({self.mxc_to_img(image_link)})")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            if not self.message.endswith('\n'):
                self.expand_message("\n")
            self.expand_message(headers[tag])
        elif tag == "hr":
            self.expand_message("\n──────────\n")
            self.tags.pop()

    def parse_mentions(self, attrs):
        self.current_link = search_attr(attrs, "href")
        if self.current_link.startswith("https://matrix.to/#/"):
            target = self.current_link[20:]
            if target.startswith("@"):
                self.expand_message(self.parse_user(target.split("?")[0]))
            # Rooms will be handled by handle_data on data

    def parse_user(self, target: str):
        if self.is_discord_user(target):
            snowflake = re.search(re.compile(self.snowflake_regex), target).group(1)
            if snowflake:
                self.current_link = None  # Meaning, skip adding text
                return f"<@{snowflake}>"
        else:
            # Matrix user, not Discord appservice account
            return ""

    def close_tags(self):
        for tag in reversed(self.tags):
            self.handle_endtag(tag)

    def expand_message(self, expansion: str):
        # This calculation is not ideal. self.limit is further restricted by message link length, so if a message
        # doesn't really go over the limit but message + message link does it will still treat it as out of the limit
        if len(self.message) + self.tags.get_size() + len(expansion) > self.limit and self.overflow is False:
            # Lets close all of the tags to make sure we don't have display errors
            self.overflow = True
            self.close_tags()
            raise StopIteration
        self.message += expansion

    def is_discord_user(self, target: str) -> bool:
        return bool(self.db.fetch_user(target))

    def handle_data(self, data):
        if self.tags:
            if self.tags.get_last() != "code":
                data = escape_markdown(data.replace("\n", ""))
            if "mx-reply" in self.tags:
                return
        if self.current_link:
            self.expand_message(f"[{data}](<{self.current_link}>)")
            self.current_link = ""
        elif self.current_link is None:
            self.current_link = ""
        else:
            self.expand_message(data)  # strip new lines, they will be mostly handled by parser

    def handle_endtag(self, tag: str):
        if "mx-reply" in self.tags and tag != "mx-reply":
            return
        if tag in htmltomarkdown:
            self.expand_message(htmltomarkdown[tag])
        last_tag = self.tags.pop()
        if last_tag is None:
            logger.error("tried to pop {} from message tags but list is empty, current message {}".format(tag, self.message))
            return
        if last_tag == "spoiler":
            self.expand_message("||")
            self.tags.pop()  # guaranteed to be a span tag
        if tag == "ol":
            self.list_num = 1
        elif tag == "code":
            if self.search_for_feature(("pre",)):
                self.expand_message("\n```")
            else:
                self.expand_message("`")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.expand_message(headers[tag][::-1])

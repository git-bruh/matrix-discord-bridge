import os
import sqlite3
import threading
from typing import List


class DataBase:
    def __init__(self, db_file) -> None:
        self.create(db_file)

        # The database is accessed via multiple threads.
        self.lock = threading.Lock()

    def create(self, db_file) -> None:
        """
        Create a database with the relevant tables if it doesn't already exist.
        """

        exists = os.path.exists(db_file)

        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.conn.row_factory = self.dict_factory

        self.cur = self.conn.cursor()

        if exists:
            return

        self.cur.execute(
            "CREATE TABLE bridge(room_id TEXT PRIMARY KEY, channel_id TEXT);"
        )

        self.cur.execute(
            "CREATE TABLE users(mxid TEXT PRIMARY KEY, "
            "avatar_url TEXT, username TEXT);"
        )

        self.conn.commit()

    def dict_factory(self, cursor, row):
        """
        https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.row_factory
        """

        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d

    def add_room(self, room_id: str, channel_id: str) -> None:
        """
        Add a bridged room to the database.
        """

        with self.lock:
            self.cur.execute(
                "INSERT INTO bridge (room_id, channel_id) VALUES (?, ?)",
                [room_id, channel_id],
            )
            self.conn.commit()

    def add_user(self, mxid: str) -> None:
        with self.lock:
            self.cur.execute("INSERT INTO users (mxid) VALUES (?)", [mxid])
            self.conn.commit()

    def add_avatar(self, avatar_url: str, mxid: str) -> None:
        with self.lock:
            self.cur.execute(
                "UPDATE users SET avatar_url = (?) WHERE mxid = (?)",
                [avatar_url, mxid],
            )
            self.conn.commit()

    def add_username(self, username: str, mxid: str) -> None:
        with self.lock:
            self.cur.execute(
                "UPDATE users SET username = (?) WHERE mxid = (?)",
                [username, mxid],
            )
            self.conn.commit()

    def get_channel(self, room_id: str) -> str:
        """
        Get the corresponding channel ID for a given room ID.
        """

        with self.lock:
            self.cur.execute(
                "SELECT channel_id FROM bridge WHERE room_id = ?", [room_id]
            )

            room = self.cur.fetchone()

        # Return an empty string if the channel is not bridged.
        return "" if not room else room["channel_id"]

    def list_channels(self) -> List[str]:
        """
        Get a list of all the bridged channels.
        """

        with self.lock:
            self.cur.execute("SELECT channel_id FROM bridge")

            channels = self.cur.fetchall()

        return [channel["channel_id"] for channel in channels]

    def fetch_user(self, mxid: str) -> dict:
        """
        Fetch the profile for a bridged user.
        """

        with self.lock:
            self.cur.execute("SELECT * FROM users where mxid = ?", [mxid])

            user = self.cur.fetchone()

        return {} if not user else user

import os
import sqlite3
import threading

class DataBase(object):
    def __init__(self, db_file) -> None:
        self.create(db_file)

        # The database is accessed via both the threads.
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
                "INSERT INTO bridge (room_id, channel_id) "
                f"VALUES ('{room_id}', '{channel_id}')"
            )
            self.conn.commit()


    def add_user(self, mxid: str) -> None:
        with self.lock:
            self.cur.execute(f"INSERT INTO users (mxid) VALUES ('{mxid}')")
            self.conn.commit()

    def get_channel(self, room_id: str) -> str:
        """
        Get the corresponding channel ID for a given room ID.
        """

        with self.lock:
            self.cur.execute("SELECT channel_id FROM bridge WHERE room_id = ?", [room_id])

            room = self.cur.fetchone()

        # Return an empty string if nothing is bridged.
        return "" if not room else room["channel_id"]

    def list_channels(self) -> list:
        """
        Get a list of all the bridged channels.
        """

        with self.lock:
            self.cur.execute("SELECT channel_id FROM bridge")

            channels = self.cur.fetchall()

        # Returns '[]' if nothing is bridged.
        return [channel["channel_id"] for channel in channels]

    def query_user(self, mxid: str) -> bool:
        """
        Check whether a puppet user has already been created for a given mxid.
        """

        with self.lock:
            self.cur.execute("SELECT mxid FROM users")

            users = self.cur.fetchall()

        return next((True for user in users if user["mxid"] == mxid), False)

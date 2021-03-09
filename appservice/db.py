import sqlite3
import os

class DataBase(object):
    def __init__(self, db_file) -> None:
        self.create(db_file)

    def create(self, db_file) -> None:
        exists = os.path.exists(db_file)

        self.conn = sqlite3.connect(db_file)
        self.conn.row_factory = self.dict_factory

        self.cur = self.conn.cursor()

        if exists:
            return

        self.execute(
            "CREATE TABLE bridge(room_id TEXT PRIMARY KEY, channel_id INT);"
        )

        self.execute(
            "CREATE TABLE users(mxid TEXT PRIMARY KEY, "
            "avatar_url TEXT, username TEXT);"
        )

    def dict_factory(self, cursor, row):
        # https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.row_factory

        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d

    def execute(self, operation: str) -> None:
        self.cur.execute(operation) # TODO remove this useless function
        self.conn.commit()

    def add_room(self, room_id: str, channel_id: int) -> None:
        self.execute(
            "INSERT INTO bridge (room_id, channel_id) "
            f"VALUES ('{room_id}', {channel_id})"
        )

    def add_user(self, mxid: str) -> None:
        self.execute(f"INSERT INTO users (mxid) VALUES ('{mxid}')")

    def get_channel(self, room_id: str) -> int:
        self.cur.execute("SELECT channel_id FROM bridge WHERE room_id = ?", [room_id])

        room = self.cur.fetchone()

        # Return '0' if nothing is bridged.
        return 0 if not room else room["channel_id"]

    def list_channels(self) -> list:
        self.execute("SELECT channel_id FROM bridge")

        channels = self.cur.fetchall()

        # Returns '[]' if nothing is bridged.
        return [channel["channel_id"] for channel in channels]

    def query_user(self, mxid: str) -> bool:
        self.execute("SELECT mxid FROM users")

        users = self.cur.fetchall()

        return next((True for user in users if user["mxid"] == mxid), False)

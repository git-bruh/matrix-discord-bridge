import threading


class Cache:
    cache = {}
    lock = threading.Lock()

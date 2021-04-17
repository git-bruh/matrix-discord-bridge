class RequestError(Exception):
    def __init__(self, status: int, *args):
        super().__init__(*args)

        self.status = status

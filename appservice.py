import aiohttp.web


class Appservice(object):
    def __init__(self):
        self.access_token = "wfghWEGh3wgWHEf3478sHFWE"

        self.app = aiohttp.web.Application(client_max_size=None)
        self.add_routes()

    def add_routes(self):
        self.app.router.add_route(
            "PUT", "/transactions/{transaction}", self.receive_event
        )
        # self.app.router.add_route("GET", "/rooms/{alias}", self.query_alias)

    def run(self, host="127.0.0.1", port=5000):
        aiohttp.web.run_app(self.app, host=host, port=port)

    async def receive_event(self, transaction):
        json = await transaction.json()
        events = json["events"]

        for event in events:
            if event:
                print(event)

        return aiohttp.web.Response(body=b"{}")


def main():
    app = Appservice()
    app.run()


if __name__ == "__main__":
    main()

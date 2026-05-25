"""Example to fetch pullpoint events."""

from aiohttp import web
import aiohttp
import argparse
import asyncio
import datetime as dt
import logging
import onvif
import os.path
import pprint
import sys

SUBSCRIPTION_TIME = dt.timedelta(minutes=1)
WAIT_TIME = dt.timedelta(seconds=30)


def subscription_lost():
    print("subscription lost")


async def post_handler(request):
    print(request)
    print(request.url)
    for k, v in request.headers.items():
        print(f"{k}: {v}")
    body = await request.content.read()
    print(body)
    return web.Response()


async def run(args):
    mycam = onvif.ONVIFCamera(
        args.host,
        args.port,
        args.username,
        args.password,
        wsdl_dir=f"{os.path.dirname(onvif.__file__)}/wsdl/",
    )
    await mycam.update_xaddrs()

    capabilities = await mycam.get_capabilities()
    pprint.pprint(capabilities)

    if args.notification:
        app = web.Application()
        app.add_routes([web.post("/", post_handler)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.notification_address, args.notification_port)
        await site.start()

        receive_url = f"http://{args.notification_address}:{args.notification_port}/"
        manager = await mycam.create_notification_manager(
            receive_url,
            SUBSCRIPTION_TIME,
            subscription_lost,
        )
        await manager.set_synchronization_point()

        print(f"waiting for messages at {receive_url}...")
        await asyncio.sleep(WAIT_TIME.total_seconds())

        await manager.shutdown()
        await runner.cleanup()
    else:
        manager = await mycam.create_pullpoint_manager(
            SUBSCRIPTION_TIME, subscription_lost
        )
        await manager.set_synchronization_point()

        pullpoint = manager.get_service()
        print("waiting for messages...")

        # PullMessages is a long-poll: the camera holds the connection open
        # until an event arrives or the request Timeout elapses. Cameras are
        # allowed to close the connection at any time (RFC 2616 section 8.1.4)
        # and many close idle connections between polls, which surfaces as
        # aiohttp.ServerDisconnectedError.
        #
        # The library retries a transient disconnect automatically, but a poll
        # loop should still guard against it: the disconnect is harmless, so
        # just issue another PullMessages on the same subscription. There is no
        # need to recreate the pullpoint manager.
        deadline = asyncio.get_event_loop().time() + WAIT_TIME.total_seconds()
        while asyncio.get_event_loop().time() < deadline:
            try:
                messages = await pullpoint.PullMessages(
                    {
                        "MessageLimit": 100,
                        "Timeout": WAIT_TIME,
                    }
                )
            except aiohttp.ServerDisconnectedError:
                print("server disconnected, re-pulling...")
                continue
            print(messages)

        await manager.shutdown()

    await mycam.close()


def main():
    logging.getLogger("zeep").setLevel(logging.DEBUG)

    parser = argparse.ArgumentParser(prog="EventTester")
    parser.add_argument("--host", default="192.168.3.10")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--username", default="hass")
    parser.add_argument("--password", default="peek4boo")
    parser.add_argument("--notification", action=argparse.BooleanOptionalAction)
    parser.add_argument("--notification_address")
    parser.add_argument("--notification_port", type=int, default=8976)

    args = parser.parse_args(sys.argv[1:])
    if args.notification and args.notification_address is None:
        parser.error("--notification requires --notification_address")

    loop = asyncio.get_event_loop()
    loop.run_until_complete(run(args))


if __name__ == "__main__":
    main()

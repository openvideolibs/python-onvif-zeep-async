"""Example to fetch pullpoint events."""

from aiohttp import web
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
        messages = await pullpoint.PullMessages(
            {
                "MessageLimit": 100,
                "Timeout": WAIT_TIME,
            }
        )
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

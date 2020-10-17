"""Example to fetch pullpoint events."""
import asyncio
import datetime as dt
import logging

from pytz import UTC
from zeep import xsd

from onvif import ONVIFCamera

logging.getLogger("zeep").setLevel(logging.DEBUG)


async def run():
    mycam = ONVIFCamera(
        "192.168.3.10",
        80,
        "hass",
        "peek4boo",
        wsdl_dir="/home/jason/python-onvif-zeep-async/onvif/wsdl",
    )
    await mycam.update_xaddrs()

    if not await mycam.create_pullpoint_subscription():
        print("PullPoint not supported")
        return

    event_service = mycam.create_events_service()
    properties = await event_service.GetEventProperties()
    print(properties)
    capabilities = await event_service.GetServiceCapabilities()
    print(capabilities)

    pullpoint = mycam.create_pullpoint_service()
    await pullpoint.SetSynchronizationPoint()
    req = pullpoint.create_type("PullMessages")
    req.MessageLimit = 100
    req.Timeout = dt.timedelta(seconds=30)
    messages = await pullpoint.PullMessages(req)
    print(messages)

    subscription = mycam.create_subscription_service("PullPointSubscription")
    termination_time = (
        (dt.datetime.utcnow() + dt.timedelta(days=1))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    await subscription.Renew(termination_time)
    await subscription.Unsubscribe()
    await mycam.close()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run())

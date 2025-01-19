from __future__ import annotations

import os

import pytest
from zeep.loader import parse_xml
import datetime
from onvif.client import ONVIFCamera
from onvif.settings import DEFAULT_SETTINGS
from onvif.transport import ASYNC_TRANSPORT
from onvif.types import FastDateTime, ForgivingTime

INVALID_TERM_TIME = b'<?xml version="1.0" encoding="UTF-8"?>\r\n<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:SOAP-ENC="http://www.w3.org/2003/05/soap-encoding" xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:wsa5="http://www.w3.org/2005/08/addressing" xmlns:chan="http://schemas.microsoft.com/ws/2005/02/duplex" xmlns:wsa="http://www.w3.org/2005/08/addressing" xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:tns1="http://www.onvif.org/ver10/topics">\r\n<SOAP-ENV:Header>\r\n<wsa5:Action>http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesResponse</wsa5:Action>\r\n</SOAP-ENV:Header>\r\n<SOAP-ENV:Body>\r\n<tev:PullMessagesResponse>\r\n<tev:CurrentTime>2024-08-17T00:56:16Z</tev:CurrentTime>\r\n<tev:TerminationTime>2024-08-17T00:61:16Z</tev:TerminationTime>\r\n</tev:PullMessagesResponse>\r\n</SOAP-ENV:Body>\r\n</SOAP-ENV:Envelope>\r\n'
_WSDL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "onvif", "wsdl")


@pytest.mark.asyncio
async def test_parse_invalid_dt(caplog: pytest.LogCaptureFixture) -> None:
    device = ONVIFCamera("127.0.0.1", 80, "user", "pass", wsdl_dir=_WSDL_PATH)
    device.xaddrs = {
        "http://www.onvif.org/ver10/events/wsdl": "http://192.168.210.102:6688/onvif/event_service"
    }
    # Create subscription manager
    subscription = await device.create_notification_service()
    operation = subscription.document.bindings[subscription.binding_name].get(
        "Subscribe"
    )
    envelope = parse_xml(
        INVALID_TERM_TIME,  # type: ignore[arg-type]
        ASYNC_TRANSPORT,
        settings=DEFAULT_SETTINGS,
    )
    result = operation.process_reply(envelope)
    assert result.CurrentTime == datetime.datetime(
        2024, 8, 17, 0, 56, 16, tzinfo=datetime.timezone.utc
    )
    assert result.TerminationTime == datetime.datetime(
        2024, 8, 17, 1, 1, 16, tzinfo=datetime.timezone.utc
    )
    assert "ValueError" not in caplog.text


def test_parse_invalid_datetime() -> None:
    with pytest.raises(ValueError, match="Invalid character while parsing year"):
        FastDateTime().pythonvalue("aaaa-aa-aaTaa:aa:aaZ")


def test_parse_invalid_time() -> None:
    with pytest.raises(ValueError, match="Unrecognised ISO 8601 time format"):
        ForgivingTime().pythonvalue("aa:aa:aa")


def test_fix_datetime_missing_time() -> None:
    assert FastDateTime().pythonvalue("2024-08-17") == datetime.datetime(
        2024, 8, 17, 0, 0, 0
    )


def test_fix_datetime_missing_t() -> None:
    assert FastDateTime().pythonvalue("2024-08-17 00:61:16Z") == datetime.datetime(
        2024, 8, 17, 1, 1, 16, tzinfo=datetime.timezone.utc
    )
    assert FastDateTime().pythonvalue("2024-08-17 00:61:16") == datetime.datetime(
        2024, 8, 17, 1, 1, 16
    )


def test_fix_datetime_overflow() -> None:
    assert FastDateTime().pythonvalue("2024-08-17T00:61:16Z") == datetime.datetime(
        2024, 8, 17, 1, 1, 16, tzinfo=datetime.timezone.utc
    )
    assert FastDateTime().pythonvalue("2024-08-17T00:60:16Z") == datetime.datetime(
        2024, 8, 17, 1, 0, 16, tzinfo=datetime.timezone.utc
    )
    assert FastDateTime().pythonvalue("2024-08-17T00:59:16Z") == datetime.datetime(
        2024, 8, 17, 0, 59, 16, tzinfo=datetime.timezone.utc
    )
    assert FastDateTime().pythonvalue("2024-08-17T23:59:59Z") == datetime.datetime(
        2024, 8, 17, 23, 59, 59, tzinfo=datetime.timezone.utc
    )
    assert FastDateTime().pythonvalue("2024-08-17T24:00:00Z") == datetime.datetime(
        2024, 8, 18, 0, 0, 0, tzinfo=datetime.timezone.utc
    )


def test_unfixable_datetime_overflow() -> None:
    with pytest.raises(ValueError, match="Invalid character while parsing minute"):
        FastDateTime().pythonvalue("2024-08-17T999:00:00Z")


def test_fix_time_overflow() -> None:
    assert ForgivingTime().pythonvalue("24:00:00") == datetime.time(0, 0, 0)
    assert ForgivingTime().pythonvalue("23:59:59") == datetime.time(23, 59, 59)
    assert ForgivingTime().pythonvalue("23:59:60") == datetime.time(0, 0, 0)
    assert ForgivingTime().pythonvalue("23:59:61") == datetime.time(0, 0, 1)
    assert ForgivingTime().pythonvalue("23:60:00") == datetime.time(0, 0, 0)
    assert ForgivingTime().pythonvalue("23:61:00") == datetime.time(0, 1, 0)


def test_unfixable_time_overflow() -> None:
    with pytest.raises(ValueError, match="Unrecognised ISO 8601 time format"):
        assert ForgivingTime().pythonvalue("999:00:00")

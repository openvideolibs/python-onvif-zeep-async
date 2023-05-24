from __future__ import annotations

import os

import pytest
from zeep.loader import parse_xml

from onvif.client import ONVIFCamera
from onvif.settings import DEFAULT_SETTINGS
from onvif.transport import ASYNC_TRANSPORT
from onvif.util import normalize_url

PULL_POINT_RESPONSE_MISSING_URL = b'<?xml version="1.0" encoding="UTF-8"?>\n<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:wsdd="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:chan="http://schemas.microsoft.com/ws/2005/02/duplex" xmlns:wsa5="http://www.w3.org/2005/08/addressing" xmlns:ns1="http://www.onvif.org/ver10/pacs" xmlns:xmime="http://tempuri.org/xmime.xsd" xmlns:xop="http://www.w3.org/2004/08/xop/include" xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:wsrfbf="http://docs.oasis-open.org/wsrf/bf-2" xmlns:wstop="http://docs.oasis-open.org/wsn/t-1" xmlns:wsrfr="http://docs.oasis-open.org/wsrf/r-2" xmlns:tdn="http://www.onvif.org/ver10/network/wsdl" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tev="http://www.onvif.org/ver10/events/wsdl" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl" xmlns:tmd="http://www.onvif.org/ver10/deviceIO/wsdl" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:trv="http://www.onvif.org/ver10/receiver/wsdl" xmlns:tse="http://www.onvif.org/ver10/search/wsdl"><SOAP-ENV:Header><wsa5:MessageID>urn:uuid:76acd0bc-498e-4657-9414-b386bd4b0985</wsa5:MessageID><wsa5:To SOAP-ENV:mustUnderstand="1">http://192.168.2.18:8080/onvif/device_service</wsa5:To><wsa5:Action SOAP-ENV:mustUnderstand="1">http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionRequest</wsa5:Action></SOAP-ENV:Header><SOAP-ENV:Body><tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference><wsa5:Address/></tev:SubscriptionReference><wsnt:CurrentTime>1970-01-01T00:00:00Z</wsnt:CurrentTime><wsnt:TerminationTime>1970-01-01T00:00:00Z</wsnt:TerminationTime></tev:CreatePullPointSubscriptionResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>\r\n'
_WSDL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "onvif", "wsdl")


def test_normalize_url():
    assert normalize_url("http://1.2.3.4:80") == "http://1.2.3.4:80"
    assert normalize_url("http://1.2.3.4:80:80") == "http://1.2.3.4:80"
    assert normalize_url("http://[:dead:beef::1]:80") == "http://[:dead:beef::1]:80"
    assert normalize_url(None) is None
    assert normalize_url(b"http://[:dead:beef::1]:80") is None


@pytest.mark.asyncio
async def test_normalize_url_with_missing_url():
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
        PULL_POINT_RESPONSE_MISSING_URL,  # type: ignore[arg-type]
        ASYNC_TRANSPORT,
        settings=DEFAULT_SETTINGS,
    )
    result = operation.process_reply(envelope)
    assert normalize_url(result.SubscriptionReference.Address._value_1) is None

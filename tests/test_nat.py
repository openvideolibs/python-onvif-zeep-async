from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from onvif.client import ONVIFCamera

_WSDL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "onvif", "wsdl")

@pytest.mark.asyncio
async def test_rewrite_xaddr_logic():
    """Test the core rewrite_xaddr logic."""
    with patch("onvif.client.TCPConnector"), \
         patch("onvif.client.ClientSession"):
        device = ONVIFCamera("203.0.113.5", 8080, "user", "pass", wsdl_dir=_WSDL_PATH)
        
        # different IP/Port
        assert device.rewrite_xaddr("http://192.168.1.10/service") == "http://203.0.113.5:8080/service"
        
        # dame IP/Port
        assert device.rewrite_xaddr("http://203.0.113.5:8080/service") == "http://203.0.113.5:8080/service"
        
        # default port
        device_80 = ONVIFCamera("203.0.113.5", 80, "user", "pass", wsdl_dir=_WSDL_PATH)
        assert device_80.rewrite_xaddr("http://10.0.0.1/service") == "http://203.0.113.5/service"

@pytest.mark.asyncio
async def test_update_xaddrs_nat_rewrite():
    """Test that update_xaddrs properly rewrites XAddrs in capabilities."""
    with patch("onvif.client.TCPConnector"), \
         patch("onvif.client.ClientSession"):
        
        device = ONVIFCamera("203.0.113.5", 8080, "user", "pass", wsdl_dir=_WSDL_PATH)
        
        mock_capabilities = {
            "Media": {"XAddr": "http://192.168.1.10/onvif/media_service"},
            "Events": {"XAddr": "http://192.168.1.10/onvif/event_service"}
        }
        
        mock_devicemgmt = AsyncMock()
        mock_devicemgmt.GetCapabilities = AsyncMock(return_value=mock_capabilities)
        mock_devicemgmt.binding_key = "device"
        
        with patch.object(device, "create_devicemgmt_service", return_value=mock_devicemgmt):
            await device.update_xaddrs()
            
            expected_media = "http://203.0.113.5:8080/onvif/media_service"
            expected_events = "http://203.0.113.5:8080/onvif/event_service"
            
            assert device._capabilities["Media"]["XAddr"] == expected_media
            assert device._capabilities["Events"]["XAddr"] == expected_events
            
            assert any(v == expected_media for v in device.xaddrs.values())
            assert any(v == expected_events for v in device.xaddrs.values())

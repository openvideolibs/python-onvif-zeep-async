"""Unit tests for the subscription managers."""

from __future__ import annotations

import datetime as dt
import os

import pytest

import onvif
from onvif import ONVIFCamera
from onvif.managers import (
    MINIMUM_SUBSCRIPTION_INTERVAL,
    NotificationManager,
    PullPointManager,
)

WSDL_DIR = os.path.join(os.path.dirname(onvif.__file__), "wsdl")


def _device() -> ONVIFCamera:
    """Build a minimal ONVIFCamera for manager construction tests."""
    return ONVIFCamera("127.0.0.1", 80, "user", "pass", wsdl_dir=WSDL_DIR)


@pytest.mark.asyncio
async def test_base_manager_floors_short_interval() -> None:
    """A 10-second interval is raised to the 60-second minimum."""
    manager = PullPointManager(
        _device(), dt.timedelta(seconds=10), lambda: None
    )
    assert manager._interval == MINIMUM_SUBSCRIPTION_INTERVAL


@pytest.mark.asyncio
async def test_base_manager_preserves_minimum_interval() -> None:
    """An interval equal to the minimum is preserved."""
    manager = PullPointManager(
        _device(), MINIMUM_SUBSCRIPTION_INTERVAL, lambda: None
    )
    assert manager._interval == MINIMUM_SUBSCRIPTION_INTERVAL


@pytest.mark.asyncio
async def test_base_manager_does_not_cap_long_interval() -> None:
    """Intervals above the minimum are kept verbatim.

    Regression test for the inverted clamp introduced in the original
    TopicFilter PR which capped intervals to 60s instead of flooring at 60s.
    """
    five_minutes = dt.timedelta(minutes=5)
    manager = PullPointManager(_device(), five_minutes, lambda: None)
    assert manager._interval == five_minutes


@pytest.mark.asyncio
async def test_notification_manager_inherits_interval_floor() -> None:
    """The floor applies to NotificationManager too."""
    manager = NotificationManager(
        _device(),
        "http://example.com/onvif/notify",
        dt.timedelta(seconds=5),
        lambda: None,
    )
    assert manager._interval == MINIMUM_SUBSCRIPTION_INTERVAL


@pytest.mark.asyncio
async def test_pullpoint_manager_defaults_have_no_topic_filter() -> None:
    """Default construction leaves ``_topic_filter`` unset."""
    manager = PullPointManager(
        _device(), dt.timedelta(seconds=60), lambda: None
    )
    assert manager._topic_filter is None


@pytest.mark.asyncio
async def test_pullpoint_manager_stores_topic_filter_and_default_dialect() -> None:
    """A topic filter is recorded with the ConcreteSet default dialect."""
    manager = PullPointManager(
        _device(),
        dt.timedelta(seconds=60),
        lambda: None,
        topic_filter="tns1:RuleEngine/CellMotionDetector/Motion",
    )
    assert manager._topic_filter == "tns1:RuleEngine/CellMotionDetector/Motion"
    assert manager._topic_filter_dialect == (
        "http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet"
    )


@pytest.mark.asyncio
async def test_pullpoint_manager_stores_custom_dialect() -> None:
    """A caller can override the WS-Topic dialect URI."""
    full_dialect = "http://docs.oasis-open.org/wsn/t-1/TopicExpression/Full"
    manager = PullPointManager(
        _device(),
        dt.timedelta(seconds=60),
        lambda: None,
        topic_filter="tns1:RuleEngine//.",
        topic_filter_dialect=full_dialect,
    )
    assert manager._topic_filter_dialect == full_dialect


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
@pytest.mark.asyncio
async def test_pullpoint_manager_rejects_blank_topic_filter(blank: str) -> None:
    """Blank topic filters are rejected up-front, not silently sent."""
    with pytest.raises(ValueError, match="topic_filter must be a non-empty"):
        PullPointManager(
            _device(),
            dt.timedelta(seconds=60),
            lambda: None,
            topic_filter=blank,
        )



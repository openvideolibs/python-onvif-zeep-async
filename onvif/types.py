"""ONVIF types."""

from datetime import datetime, timedelta, time
import ciso8601
from zeep.xsd.types.builtins import DateTime, treat_whitespace, Time
import isodate


def _try_parse_datetime(value: str) -> datetime | None:
    try:
        return ciso8601.parse_datetime(value)
    except ValueError:
        pass

    try:
        return isodate.parse_datetime(value)
    except ValueError:
        pass

    return None


def _try_fix_time_overflow(time: str) -> tuple[str, dict[str, int]]:
    """Some camera will overflow time so we need to fix it.

    To do this we calculate the offset beyond the maximum value
    and then add it to the current time as a timedelta.
    """
    offset: dict[str, int] = {}
    hour = int(time[0:2])
    if hour > 23:
        offset["hours"] = hour - 23
        hour = 23
    minute = int(time[3:5])
    if minute > 59:
        offset["minutes"] = minute - 59
        minute = 59
    second = int(time[6:8])
    if second > 59:
        offset["seconds"] = second - 59
        second = 59
    time_trailer = time[8:]
    return f"{hour:02d}:{minute:02d}:{second:02d}{time_trailer}", offset


# see https://github.com/mvantellingen/python-zeep/pull/1370
class FastDateTime(DateTime):
    """Fast DateTime that supports timestamps with - instead of T."""

    @treat_whitespace("collapse")
    def pythonvalue(self, value: str) -> datetime:
        """Convert the xml value into a python value."""
        if len(value) > 10 and value[10] == "-":  # 2010-01-01-00:00:00...
            value[10] = "T"
        if len(value) > 10 and value[11] == "-":  # 2023-05-15T-07:10:32Z...
            value = value[:11] + value[12:]
        # Determine based on the length of the value if it only contains a date
        # lazy hack ;-)
        if len(value) == 10:
            value += "T00:00:00"
        elif (len(value) in (19, 20, 26)) and value[10] == " ":
            value = "T".join(value.split(" "))

        if dt := _try_parse_datetime(value):
            return dt

        # Some cameras overflow the hours/minutes/seconds
        # For example, 2024-08-17T00:61:16Z so we need
        # to fix the overflow
        date, _, time = value.partition("T")
        try:
            fixed_time, offset = _try_fix_time_overflow(time)
        except ValueError:
            return ciso8601.parse_datetime(value)

        if dt := _try_parse_datetime(f"{date}T{fixed_time}"):
            return dt + timedelta(**offset)

        return ciso8601.parse_datetime(value)


class ForgivingTime(Time):
    """ForgivingTime."""

    @treat_whitespace("collapse")
    def pythonvalue(self, value: str) -> time:
        try:
            return isodate.parse_time(value)
        except ValueError:
            pass

        # Some cameras overflow the hours/minutes/seconds
        # For example, 00:61:16Z so we need
        # to fix the overflow
        try:
            fixed_time, offset = _try_fix_time_overflow(value)
        except ValueError:
            return isodate.parse_time(value)
        if fixed_dt := _try_parse_datetime(f"2024-01-15T{fixed_time}Z"):
            return (fixed_dt + timedelta(**offset)).time()
        return isodate.parse_time(value)

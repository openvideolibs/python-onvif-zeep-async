"""ONVIF types."""


import ciso8601
import isodate
from zeep.xsd.types.builtins import DateTime, treat_whitespace


# see https://github.com/mvantellingen/python-zeep/pull/1370
class FastDateTime(DateTime):
    """Fast DateTime that supports timestamps with - instead of T."""

    @treat_whitespace("collapse")
    def pythonvalue(self, value):
        """Convert the xml value into a python value."""
        try:
            return ciso8601.parse_datetime(value)
        except ValueError:
            pass
        # Determine based on the length of the value if it only contains a date
        # lazy hack ;-)

        if len(value) == 10:
            value += "T00:00:00"
        elif (len(value) == 19 or len(value) == 26) and value[10] == " ":
            value = "T".join(value.split(" "))
        elif len(value) > 10 and value[10] == "-":  # 2010-01-01-00:00:00...
            value[10] = "T"
        return isodate.parse_datetime(value)

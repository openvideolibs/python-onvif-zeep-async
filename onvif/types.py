"""ONVIF types."""


import ciso8601
from zeep.xsd.types.builtins import DateTime, treat_whitespace


# see https://github.com/mvantellingen/python-zeep/pull/1370
class FastDateTime(DateTime):
    """Fast DateTime that supports timestamps with - instead of T."""

    @treat_whitespace("collapse")
    def pythonvalue(self, value):
        """Convert the xml value into a python value."""
        if len(value) > 10 and value[10] == "-":  # 2010-01-01-00:00:00...
            value[10] = "T"
        if len(value) > 10 and value[11] == "-":  # 2023-05-15T-07:10:32Z...
            value = value[:11] + value[12:]

        try:
            return ciso8601.parse_datetime(value)
        except ValueError:
            pass

        return super().pythonvalue(value)

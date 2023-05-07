"""ONVIF settings."""

from zeep.client import Settings

DEFAULT_SETTINGS = Settings()
DEFAULT_SETTINGS.strict = False
DEFAULT_SETTINGS.xml_huge_tree = True

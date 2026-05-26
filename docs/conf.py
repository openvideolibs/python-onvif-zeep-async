"""Sphinx configuration for python-onvif-zeep-async documentation."""

from pathlib import Path

# -- Project information -----------------------------------------------------

project = "python-onvif-zeep-async"
author = "Cherish Chen"
copyright = "Cherish Chen"  # noqa: A001

_version_path = Path(__file__).parent / ".." / "onvif" / "version.txt"
with _version_path.open(encoding="utf-8") as _version_file:
    release = _version_file.read().strip()
version = release

# -- General configuration ---------------------------------------------------

extensions = []

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Options for HTML output -------------------------------------------------

html_theme = "sphinx_rtd_theme"

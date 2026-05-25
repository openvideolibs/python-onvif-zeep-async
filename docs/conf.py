"""Sphinx configuration for python-onvif-zeep-async documentation."""

import os

# -- Project information -----------------------------------------------------

project = "python-onvif-zeep-async"
author = "Cherish Chen"
copyright = "Cherish Chen"  # noqa: A001

_version_path = os.path.join(os.path.dirname(__file__), "..", "onvif", "version.txt")
with open(_version_path) as _version_file:
    release = _version_file.read().strip()
version = release

# -- General configuration ---------------------------------------------------

extensions = []

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Options for HTML output -------------------------------------------------

html_theme = "sphinx_rtd_theme"

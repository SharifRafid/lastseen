"""lastseen: a private location history for your Apple devices (works with iCloud Find My)."""

from __future__ import annotations

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("lastseen")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0"

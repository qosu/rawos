"""kernel/arch/base — rawos's ABI.

These Protocols are the ONLY interface the rawos kernel uses to talk to
the host. Every arch backend (linux.py, macos.py, windows.py, and any
future backend) implements them exactly. The kernel never calls a raw
OS command directly.
"""
from __future__ import annotations

from typing import Protocol


class ResourceProbe(Protocol):
    def disk_percent(self, path: str) -> int | None:
        """Return disk usage percent for `path`, or None if the probe failed."""
        ...

"""kernel/arch/detect — which arch backend is rawos running on.

current_os() reads sys.platform by default. Settings.arch_override
(env ARCH_OVERRIDE) forces a specific OS regardless of host — used by
Stage A/B tests so the same suite proves backend behavior on any host.
"""
from __future__ import annotations

import sys
from enum import Enum

from rawos.config import Settings


class OS(str, Enum):
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"


def current_os(settings: Settings | None = None) -> OS:
    """Return the OS enum for the active arch backend.

    settings.arch_override (if set) takes precedence over sys.platform.
    """
    if settings is None:
        settings = Settings()

    if settings.arch_override:
        return OS(settings.arch_override.lower())

    platform = sys.platform
    if platform.startswith("linux"):
        return OS.LINUX
    if platform == "darwin":
        return OS.MACOS
    if platform == "win32":
        return OS.WINDOWS

    raise ValueError(f"unsupported platform for rawos arch backend: {platform!r}")

"""rawos kernel/arch — the driver layer.

Linux/macOS/Windows are arch backends, exactly like Linux kernel's
arch/x86_64, arch/arm64, arch/riscv: interchangeable substrates the
rawos kernel targets. This package's Protocols (kernel/arch/base.py)
are rawos's stable ABI — the kernel never calls a raw OS command
directly, only through these.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from rawos.config import Settings
from rawos.kernel.arch.base import LogReader, ResourceProbe, ServiceManager
from rawos.kernel.arch.detect import OS, current_os
from rawos.kernel.arch.linux import LinuxLogReader, LinuxResourceProbe, LinuxServiceManager


@dataclass(frozen=True)
class Backend:
    resource_probe: ResourceProbe
    service_manager: ServiceManager
    log_reader: LogReader


@lru_cache(maxsize=None)
def _build_backend(os_: OS) -> Backend:
    if os_ == OS.LINUX:
        return Backend(
            resource_probe=LinuxResourceProbe(),
            service_manager=LinuxServiceManager(),
            log_reader=LinuxLogReader(),
        )
    if os_ == OS.MACOS:
        raise NotImplementedError("macOS arch backend not yet implemented (Stage B)")
    raise NotImplementedError("Windows arch backend not yet implemented (Stage C)")


def get_arch(settings: Settings | None = None) -> Backend:
    return _build_backend(current_os(settings))


__all__ = ["OS", "current_os", "get_arch", "Backend"]

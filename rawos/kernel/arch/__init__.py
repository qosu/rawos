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
from rawos.kernel.arch.base import (
    CrashReporter,
    FileOperator,
    KernelObserver,
    LogReader,
    ResourceProbe,
    ServiceManager,
    ShellPolicy,
)
from rawos.kernel.arch.detect import OS, current_os
from rawos.kernel.arch.linux import (
    LinuxCrashReporter,
    LinuxFileOperator,
    LinuxKernelObserver,
    LinuxLogReader,
    LinuxResourceProbe,
    LinuxServiceManager,
    LinuxShellPolicy,
)
from rawos.kernel.arch.macos import (
    MacOSCrashReporter,
    MacOSFileOperator,
    MacOSKernelObserver,
    MacOSLogReader,
    MacOSResourceProbe,
    MacOSServiceManager,
    MacOSShellPolicy,
)
from rawos.kernel.arch.windows import (
    WindowsCrashReporter,
    WindowsFileOperator,
    WindowsKernelObserver,
    WindowsLogReader,
    WindowsResourceProbe,
    WindowsServiceManager,
    WindowsShellPolicy,
)


@dataclass(frozen=True)
class Backend:
    resource_probe: ResourceProbe
    service_manager: ServiceManager
    log_reader: LogReader
    shell_policy: ShellPolicy
    crash_reporter: CrashReporter
    file_operator: FileOperator
    kernel_observer: KernelObserver


@lru_cache(maxsize=None)
def _build_backend(os_: OS) -> Backend:
    if os_ == OS.LINUX:
        return Backend(
            resource_probe=LinuxResourceProbe(),
            service_manager=LinuxServiceManager(),
            log_reader=LinuxLogReader(),
            shell_policy=LinuxShellPolicy(),
            crash_reporter=LinuxCrashReporter(),
            file_operator=LinuxFileOperator(),
            kernel_observer=LinuxKernelObserver(),
        )
    if os_ == OS.MACOS:
        return Backend(
            resource_probe=MacOSResourceProbe(),
            service_manager=MacOSServiceManager(),
            log_reader=MacOSLogReader(),
            shell_policy=MacOSShellPolicy(),
            crash_reporter=MacOSCrashReporter(),
            file_operator=MacOSFileOperator(),
            kernel_observer=MacOSKernelObserver(),
        )
    return Backend(
        resource_probe=WindowsResourceProbe(),
        service_manager=WindowsServiceManager(),
        log_reader=WindowsLogReader(),
        shell_policy=WindowsShellPolicy(),
        crash_reporter=WindowsCrashReporter(),
        file_operator=WindowsFileOperator(),
        kernel_observer=WindowsKernelObserver(),
    )


def get_arch(settings: Settings | None = None) -> Backend:
    return _build_backend(current_os(settings))


__all__ = ["OS", "current_os", "get_arch", "Backend"]

"""kernel/arch/linux — the Linux arch backend.

Today's complete backend build. Reproduces, byte-for-byte, the
commands previously inlined in context/server_scanner.py and
kernel/sandbox.py — Stage A is a zero-behavior-change extraction.
"""
from __future__ import annotations

import subprocess


class LinuxResourceProbe:
    def disk_percent(self, path: str) -> int | None:
        try:
            r = subprocess.run(
                ["df", path, "--output=pcent"],
                capture_output=True, text=True, timeout=3.0,
            )
        except Exception:
            return None
        if r.returncode != 0:
            return None
        pct_str = r.stdout.strip().splitlines()[-1].strip().rstrip("%")
        return int(pct_str)

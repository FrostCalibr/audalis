"""Small shared helpers (command execution, integer parsing)."""

from __future__ import annotations

import dataclasses
import subprocess
from typing import Sequence


@dataclasses.dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def err_text(self) -> str:
        return (self.stderr or self.stdout or "").strip()


def run_cmd(args: Sequence[str], timeout: float = 6.0) -> CommandResult:
    """Run a command, capturing output. Never raises; failures return rc != 0."""
    try:
        p = subprocess.run(
            [str(a) for a in args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(p.returncode, p.stdout or "", p.stderr or "")
    except Exception as exc:  # noqa: BLE001 - degradation is the plan
        return CommandResult(1, "", str(exc))


def nint(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def is_available_on_path(name: str) -> bool:
    import shutil

    return shutil.which(name) is not None
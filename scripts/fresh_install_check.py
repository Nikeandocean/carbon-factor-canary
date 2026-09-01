#!/usr/bin/env python3
"""Fresh-install canary: install the latest PyPI release into a brand-new
virtual environment and import the server.

Catches dependency-resolution breakage that a stale dev environment cannot
see -- the 2026-08 mcp-2.x failure mode, where a long-lived venv kept tests
green while every fresh user install crashed at startup.

Exit code 0 = install + import OK; 1 = failure.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

PACKAGE = "carbon-factor-matcher"
IMPORT_PROBE = (
    "import carbon_factor_matcher.server as s; "
    "from carbon_factor_matcher import __version__; "
    "print('import OK, version', __version__)"
)


def log(msg: str) -> None:
    print(f"[canary] {msg}", flush=True)


def venv_python(venv_dir: Path) -> Path:
    sub = "Scripts" if sys.platform == "win32" else "bin"
    exe = "python.exe" if sys.platform == "win32" else "python"
    return venv_dir / sub / exe


def run(cmd: list[str], desc: str, timeout_s: float) -> str:
    log(f"{desc}: {' '.join(cmd)}")
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    elapsed = time.monotonic() - t0
    if proc.returncode != 0:
        log(f"{desc} FAILED after {elapsed:.0f}s (exit {proc.returncode})")
        log(f"stdout tail:\n{proc.stdout[-1500:]}")
        log(f"stderr tail:\n{proc.stderr[-1500:]}")
        sys.exit(1)
    log(f"{desc} OK in {elapsed:.0f}s")
    return proc.stdout


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cfm-canary-") as tmp:
        venv_dir = Path(tmp) / "venv"
        py = venv_python(venv_dir)

        run([sys.executable, "-m", "venv", str(venv_dir)],
            "create fresh venv", timeout_s=120)

        # Real install (not --dry-run): resolution is only half the story;
        # the mcp 2.x disaster passed resolution fine and died at import.
        run([str(py), "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", PACKAGE],
            "pip install latest from PyPI", timeout_s=900)

        versions = run([str(py), "-m", "pip", "show", PACKAGE, "mcp"],
                       "collect resolved versions", timeout_s=60)
        for line in versions.splitlines():
            if line.startswith(("Name:", "Version:")):
                log(line)

        run([str(py), "-c", IMPORT_PROBE], "import server module",
            timeout_s=120)

    log("FRESH INSTALL CANARY PASSED")


if __name__ == "__main__":
    main()

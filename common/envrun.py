"""Load .env into the environment, set DD_VERSION to the git sha, then exec a command.

Usage: python -m common.envrun <service> <command> [args...]
Sourcing .env in a shell breaks on URLs with '&'; this parses it safely instead.
"""
from __future__ import annotations

import os
import subprocess
import sys

from dotenv import dotenv_values

from common.settings import ROOT


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: python -m common.envrun <service> <command> [args...]")
    service, cmd = sys.argv[1], sys.argv[2:]
    for key, val in dotenv_values(ROOT / ".env").items():
        if val is not None and key not in os.environ:
            os.environ[key] = val
    if "DD_VERSION" not in os.environ:
        try:
            sha = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            sha = "dev"
        os.environ["DD_VERSION"] = sha or "dev"
    os.environ.setdefault("DD_SERVICE", service)
    # Separate MongoDB service per process: the worker's change-stream getMore waits up to
    # 500 ms by design and must not skew the gateway's MongoDB latency.
    os.environ.setdefault("DD_PYMONGO_SERVICE", f"{service}-mongodb")
    os.environ.setdefault("DD_ENV", "hackathon")
    os.environ.setdefault("DD_LOGS_INJECTION", "true")
    os.environ.setdefault("DD_AGENT_HOST", "localhost")
    os.environ["PYTHONPATH"] = str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
    os.chdir(ROOT)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()

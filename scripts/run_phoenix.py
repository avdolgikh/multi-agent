from __future__ import annotations

import os
import subprocess
import sys


def main() -> None:
    """Launch Arize Phoenix on http://localhost:6006.

    Phoenix's CLI reads host/port from env vars (PHOENIX_HOST, PHOENIX_PORT),
    not from `serve` flags, so we inject defaults if the caller hasn't set
    them.
    """

    env = os.environ.copy()
    env.setdefault("PHOENIX_HOST", "127.0.0.1")
    env.setdefault("PHOENIX_PORT", "6006")

    command = [sys.executable, "-m", "phoenix.server.main", "serve"]
    try:
        subprocess.run(command, check=True, env=env)
    except KeyboardInterrupt:  # pragma: no cover - manual shutdown
        pass


if __name__ == "__main__":
    main()

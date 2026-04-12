from __future__ import annotations

import subprocess
import sys


def main() -> None:
    """Launch Arize Phoenix on http://localhost:6006."""

    command = [
        sys.executable,
        "-m",
        "phoenix.server.main",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "6006",
    ]
    try:
        subprocess.run(command, check=True)
    except KeyboardInterrupt:  # pragma: no cover - manual shutdown
        pass


if __name__ == "__main__":
    main()

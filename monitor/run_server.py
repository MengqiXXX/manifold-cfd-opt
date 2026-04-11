from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    host = os.getenv("MONITOR_HOST", "0.0.0.0")
    port = int(os.getenv("MONITOR_PORT", "8090"))
    uvicorn.run("monitor.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()

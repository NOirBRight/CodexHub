import os
from pathlib import Path
import subprocess
import sys
import time


def main() -> int:
    marker_prefix = Path(sys.argv[1])
    if len(sys.argv) == 3 and sys.argv[2] == "intermediate":
        child = subprocess.Popen(
            ["ping.exe", "127.0.0.1", "-t"],
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        Path(f"{marker_prefix}.{os.getpid()}").write_text(
            str(child.pid), encoding="ascii"
        )
        return 0

    for _ in range(6):
        intermediate = subprocess.Popen(
            [sys.executable, __file__, str(marker_prefix), "intermediate"]
        )
        intermediate.wait(timeout=5)
    time.sleep(3600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    mode = sys.argv[1]
    pid_log = Path(sys.argv[2])
    if len(sys.argv) == 4 and sys.argv[3] == "intermediate":
        inherited = mode == "inherited-pipe"
        child = subprocess.Popen(
            ["ping.exe", "127.0.0.1", "-t"],
            stdout=None if inherited else subprocess.DEVNULL,
            stderr=None if inherited else subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        pid_log.write_text(f"{os.getpid()}\n{child.pid}\n", encoding="ascii")
        return 0

    intermediate = subprocess.Popen(
        [sys.executable, __file__, mode, str(pid_log), "intermediate"]
    )
    return intermediate.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Start Camoufox browser server if not already running."""
import subprocess
import time
import urllib.request

CAMOFOX_DIR = "/home/deyaan666/camofox-browser"
SERVER_URL = "http://localhost:9377"


def is_running() -> bool:
    try:
        urllib.request.urlopen(SERVER_URL, timeout=2)
        return True
    except Exception:
        return False


def main():
    if is_running():
        print("Camoufox already running on :9377")
        return 0

    print("Starting Camoufox server...")
    proc = subprocess.Popen(
        ["npm", "start"],
        cwd=CAMOFOX_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait up to 10s for server to respond
    for _ in range(10):
        time.sleep(1)
        if is_running():
            print(f"Camoufox started OK (pid={proc.pid})")
            return 0

    print(f"Camoufox failed to start (pid={proc.pid})")
    return 1


if __name__ == "__main__":
    exit(main())

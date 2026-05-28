#!/usr/bin/env python3
"""Check if Camoufox browser server is running on localhost:9377."""
import urllib.request
import json


def main():
    try:
        req = urllib.request.Request(
            "http://localhost:9377/",
            method="GET",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            status = data.get("status", "unknown")
            version = data.get("version", "unknown")
            print(f"Camoufox: {status} | v{version}")
            return 0
    except urllib.error.URLError as e:
        print(f"Camoufox: OFFLINE ({e.reason})")
        return 1
    except Exception as e:
        print(f"Camoufox: ERROR ({e})")
        return 1


if __name__ == "__main__":
    exit(main())

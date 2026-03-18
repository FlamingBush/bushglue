#!/usr/bin/env python3
"""
Poll a URL until it returns a 2xx response.
Usage: wait-for-http.py <url> <name> [timeout_seconds]
Exits 0 on success, 1 on timeout.
"""
import sys
import time
import urllib.request
import urllib.error

def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <url> <name> [timeout]", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    name = sys.argv[2]
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 120

    print(f"Waiting for {name} at {url}...", flush=True)
    for i in range(timeout):
        try:
            urllib.request.urlopen(url, timeout=1)
            print(f"{name} ready (took ~{i}s)", flush=True)
            sys.exit(0)
        except (urllib.error.URLError, OSError):
            time.sleep(1)

    print(f"ERROR: {name} did not become ready within {timeout}s", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()

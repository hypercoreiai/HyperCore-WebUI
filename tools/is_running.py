#!/usr/bin/env python3
"""tools/is_running.py - is a HyperCore/Strata web app already answering on this port?

One engine per machine, so run.sh and run.bat check before starting: a port in use
is not necessarily ours, and starting a second copy of a tens-of-GB model would
fail rather than just annoy. /health is the smallest thing both servers answer, and
it is answered even when no model is loaded.

    python tools/is_running.py 8081 [host]

Exit 0 when something here is a HyperCore/Strata server, 1 otherwise.
"""
import socket
import sys


def is_running(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), 2.0) as sock:
            sock.sendall(b"GET /health HTTP/1.0\r\n\r\n")
            body = sock.makefile("rb").read().decode("utf-8", "replace")
    except OSError:
        return False
    return '"status"' in body or '"max_context"' in body


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else 8081
    host = argv[2] if len(argv) > 2 else "127.0.0.1"
    return 0 if is_running(port, host) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

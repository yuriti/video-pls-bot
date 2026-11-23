"""Readiness gate for the local proxy and Bot API systemd units."""

import socket
import sys
import time

ports = [int(value) for value in sys.argv[1:]]
deadline = time.monotonic() + 12
pending = set(ports)
while pending and time.monotonic() < deadline:
    for port in tuple(pending):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pending.remove(port)
        except OSError:
            pass
    if pending:
        time.sleep(0.1)
if pending:
    raise SystemExit("Local service did not become ready")

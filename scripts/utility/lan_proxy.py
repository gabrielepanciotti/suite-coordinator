#!/usr/bin/env python3
"""Minimal HTTP/HTTPS forward proxy: a bridge into the work instance LAN.

Why this exists: when you work from outside the company network (VS Code
Remote-SSH into a dev box over a VPN, for instance) the internal hosts
(`app.corp.example` & co.) are unreachable from the
laptop's browser — they resolve to private addresses only the LAN can route.

Run this on a machine that IS on the LAN, point the browser at it, and every
request — DNS resolution included — happens on that side. Unlike a per-host
port-forward this keeps the real hostnames, so redirects and OIDC SSO
round-trips (app -> Hub Offerte -> back) work untouched.

Bound to 127.0.0.1 on purpose: the only ways in are a local process or an SSH
tunnel, never the network. Run it in the foreground and Ctrl-C to stop; there is
no daemon mode and nothing is persisted.

Usage:  .venv/bin/python scripts/utility/lan_proxy.py [--port 8888]
"""

from __future__ import annotations

import argparse
import select
import socket
import socketserver
import sys
import threading
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import local_defaults  # noqa: E402

BUFFER = 65536
TIMEOUT = 30
# Which LAN domains the proxy may tunnel to: the installation's, from
# locale/defaults.json. Empty = nothing is allowed (fail closed).
ALLOWED_SUFFIXES = local_defaults.get_list("LAN_PROXY_SUFFISSI")


def pump(a: socket.socket, b: socket.socket) -> None:
    """Shuttle bytes both ways until either side closes."""
    sockets = [a, b]
    try:
        while True:
            readable, _, errored = select.select(sockets, [], sockets, TIMEOUT)
            if errored or not readable:
                return
            for source in readable:
                data = source.recv(BUFFER)
                if not data:
                    return
                (b if source is a else a).sendall(data)
    except OSError:
        return


class ProxyHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request = self.rfile.readline(BUFFER).decode("latin-1").strip()
        if not request:
            return
        try:
            method, target, _ = request.split(" ", 2)
        except ValueError:
            return

        if method.upper() == "CONNECT":  # HTTPS
            host, _, port = target.partition(":")
            self.tunnel(host, int(port or 443), preamble=b"")
            return

        # Plain HTTP: rebuild the request with an origin-form path.
        parsed = urllib.parse.urlsplit(target)
        host, port = parsed.hostname or "", parsed.port or 80
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        headers = [f"{method} {path} HTTP/1.1"]
        while (line := self.rfile.readline(BUFFER).decode("latin-1").rstrip("\r\n")):
            if not line.lower().startswith("proxy-connection:"):
                headers.append(line)
        self.tunnel(host, port, preamble=("\r\n".join(headers) + "\r\n\r\n").encode("latin-1"))

    def tunnel(self, host: str, port: int, preamble: bytes) -> None:
        if not ALLOWED_SUFFIXES or not host.endswith(ALLOWED_SUFFIXES):
            ammessi = ", ".join(f"*{s}" for s in ALLOWED_SUFFIXES) or "nessuno"
            self.wfile.write(f"HTTP/1.1 403 Forbidden\r\n\r\nhost ammessi: {ammessi}\r\n".encode())
            return
        try:
            upstream = socket.create_connection((host, port), timeout=TIMEOUT)
        except OSError as exc:
            self.wfile.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{host}:{port} — {exc}\r\n".encode())
            return
        with upstream:
            if preamble:
                upstream.sendall(preamble)
            else:
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            pump(self.connection, upstream)

    def handle_error(self, *_: object) -> None:  # noqa: D102 - browsers abort constantly
        pass


class ThreadedProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, *_: object) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()

    with ThreadedProxy(("127.0.0.1", args.port), ProxyHandler) as server:
        print(f"proxy LAN su http://127.0.0.1:{args.port} — Ctrl-C per fermarlo")
        print("host ammessi:", ", ".join(f"*{s}" for s in ALLOWED_SUFFIXES) or "nessuno (LAN_PROXY_SUFFISSI)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nfermato")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

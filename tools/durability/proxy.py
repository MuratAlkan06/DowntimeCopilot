"""Threaded TCP proxy in front of SYNC_ENDPOINT_URL (docs/durability.md "Proxy modes"). Stdlib only.

Modes, chosen per connection at accept time; set_mode() also cuts every live connection, so a pooled
keep-alive socket cannot carry traffic past a mode change:

  pass          forward both ways
  refuse        listener closed: connection refused (endpoint down, F7a)
  drop          accept, then close at once (connection cut mid-drain, F3)
  hold          accept, read and discard, never forward or answer: the worker freezes inside put_item until
                botocore gives up (connect 2 s, read 5 s, total_max_attempts=2), the deterministic SIGKILL
                window of F4
  fwd_drop      forward the request, let the upstream answer land, then close without relaying it: the put
                is applied but the client sees an error, so the outbox row survives and the restart re-puts
                the same key (F4 overwrite exclusion)
  deny          answer every request with an AWS-shaped AccessDeniedException (HTTP 400, awsJson1.0):
                the tiny DynamoDB fake of F7b
"""
from __future__ import annotations

import json
import socket
import threading

MODES = ("pass", "refuse", "drop", "hold", "fwd_drop", "deny")

DENY_BODY = json.dumps({"__type": "com.amazonaws.dynamodb.v20120810#AccessDeniedException",
                        "message": "durability harness: simulated AccessDenied"}).encode()


def _http_request_end(buf: bytes) -> int | None:
    """Byte length of the first complete HTTP request in `buf` (Content-Length framing), else None."""
    head_end = buf.find(b"\r\n\r\n")
    if head_end < 0:
        return None
    length = 0
    for line in buf[:head_end].split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip() or 0)
    end = head_end + 4 + length
    return end if len(buf) >= end else None


class Proxy:
    def __init__(self, listen_port: int, upstream_port: int, host: str = "127.0.0.1") -> None:
        self.host, self.port, self.upstream = host, listen_port, (host, upstream_port)
        self.mode = "pass"
        self.stats = {"accepted": 0, "forwarded": 0, "held": 0, "dropped": 0, "denied": 0, "fwd_dropped": 0}
        self._lock = threading.Lock()
        self._live: set[socket.socket] = set()
        self._listener: socket.socket | None = None
        self._closed = False

    # ------------------------------------------------------------ control
    def start(self) -> "Proxy":
        self._listen()
        return self

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        with self._lock:
            self.mode = mode
        self.cut()
        if mode == "refuse":
            self._unlisten()
        elif self._listener is None:
            self._listen()

    def cut(self) -> None:
        """Close every live connection (both legs)."""
        with self._lock:
            live, self._live = list(self._live), set()
        for s in live:
            _close(s)

    def close(self) -> None:
        self._closed = True
        self._unlisten()
        self.cut()

    def bump(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    # ------------------------------------------------------------ plumbing
    def _listen(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(64)
        self._listener = s
        threading.Thread(target=self._accept_loop, args=(s,), daemon=True, name="dur-proxy-accept").start()

    def _unlisten(self) -> None:
        s, self._listener = self._listener, None
        if s is not None:
            _close(s)

    def _track(self, *socks: socket.socket) -> None:
        with self._lock:
            self._live.update(socks)

    def _accept_loop(self, listener: socket.socket) -> None:
        while not self._closed:
            try:
                conn, _ = listener.accept()
            except OSError:
                return  # listener closed (refuse mode or shutdown)
            self.bump("accepted")
            threading.Thread(target=self._serve, args=(conn, self.mode), daemon=True).start()

    def _serve(self, conn: socket.socket, mode: str) -> None:
        self._track(conn)
        try:
            if mode == "drop":
                self.bump("dropped")
                return
            if mode == "hold":
                self.bump("held")
                while conn.recv(65536):
                    pass
                return
            if mode == "deny":
                self._deny(conn)
                return
            up = socket.create_connection(self.upstream, timeout=10)
            up.settimeout(None)
            self._track(up)
            if mode == "fwd_drop":
                self._forward_then_drop(conn, up)
                return
            t = threading.Thread(target=_pipe, args=(up, conn), daemon=True)
            t.start()
            self.bump("forwarded")
            _pipe(conn, up)
            t.join(timeout=30)
        except OSError:
            pass
        finally:
            _close(conn)

    def _deny(self, conn: socket.socket) -> None:
        buf = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
            while (end := _http_request_end(buf)) is not None:
                buf = buf[end:]
                self.bump("denied")
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/x-amz-json-1.0\r\n"
                             b"x-amzn-ErrorType: AccessDeniedException\r\nx-amzn-RequestId: dur-harness\r\n"
                             b"Content-Length: " + str(len(DENY_BODY)).encode() + b"\r\n\r\n" + DENY_BODY)

    def _forward_then_drop(self, conn: socket.socket, up: socket.socket) -> None:
        buf = b""
        while (end := _http_request_end(buf)) is None:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        up.sendall(buf[:end])
        self.bump("forwarded")
        up.recv(65536)  # the upstream applied the request and started answering; never relay it
        self.bump("fwd_dropped")
        _close(up)


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while chunk := src.recv(65536):
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        _close(dst)


def _close(s: socket.socket) -> None:
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

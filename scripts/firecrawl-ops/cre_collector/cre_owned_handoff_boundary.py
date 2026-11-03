"""Ephemeral, owner-served native mutation checks over a private Unix socket.

The socket carries no reusable approval: every request causes the live owner to
recheck its validated evidence, policy, clocks and lease before replying.
"""

import json
import os
import re
import secrets
import socket
import struct
import sys
import time
from pathlib import Path

from cre_code_handoff_evidence import require

MAX_FRAME = 8192
IO_SECONDS = 30


def deactivate_preserving_error(boundary):
    """Do not replace a native failure with a secondary socket-close failure."""
    original = sys.exc_info()[1]
    try:
        boundary.deactivate()
    except BaseException as cleanup_error:
        if original is None:
            raise
        if hasattr(original, "add_note"):
            original.add_note(f"owner boundary deactivation failed: {type(cleanup_error).__name__}")


def endpoint_for(owner_pid):
    require(type(owner_pid) is int and owner_pid > 0, "invalid owner PID")
    return Path("/tmp") / ("cre-owned-handoff-" + str(owner_pid)) / "socket"


def _frame(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    require(len(raw) <= MAX_FRAME, "owner boundary frame too large")
    return raw


def _peer(sock):
    return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))


def _parse(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "duplicate owner boundary field")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs)


class OwnerBoundary:
    """One live lease owner's endpoint, polled by its existing foreground wait."""

    def __init__(self, binding, check, verify_peer):
        self.binding = dict(binding)
        self.check = check
        self.verify_peer = verify_peer
        self.mode = None
        self.used = set()
        self.pending = None
        self.accepted_at = None
        self.command_deadline = None

    def __enter__(self):
        self.path = endpoint_for(os.getpid())
        self.directory = self.path.parent
        os.mkdir(self.directory, 0o700)
        self.directory_identity = (self.directory.stat().st_dev, self.directory.stat().st_ino)
        self.server = None
        try:
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self.socket_identity = (self.path.lstat().st_dev, self.path.lstat().st_ino)
            self.server.listen(4)
            self.server.setblocking(False)
        except BaseException:
            if self.server is not None:
                self.server.close()
            if self.path.exists() and not self.path.is_symlink():
                self.path.unlink()
            self.directory.rmdir()
            raise
        return self

    def __exit__(self, _kind, original, _traceback):
        try:
            if self.pending is not None:
                self.pending.close()
            self.server.close()
            if (not self.path.is_symlink() and self.path.exists()
                    and (self.path.lstat().st_dev, self.path.lstat().st_ino) == self.socket_identity):
                self.path.unlink()
            if (not self.directory.is_symlink() and self.directory.is_dir()
                    and (self.directory.stat().st_dev, self.directory.stat().st_ino) == self.directory_identity):
                self.directory.rmdir()
        except BaseException as cleanup_error:
            if original is None:
                raise
            if hasattr(original, "add_note"):
                original.add_note(f"owner boundary cleanup failed: {type(cleanup_error).__name__}")

    def activate(self, mode):
        require(mode in {"Hold", "Repin", "Verify", "Restore"}, "unsupported native boundary mode")
        self.mode = mode

    def deactivate(self):
        self.mode = None
        if self.pending is not None:
            self.pending.close()
            self.pending = None

    def poll(self):
        if self.pending is None:
            try:
                self.pending, _ = self.server.accept()
            except BlockingIOError:
                return
            self.pending.setblocking(False)
            self.accepted_at = time.monotonic()
        conn = self.pending
        if time.monotonic() - self.accepted_at >= IO_SECONDS:
            conn.close()
            self.pending = None
            return
        try:
            raw, _, flags, _ = conn.recvmsg(MAX_FRAME)
        except BlockingIOError:
            return
        try:
            require(raw and not flags & socket.MSG_TRUNC, "invalid owner boundary frame")
            request = _parse(raw)
            require(isinstance(request, dict) and set(request) == set(self.binding) | {"mode", "nonce"},
                    "owner boundary fields differ")
            nonce = request.get("nonce")
            require(isinstance(nonce, str) and re.fullmatch(r"[0-9a-f]{32}", nonce)
                    and nonce not in self.used and len(self.used) < 64, "owner boundary nonce differs")
            require(self.mode is not None and request.get("mode") == self.mode
                    and all(request.get(key) == value for key, value in self.binding.items()),
                    "owner boundary operation differs")
            pid, uid, _ = _peer(conn)
            require(pid > 0 and uid == os.geteuid() and self.verify_peer(pid, uid, request),
                    "owner boundary client differs")
            self.used.add(nonce)
            self.check(self.mode)
            require(self.command_deadline is None or time.monotonic() < self.command_deadline,
                    "owned command deadline expired")
            response = {"nonce": nonce, "ok": True}
        except Exception:  # noqa: BLE001
            response = {"ok": False}
        try:
            conn.send(_frame(response))
        except OSError:
            pass
        finally:
            conn.close()
            self.pending = None


def request_boundary(endpoint, owner_pid, binding, mode, check_owner, check_clock, check_deadline=lambda: None):
    """Verify exact owner socket and one current request; no approval is stored."""
    require(owner_pid > 0, "invalid owner PID")
    nonce = secrets.token_hex(16)
    request = {**binding, "mode": mode, "nonce": nonce}
    check_clock()
    check_owner()
    check_deadline()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock.settimeout(IO_SECONDS)
    try:
        sock.connect(str(endpoint))
        pid, uid, _ = _peer(sock)
        require(pid == owner_pid and uid == os.geteuid(), "owner boundary server differs")
        sock.sendall(_frame(request))
        raw, _, flags, _ = sock.recvmsg(MAX_FRAME)
        require(raw and not flags & socket.MSG_TRUNC, "owner boundary response missing")
        response = _parse(raw)
        require(response == {"nonce": nonce, "ok": True}, "owner boundary refused")
    finally:
        sock.close()
    check_clock()
    check_owner()
    check_deadline()

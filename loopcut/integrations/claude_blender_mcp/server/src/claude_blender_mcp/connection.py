"""Client for the addon's local socket server. No MCP or bpy imports, so tests can use it directly."""

import itertools
import select
import socket
import threading

from . import protocol


class BlenderError(Exception):
    """Blender ran the command and reported a failure."""

    def __init__(self, err):
        self.type = err.get("type", "Error")
        self.traceback = err.get("traceback")
        super().__init__(f"{self.type}: {err.get('message', '')}")


class BlenderUnavailable(Exception):
    """Could not reach the addon."""


class BlenderConnection:
    def __init__(self, host=protocol.DEFAULT_HOST, port=protocol.DEFAULT_PORT, connect_timeout=5.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._sock = None
        self._buf = b""
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _connect(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as exc:
            raise BlenderUnavailable(
                f"Cannot reach Blender at {self.host}:{self.port} ({exc}). Open Blender, enable the "
                "Claude Blender Integration addon and press Start Claude Bridge in the sidebar (N panel > Claude)."
            ) from exc
        self._sock = sock
        self._buf = b""

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _peer_closed(self):
        """True if Blender closed our idle socket (e.g. it restarted since the last call)."""
        try:
            readable, _, _ = select.select([self._sock], [], [], 0)
            if not readable:
                return False
            return self._sock.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _send(self, payload):
        if self._sock is not None and self._peer_closed():
            self.close()
        if self._sock is None:
            self._connect()
        self._sock.sendall(payload)

    def _receive(self, cmd, timeout):
        # Give Blender a little longer than the addon's own wait so its Timeout error arrives first.
        self._sock.settimeout(timeout + 10)
        try:
            while b"\n" not in self._buf:
                chunk = self._sock.recv(1 << 20)
                if not chunk:
                    raise ConnectionError("closed")
                self._buf += chunk
        except (OSError, ConnectionError) as exc:
            self.close()
            # The command may have run; never resend it automatically.
            raise BlenderUnavailable(
                f"Lost the connection to Blender while it ran {cmd!r} ({exc or type(exc).__name__}). "
                "Check scene_state before retrying."
            ) from exc
        line, self._buf = self._buf.split(b"\n", 1)
        return protocol.decode(line)

    def call(self, cmd, params=None, timeout=protocol.DEFAULT_TIMEOUT):
        request = {"id": next(self._ids), "cmd": cmd, "params": params or {}, "timeout": timeout}
        payload = protocol.encode(request)
        with self._lock:
            try:
                self._send(payload)
            except OSError:
                # Stale socket (Blender restarted). Nothing was delivered, so reconnect and send once more.
                self.close()
                self._send(payload)
            response = self._receive(cmd, timeout)
        if response.get("id") != request["id"]:
            self.close()
            raise BlenderUnavailable("Out-of-sync response from Blender; reconnecting on next call")
        if not response.get("ok"):
            raise BlenderError(response.get("error") or {})
        return response.get("result")

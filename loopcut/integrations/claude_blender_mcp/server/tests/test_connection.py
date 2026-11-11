import pathlib
import socket
import threading

import pytest

from claude_blender_mcp import protocol
from claude_blender_mcp.connection import BlenderConnection, BlenderError, BlenderUnavailable

REPO = pathlib.Path(__file__).resolve().parents[2]


class FakeAddon:
    """Line-delimited JSON server that answers with a canned function."""

    def __init__(self, answer):
        self.answer = answer
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.received = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        buf = b""
        with conn:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    req = protocol.decode(line)
                    self.received.append(req)
                    reply = self.answer(req)
                    if reply is None:
                        conn.close()
                        return
                    conn.sendall(protocol.encode(reply))

    def close(self):
        self.sock.close()


def test_protocol_copies_match():
    addon = (REPO / "addon/claude_blender/protocol.py").read_text()
    server = (REPO / "server/src/claude_blender_mcp/protocol.py").read_text()
    assert addon == server, "Keep addon and server protocol.py identical"


def test_roundtrip_and_ids():
    fake = FakeAddon(lambda r: protocol.ok(r["id"], {"echo": r["params"]}))
    conn = BlenderConnection(port=fake.port)
    assert conn.call("ping", {"a": 1}) == {"echo": {"a": 1}}
    assert conn.call("ping", {"a": 2}) == {"echo": {"a": 2}}
    assert [r["id"] for r in fake.received] == [1, 2]
    fake.close()


def test_error_is_raised_with_type():
    fake = FakeAddon(lambda r: protocol.error(r["id"], "CommandError", "No object named 'x'"))
    conn = BlenderConnection(port=fake.port)
    with pytest.raises(BlenderError, match="No object named"):
        conn.call("mesh_report", {"name": "x"})
    fake.close()


def test_unavailable_when_nothing_listens():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(BlenderUnavailable, match="Start Claude Bridge"):
        BlenderConnection(port=port, connect_timeout=1).call("ping")


def test_dropped_mid_command_is_not_resent():
    fake = FakeAddon(lambda r: None)  # close without answering
    conn = BlenderConnection(port=fake.port)
    with pytest.raises(BlenderUnavailable, match="Check scene_state"):
        conn.call("run_python", {"code": "x = 1"})
    assert len(fake.received) == 1
    fake.close()


def test_reconnects_after_peer_restart():
    fake = FakeAddon(lambda r: protocol.ok(r["id"], "first"))
    conn = BlenderConnection(port=fake.port)
    assert conn.call("ping") == "first"
    # Simulate Blender restarting: server-side connection drops, a new one answers.
    conn._sock.shutdown(socket.SHUT_WR)  # makes the fake close its end
    import time

    time.sleep(0.2)
    fake.answer = lambda r: protocol.ok(r["id"], "second")
    assert conn.call("ping") == "second"
    fake.close()


def test_to_jsonable_handles_iterables_and_named():
    class Named:
        name = "Cube"

    assert protocol.to_jsonable({"v": (1.0, 2.0), "o": Named(), "s": {3}}) == {"v": [1.0, 2.0], "o": "Cube", "s": [3]}

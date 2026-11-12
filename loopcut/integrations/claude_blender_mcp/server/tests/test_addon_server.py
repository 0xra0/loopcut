"""The addon's server runs any Python it is sent, so it answers only a client holding this user's token."""

import builtins
import pathlib
import sys
import threading
import time

import pytest

pytest.importorskip("bpy")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "addon"))
from claude_blender import protocol as addon_protocol  # noqa: E402
from claude_blender import server as addon_server  # noqa: E402

from claude_blender_mcp import protocol  # noqa: E402
from claude_blender_mcp.connection import BlenderConnection, BlenderError  # noqa: E402

MARK = "import builtins; builtins._claude_bridge_ran = True"


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(protocol, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(addon_protocol, "TOKEN_DIR", tmp_path)
    monkeypatch.delattr(builtins, "_claude_bridge_ran", raising=False)
    srv = addon_server.BridgeServer(port=0)
    srv.start(use_timer=False)
    yield srv
    srv.stop()


def call(srv, cmd, params=None):
    """Call from a client thread while this thread drains the queue, as Blender's timer would."""
    out = {}

    def run():
        try:
            out["result"] = BlenderConnection(port=srv.port).call(cmd, params)
        except BlenderError as exc:
            out["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 10
    while thread.is_alive() and time.monotonic() < deadline:
        srv.process_pending()
        time.sleep(0.01)
    thread.join(1)
    return out


def test_a_client_with_the_token_runs_code(bridge):
    assert "result" in call(bridge, "run_python", {"code": MARK})
    assert builtins._claude_bridge_ran is True


def test_a_client_without_the_token_runs_nothing(bridge):
    protocol.token_path(bridge.port).write_text("wrong")
    out = call(bridge, "run_python", {"code": MARK})
    assert "Unauthorized" in str(out["error"])
    assert not hasattr(builtins, "_claude_bridge_ran")


def test_stopping_removes_the_token(bridge):
    path = protocol.token_path(bridge.port)
    assert path.read_text()
    bridge.stop()
    assert not path.exists()

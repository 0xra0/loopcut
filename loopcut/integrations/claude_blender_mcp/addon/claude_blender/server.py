"""Local socket server. Network I/O runs on a background thread; every command runs on
Blender's main thread through a queue drained by a bpy timer, because bpy is not thread safe."""

import queue
import socket
import threading

import bpy

from . import protocol
from .handlers import dispatch

TICK_SECONDS = 0.02
MAX_JOBS_PER_TICK = 8


class _Job:
    __slots__ = ("request", "response", "done")

    def __init__(self, request):
        self.request = request
        self.response = None
        self.done = threading.Event()


class BridgeServer:
    def __init__(self, host=protocol.DEFAULT_HOST, port=protocol.DEFAULT_PORT):
        self.host = host
        self.port = port
        self._sock = None
        self._thread = None
        self._jobs = queue.Queue()
        self._running = threading.Event()
        self.clients = 0

    @property
    def running(self):
        return self._running.is_set()

    def start(self, use_timer=True):
        if self.running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(4)
        sock.settimeout(0.5)
        self.port = sock.getsockname()[1]  # resolves port 0 in tests
        self._sock = sock
        self._running.set()
        self._thread = threading.Thread(target=self._accept_loop, name="claude-blender-accept", daemon=True)
        self._thread.start()
        if use_timer and not bpy.app.timers.is_registered(self._tick):
            bpy.app.timers.register(self._tick, first_interval=TICK_SECONDS, persistent=True)

    def stop(self):
        self._running.clear()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if bpy.app.timers.is_registered(self._tick):
            bpy.app.timers.unregister(self._tick)
        # Unblock anyone still waiting.
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            job.response = protocol.error(job.request.get("id"), "ServerStopped", "Bridge stopped")
            job.done.set()

    # --- main thread -------------------------------------------------------

    def _tick(self):
        if not self.running:
            return None
        self.process_pending()
        return TICK_SECONDS

    def process_pending(self, max_jobs=MAX_JOBS_PER_TICK):
        """Run queued commands. Must be called on the main thread."""
        handled = 0
        while handled < max_jobs:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            job.response = dispatch(job.request)
            job.done.set()
            handled += 1
        return handled

    # --- network thread ----------------------------------------------------

    def _accept_loop(self):
        while self.running:
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client_loop, args=(conn,), name="claude-blender-client", daemon=True).start()

    def _client_loop(self, conn):
        self.clients += 1
        buf = b""
        try:
            conn.settimeout(None)
            while self.running:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        conn.sendall(protocol.encode(self._handle_line(line)))
        except OSError:
            pass
        finally:
            self.clients -= 1
            conn.close()

    def _handle_line(self, line):
        try:
            request = protocol.decode(line)
        except ValueError as exc:
            return protocol.error(None, "BadRequest", f"Invalid JSON: {exc}")
        job = _Job(request)
        self._jobs.put(job)
        timeout = float(request.get("timeout") or protocol.DEFAULT_TIMEOUT)
        if not job.done.wait(timeout):
            return protocol.error(
                request.get("id"),
                "Timeout",
                f"Blender did not finish {request.get('cmd')!r} within {timeout:.0f}s. "
                "It may still be running; check scene_state before retrying.",
            )
        return job.response


_server = None


def get_server():
    return _server


def start(host=protocol.DEFAULT_HOST, port=protocol.DEFAULT_PORT):
    global _server
    if _server and _server.running:
        return _server
    _server = BridgeServer(host, port)
    _server.start()
    return _server


def stop():
    global _server
    if _server:
        _server.stop()
    _server = None

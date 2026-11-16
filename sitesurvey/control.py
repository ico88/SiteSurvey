"""Socket Unix di controllo: `sitesurvey ctl start|stop|toggle|status`."""
from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import threading
from pathlib import Path

log = logging.getLogger(__name__)


class ControlServer:
    def __init__(self, path: str, surveyor):
        self.path = path
        self.surveyor = surveyor
        self.server: socketserver.UnixStreamServer | None = None

    def start(self) -> None:
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()
        sv = self.surveyor

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                cmd = self.rfile.readline().decode().strip().lower()
                actions = {"start": sv.start_recording, "stop": sv.stop_recording,
                           "toggle": sv.toggle_recording, "status": sv.state.snapshot}
                fn = actions.get(cmd)
                try:
                    resp = fn() if fn else {"error": f"comando sconosciuto: {cmd}"}
                except Exception as e:  # noqa: BLE001
                    resp = {"error": str(e)}
                self.wfile.write((json.dumps(resp) + "\n").encode())

        self.server = socketserver.ThreadingUnixStreamServer(self.path, Handler)
        self.server.daemon_threads = True
        os.chmod(self.path, 0o660)
        threading.Thread(target=self.server.serve_forever, name="control", daemon=True).start()
        log.info("socket di controllo: %s", self.path)

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            try:
                os.unlink(self.path)
            except OSError:
                pass


def send_command(path: str, cmd: str, timeout: float = 5.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((cmd + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b"{}")

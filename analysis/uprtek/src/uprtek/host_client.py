"""Client for the chronolume_host ``--control-port`` line protocol (stdlib only)."""

from __future__ import annotations

import json
import socket
from typing import Any


class HostError(RuntimeError):
    pass


class HostClient:
    """Each command returns one JSON line, sent after the next frame has been presented."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7777, timeout_s: float = 10.0) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout_s)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._buf = b""

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> HostClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def command(self, line: str, *, check: bool = True) -> dict[str, Any]:
        if "\n" in line:
            raise ValueError("command must be a single line")
        self._sock.sendall(line.encode("utf-8") + b"\n")
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise HostError("host closed the control connection")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        reply = json.loads(raw.decode("utf-8"))
        if check and not reply.get("ok", False):
            raise HostError(f"{line!r}: {reply.get('error', 'unknown error')}")
        return reply

    def ping(self) -> dict[str, Any]:
        return self.command("ping")

    def info(self) -> dict[str, Any]:
        return self.command("info")

    def set_rgb(self, r: int, g: int, b: int, label: str) -> dict[str, Any]:
        return self.command(f"rgb {int(r)} {int(g)} {int(b)} {_token(label)}")

    def show_image(self, path: str, label: str) -> dict[str, Any]:
        """Show a binary PPM pixel for pixel (the path is the rest of the line, spaces allowed)."""
        return self.command(f"image {_token(label)} {path}")

    def set_eye(self, eye: str) -> dict[str, Any]:
        return self.command(f"eye {eye}")

    def set_brightness(self, level: int, *, check: bool = True) -> dict[str, Any]:
        return self.command(f"brightness {int(level)}", check=check)

    def set_duty(self, percent: int, *, check: bool = True) -> dict[str, Any]:
        return self.command(f"duty {int(percent)}", check=check)

    def set_film(self, on: bool) -> dict[str, Any]:
        return self.command(f"film {'on' if on else 'off'}")

    def set_calibration(self, version: str) -> dict[str, Any]:
        return self.command(f"calibration {_token(version)}")

    def fullscreen(self, on: bool) -> dict[str, Any]:
        return self.command(f"fullscreen {'on' if on else 'off'}")

    def log_start(self, path: str | None = None) -> dict[str, Any]:
        return self.command("log start" + (f" {path}" if path else ""))

    def log_stop(self) -> dict[str, Any]:
        return self.command("log stop")

    def mark(self, **fields: Any) -> dict[str, Any]:
        parts = [f"{k}={_token(str(v))}" for k, v in fields.items()]
        return self.command("mark " + " ".join(parts))


def _token(s: str) -> str:
    """The host splits on whitespace; keep labels and values as single tokens."""
    return "_".join(str(s).split()) or "_"


def readback_matches(reply: dict[str, Any], rgb: tuple[int, int, int], eye: str) -> bool:
    rb = reply.get("readback") or {}
    zero = [0, 0, 0]
    want = list(rgb)
    left = want if eye in ("left", "both") else zero
    right = want if eye in ("right", "both") else zero
    return rb.get("left") == left and rb.get("right") == right


def image_readback_matches(reply: dict[str, Any], frame_hash: str) -> bool:
    """The whole framebuffer equals the expected frame, and the host loaded the expected pixels."""
    img = (reply.get("readback") or {}).get("image") or {}
    return bool(img.get("match")) and img.get("hash") == frame_hash

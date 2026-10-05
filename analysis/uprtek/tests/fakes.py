"""Test doubles: a fake chronolume_host control server and a fake meter."""

from __future__ import annotations

import json
import math
import socket
import threading
from dataclasses import dataclass


class FakeHost:
    """Speaks the chronolume_host control protocol on an ephemeral localhost port."""

    def __init__(self, backend: str = "viture 2.4.0", brightness_ok: bool = True,
                 duty_after_brightness: str | None = None,
                 duty_rejects: frozenset[int] = frozenset()) -> None:
        self.backend = backend
        self.brightness_ok = brightness_ok
        self.duty_rejects = duty_rejects  # duty values the fake glasses ignore (readback unchanged)
        self.duty_after_brightness = duty_after_brightness  # simulate a brightness change moving duty
        self.minimized = False  # stimulus window iconified: 0x0 framebuffer, zero readback
        self.fullscreen_restores = True  # `fullscreen on` brings a minimized window back
        self.fb = (3840, 1080)
        self.image: dict | None = None  # loaded frame: w, h, hash
        self.state = {
            "r": "0", "g": "0", "b": "0", "label": "black", "eye": "both",
            "brightness": "5", "duty_cycle": "98", "film": "0.000000",
            "display_mode": "50", "calibration": "uncalibrated",
            "backend": backend, "device": "Fake Luma Ultra", "image": "", "image_hash": "",
        }
        self.commands: list[str] = []
        self.frame = 0
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._srv.close()

    def _serve(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        buf = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    reply = self._handle(line.decode())
                    conn.sendall((json.dumps(reply) + "\n").encode())

    def _handle(self, line: str) -> dict:
        self.commands.append(line)
        tok = line.split()
        cmd = tok[0]
        ok, error, extra = True, None, {}
        if cmd == "rgb":
            self.state.update(r=tok[1], g=tok[2], b=tok[3], label=" ".join(tok[4:]) or "remote",
                              image="", image_hash="")
            self.image = None
        elif cmd == "image":
            from uprtek.frames import frame_hash, read_ppm
            path = line.split(None, 2)[2]
            try:
                w, h, px = read_ppm(path)
            except OSError as exc:
                ok, error = False, f"cannot open {path} ({exc})"
            else:
                self.image = {"w": w, "h": h, "hash": frame_hash(px)}
                self.state.update(r="0", g="0", b="0", label=tok[1], image=path, image_hash=self.image["hash"])
        elif cmd == "eye":
            self.state["eye"] = tok[1]
        elif cmd == "brightness":
            if self.brightness_ok:
                self.state["brightness"] = tok[1]
                if self.duty_after_brightness is not None:
                    self.state["duty_cycle"] = self.duty_after_brightness
            else:
                ok, error = False, "set_brightness failed: unsupported"
        elif cmd == "duty":
            if int(tok[1]) not in self.duty_rejects:
                self.state["duty_cycle"] = tok[1]
        elif cmd == "calibration":
            self.state["calibration"] = tok[1]
        elif cmd == "fullscreen":
            if tok[1] == "on" and self.fullscreen_restores:
                self.minimized = False
        elif cmd == "info":
            extra = {"fullscreen": "1", "firmware": "fake", "product_id": "4356"}
        elif cmd == "log":
            extra = {"log_path": "data/sessions/fake.jsonl"}
        self.frame += 1
        eye = self.state["eye"]
        rgb = [int(self.state[k]) for k in ("r", "g", "b")]
        reply = {
            "ok": ok,
            "cmd": cmd,
            "frame": self.frame,
            "state": dict(self.state),
            "readback": {
                "fb_w": self.fb[0], "fb_h": self.fb[1],
                "left": rgb if eye in ("left", "both") else [0, 0, 0],
                "right": rgb if eye in ("right", "both") else [0, 0, 0],
            },
        }
        if self.image is not None:
            same = (self.image["w"], self.image["h"]) == self.fb and not self.minimized
            reply["readback"]["image"] = {**self.image, "fb_hash": self.image["hash"] if same else "0" * 16,
                                          "mismatched_pixels": 0 if same else -1, "match": same}
        if self.minimized:
            reply["readback"].update(fb_w=0, fb_h=0, left=[0, 0, 0], right=[0, 0, 0])
        if error:
            reply["error"] = error
        if extra:
            reply["extra"] = extra
        return reply


@dataclass
class FakeMeterInfo:
    name: str = "fake"
    optical_sn: str = "FAKE0001"
    model: str = "MK350X"
    hw_version: str = "HW"
    fw_version: str = "FW"
    wavelength_start_nm: int = 380
    wavelength_end_nm: int = 780
    library_version: str = "0x0"
    library_dir: str = "."


class FakeMeter:
    """Returns a Gaussian at 450 nm scaled by the host's current blue code, in mW/m²/nm."""

    def __init__(self, host: FakeHost) -> None:
        self.host = host
        self.info = FakeMeterInfo()
        self.dark_calls = 0
        self.light_strength = 1
        self.exposure_time_raw = 1000.0
        self.lux: float | None = None
        self.max_exposure_calls: list[int] = []
        self.dark_captures = 0  # next N captures read as a blank display
        self.lux_queue: list[float] = []  # explicit lux for the next captures
        self.dim_labels: dict[str, int] = {}  # host label -> captures read at 5 % (display dimmed)
        self.image_level = 0.3  # an image stimulus reads like this fraction of full-code blue

    def dark(self) -> None:
        self.dark_calls += 1

    def set_max_exposure_ms(self, ms: int) -> None:
        self.max_exposure_calls.append(ms)

    def _peak(self) -> float:
        if self.dark_captures > 0:
            self.dark_captures -= 1
            return 766.0
        return 450.0

    def capture(self, *, auto_exposure: bool, exposure_ms: int = 100) -> dict:
        b = 255 * self.image_level if self.host.state.get("image") else int(self.host.state["b"])
        scale = b / 255.0 * (1 + int(self.host.state["brightness"])) / 9.0
        label = self.host.state["label"]
        if self.dim_labels.get(label, 0) > 0:
            self.dim_labels[label] -= 1
            scale *= 0.05
        if self.lux_queue:
            lux = self.lux_queue.pop(0)
        else:
            lux = self.lux if self.lux is not None else 100.0 * scale
        spectrum = [
            1.0 + 50.0 * scale * math.exp(-0.5 * ((wl - 450) / 9.0) ** 2) for wl in range(380, 781)
        ]
        return {
            "auto_exposure": auto_exposure,
            "exposure_ms_requested": None,
            "light_strength": self.light_strength,
            "light_strength_name": "normal",
            "temperature_c": 30.0,
            "instrument": {"lux": lux, "cct_K": None, "exposure_time_raw": self.exposure_time_raw,
                           "lambda_peak_nm": self._peak()},
            "spectrum": spectrum,
        }

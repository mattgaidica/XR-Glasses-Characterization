"""Spatial grid capture: the nine single cells of a 3x3 grid (uprtek.grid_frames) on the glasses.

One block per color (R, G, B, W at full code), all at brightness ``GRID_LEVEL`` and the session's
base duty cycle: black, the uniform full field, the nine cells (center first), and black again. A
B255 anchor at brightness 8 runs at the start and after every block. Cells are rendered at the
host's framebuffer size and shown with the host's ``image`` command; a capture starts only when the
whole framebuffer reads back equal to the pattern. Analyze with ``chronolume-uprtek-grid-check``.

Run under 32-bit Python from the repository root, with chronolume_host fullscreen on the glasses::

    py -3.12-32 -m uprtek.grid_capture --session-index 1 --host-port 7777

``--dry-run`` renders the patterns and prints the plan without a meter or host.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

from uprtek import __version__
from uprtek.frame_capture import _capture_frame, _merge_meta, check_frame
from uprtek.frames import write_ppm
from uprtek.grid_frames import (CELLS, COLOR_RGB, COLORS, GRID_LEVEL, PATTERNS, color_patterns, describe_pattern,
                                render)
from uprtek.host_client import HostClient, HostError
from uprtek.mkusb import DEFAULT_MAX_EXPOSURE_MS
from uprtek.rawio import list_captures, write_json
from uprtek.sequences import DUTY_PRESETS, Step
from uprtek.sweep import REFERENCE_ANCHOR_TRIES, MeterLike, SweepOptions, _anchor, _run_step, duty_acceptance, now_iso

SESSION_SCHEMA = "chronolume-uprtek-grid-session/1"


def full_label(color: str) -> str:
    return f"G_{color}_FULL"


def color_order(session_index: int) -> list[str]:
    """Colors in their listed order, reversed on even session indices (counterbalanced drift)."""
    names = [c for c, _ in COLORS]
    return names[::-1] if session_index % 2 == 0 else names


def block_labels(color: str) -> list[str]:
    return ([f"K_{color}_start", full_label(color)] + [p.name for p in color_patterns(color)]
            + [f"K_{color}_end"])


def prepare_patterns(frames_dir: Path, w: int, h: int) -> list[dict[str, Any]]:
    """Render every pattern at w x h into ``frames_dir/<w>x<h>/`` (kept if already there with the same
    hash) and return each pattern's description with its absolute file path."""
    out = frames_dir / f"{w}x{h}"
    records = []
    for spec in PATTERNS:
        px = render(spec.name, w, h)
        rec = describe_pattern(spec.name, w, h)
        check_frame(spec, [tuple(c["rgb"]) for c in rec["colors"]])
        path = out / f"{spec.name}.ppm"
        header = f"P6\n{w} {h}\n255\n".encode("ascii")
        if not path.exists() or path.read_bytes() != header + px:
            write_ppm(path, w, h, px)
        records.append({**rec, "file": str(path.resolve())})
    (out / "grid.json").write_text(json.dumps({"schema": "chronolume-grid-frames/1", "frames": records}, indent=1)
                                   + "\n", encoding="utf-8")
    return records


def run_grid_session(meter: MeterLike, host: HostClient, session_dir: Path, opts: SweepOptions, *,
                     session_index: int = 1, frames_dir: Path = Path("data/uprtek/grid_frames"),
                     reseated: bool = True, prompt: Callable[[str], str] = input,
                     sleep: Callable[[float], None] = time.sleep,
                     out: Callable[[str], None] = print) -> dict[str, Any]:
    session_dir.mkdir(parents=True, exist_ok=True)
    host_info = host.info()
    backend = host_info["state"].get("backend", "")
    if backend == "mock" and not opts.allow_mock:
        raise HostError("host is running --device mock; pass --allow-mock to measure anyway")
    if host_info.get("extra", {}).get("fullscreen") != "1":
        host.fullscreen(True)
        host_info = host.info()
    rb = host_info.get("readback") or {}
    fb_w, fb_h = int(rb.get("fb_w") or 0), int(rb.get("fb_h") or 0)
    if fb_w < 3 or fb_h < 3:
        raise HostError(f"framebuffer {fb_w}x{fb_h}: put the stimulus window fullscreen on the glasses")
    records = {r["name"]: r for r in prepare_patterns(frames_dir, fb_w, fb_h)}
    initial_brightness = int(host_info["state"].get("brightness", "0"))
    initial_duty = host_info["state"].get("duty_cycle")

    opts = replace(opts, sequence="grid", base_brightness=None)
    if opts.calibration_version:
        host.set_calibration(opts.calibration_version)
    log_reply = host.log_start()
    meter_info = asdict(meter.info) if hasattr(meter.info, "__dataclass_fields__") else dict(meter.info)
    order = color_order(session_index)
    session: dict[str, Any] = {
        "schema": SESSION_SCHEMA,
        "status": "running",
        "started": now_iso(),
        "finished": None,
        "package_version": __version__,
        "python": f"{platform.python_version()} {platform.architecture()[0]}",
        "session_index": session_index,
        "reseated": reseated,
        "meter": meter_info,
        "host": {
            "backend": backend,
            "device": host_info["state"].get("device"),
            "firmware": host_info.get("extra", {}).get("firmware"),
            "product_id": host_info.get("extra", {}).get("product_id"),
            "film": host_info["state"].get("film"),
            "display_mode": host_info["state"].get("display_mode"),
            "initial_brightness": initial_brightness,
            "initial_duty_cycle": initial_duty,
            "framebuffer": [fb_w, fb_h],
            "session_log": log_reply.get("extra", {}).get("log_path", ""),
        },
        "grid": {"level": GRID_LEVEL, "cells": [{"cell": c, "row": r, "col": k} for c, r, k in CELLS],
                 "colors": {c: list(rgb) for c, rgb in COLORS}},
        "frames": [records[p.name] for color in order for p in color_patterns(color)],
        "measure_options": asdict(opts),
        "duty_acceptance": None,
        "base_duty": None,
        "dark": None,
        "warmup": None,
        "blocks": [],
        "anchors": [],
        "captures": [],
    }
    session_path = session_dir / "session.json"
    write_json(session_path, session)

    try:
        acceptance = duty_acceptance(host, DUTY_PRESETS, initial_duty)
        base_duty = max(acceptance["duties"])
        opts = replace(opts, duty=base_duty)
        session.update(duty_acceptance=acceptance, base_duty=base_duty, measure_options=asdict(opts))
        write_json(session_path, session)
        out(f"grid: base duty {base_duty}, level {GRID_LEVEL}, framebuffer {fb_w}x{fb_h}, order {' '.join(order)}")

        meter.set_max_exposure_ms(opts.max_exposure_ms)
        if not opts.skip_dark:
            host.set_rgb(0, 0, 255, "dark_cal")
            prompt("Cap the meter sensor for dark calibration, then press Enter… ")
            t_dark = time.monotonic()
            meter.dark()
            session["dark"] = {"time": now_iso(), "method": "mk_Msr_Dark (capped sensor)",
                               "duration_s": round(time.monotonic() - t_dark, 2)}
            host.mark(event="dark_calibration")
            prompt("Uncap and position the probe at the eye position, then press Enter… ")
            write_json(session_path, session)
        if opts.eye:
            host.set_eye(opts.eye)
        host.set_rgb(0, 0, 0, "duty_set")
        reply = host.set_duty(base_duty, check=False)
        if reply["state"].get("duty_cycle") != str(base_duty):
            raise HostError(f"base duty {base_duty} not applied ({reply.get('error')})")
        if opts.warmup_s > 0:
            host.set_rgb(0, 0, 255, "warmup")
            out(f"warm-up: (0,0,255) for {opts.warmup_s:.0f} s")
            t0 = now_iso()
            sleep(opts.warmup_s)
            session["warmup"] = {"start": t0, "end": now_iso(), "rgb": [0, 0, 255]}

        index = 0
        reference: float | None = None

        def anchor(after: str | None) -> dict[str, Any]:
            rec = _anchor(meter, host, session_dir, len(session["anchors"]), after, GRID_LEVEL, reference,
                          opts, prompt=prompt, sleep=sleep, out=out, restore_duty=base_duty)
            session["anchors"].append(rec)
            write_json(session_path, session)
            return rec

        for attempt in range(REFERENCE_ANCHOR_TRIES):
            rec = anchor(None)
            if not rec["flags"]:
                reference = rec["lux_median"]
                break
            if attempt + 1 < REFERENCE_ANCHOR_TRIES:
                prompt(f"Reference anchor failed ({' '.join(rec['flags'])}). Check the glasses are awake "
                       "and showing blue, then press Enter… ")
        else:
            raise HostError("reference anchor failed; check the glasses and probe")

        def record(label: str, rgb, role: str, block: int, color: str, cell: str | None,
                   meta: dict[str, Any]) -> None:
            fields = {"role": role, "block_index": block, "color": color, "cell": cell, "acquisition_order": index}
            _merge_meta(session_dir, "raw", label, fields)
            session["captures"].append({"label": label, "rgb": list(rgb), "level": GRID_LEVEL, "duty": base_duty,
                                        "flags": meta["flags"], **fields})
            write_json(session_path, session)

        def uniform(label: str, rgb: tuple[int, int, int], role: str, block: int, color: str) -> None:
            nonlocal index
            meta = _run_step(meter, host, session_dir, Step(label, rgb, GRID_LEVEL, base_duty), index, opts,
                             GRID_LEVEL, prompt=prompt, sleep=sleep, out=out)
            index += 1
            record(label, rgb, role, block, color, None, meta)

        for bi, color in enumerate(order):
            labels = block_labels(color)
            session["blocks"].append({"index": bi, "color": color, "rgb": list(COLOR_RGB[color]),
                                      "level": GRID_LEVEL, "duty": base_duty, "captures": labels})
            uniform(labels[0], (0, 0, 0), "black_start", bi, color)
            uniform(full_label(color), COLOR_RGB[color], "full", bi, color)
            for spec, (cell, _, _) in zip(color_patterns(color), CELLS):
                meta = _capture_frame(meter, host, session_dir, records[spec.name], spec, index, opts, base_duty,
                                      prompt=prompt, sleep=sleep, out=out)
                index += 1
                record(spec.name, spec.nominal_rgb, "cell", bi, color, cell, meta)
            uniform(labels[-1], (0, 0, 0), "black_end", bi, color)
            a = anchor(labels[-1])
            if a["flags"]:
                sleep(opts.retry_wait_s)
                a = anchor(labels[-1])
            if a["flags"]:
                for c in session["captures"]:
                    if c["label"] in labels and "ANCHOR_DRIFT" not in c["flags"]:
                        c["flags"].append("ANCHOR_DRIFT")
                        _merge_meta(session_dir, "raw", c["label"], {"flags": c["flags"]})
                write_json(session_path, session)
                prompt(f"Anchor {a['label']} failed ({' '.join(a['flags'])}); block {color} is flagged "
                       "ANCHOR_DRIFT. Check the glasses are awake and the probe has not moved, then press Enter… ")
        session["status"] = "complete"
    except KeyboardInterrupt:
        session["status"] = "aborted"
        out("aborted by operator")
    except Exception as exc:
        session["status"] = "error"
        session["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        session["finished"] = now_iso()
        try:
            meter.set_max_exposure_ms(DEFAULT_MAX_EXPOSURE_MS)
        except Exception as exc:
            session.setdefault("cleanup_errors", []).append(f"max exposure not restored: {exc}")
        try:
            host.set_rgb(0, 0, 0, "black")
            reply = host.set_brightness(initial_brightness, check=False)
            session["host"]["restored_brightness"] = reply["state"].get("brightness")
            if initial_duty is not None:
                reply = host.set_duty(int(initial_duty), check=False)
                session["host"]["restored_duty_cycle"] = reply["state"].get("duty_cycle")
            host.log_stop()
        except (HostError, OSError) as exc:
            session.setdefault("cleanup_errors", []).append(str(exc))
        write_json(session_path, session)
    return session


def describe_plan(session_index: int, w: int, h: int) -> str:
    lines = [f"grid session {session_index}, framebuffer {w}x{h}, level {GRID_LEVEL}"]
    for color in color_order(session_index):
        rec = describe_pattern(color_patterns(color)[0].name, w, h)
        lines.append(f"  {color} {COLOR_RGB[color]}: " + ", ".join(block_labels(color)) + ", anchor"
                     f"  (cell lit {100 * rec['lit_fraction']:.2f}%)")
    n = sum(len(block_labels(c)) for c in color_order(session_index))
    lines.append(f"  {n} captures + {1 + len(COLORS)} anchors")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session-index", type=int, default=1,
                   help="1, 2, 3 … for reseated sessions; even indices run the colors in reverse order")
    p.add_argument("--frames-dir", type=Path, default=Path("data/uprtek/grid_frames"),
                   help="where the rendered patterns are written (default data/uprtek/grid_frames)")
    p.add_argument("--dry-run", action="store_true", help="print the plan (at --width x --height) and exit")
    p.add_argument("--width", type=int, default=1920, help="framebuffer width for --dry-run")
    p.add_argument("--height", type=int, default=1080, help="framebuffer height for --dry-run")
    p.add_argument("--session-dir", type=Path, help="default: data/uprtek/grid_s<index>_<timestamp>")
    p.add_argument("--data-root", type=Path, default=Path("data/uprtek"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--host-port", type=int, default=7777)
    p.add_argument("--allow-mock", action="store_true", help="measure even if the host is --device mock")
    p.add_argument("--eye", default="both", choices=["both", "left", "right"])
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--settle-s", type=float, default=1.0, help="dwell after each stimulus change")
    p.add_argument("--brightness-settle-s", type=float, default=3.0,
                   help="dwell after each brightness or duty change")
    p.add_argument("--max-exposure-ms", type=int, default=DEFAULT_MAX_EXPOSURE_MS)
    p.add_argument("--calibration-version", help="tag sent to the host and logged with every event")
    p.add_argument("--skip-dark", action="store_true", help="skip mk_Msr_Dark (capped-sensor) step")
    p.add_argument("--warmup-s", type=float, default=0.0, help="present (0,0,255) before the session")
    p.add_argument("--not-reseated", action="store_true",
                   help="the glasses were not reseated on the fixture since the previous session")
    p.add_argument("--meter-sn", help="optical serial number if several meters are connected")
    p.add_argument("--mkusb-dir", help="folder containing mkusb.dll (default: uSpectrum VC example)")
    p.add_argument("--notes", default="")
    p.add_argument("--overwrite", action="store_true", help="allow writing into a session with captures")
    args = p.parse_args(argv)

    if args.dry_run:
        print(describe_plan(args.session_index, args.width, args.height))
        return 0
    if args.repeats < 1:
        p.error("--repeats must be >= 1")
    import datetime as _dt
    session_dir = args.session_dir or args.data_root / _dt.datetime.now().strftime(
        f"grid_s{args.session_index}_%Y%m%d_%H%M%S")
    if session_dir.exists() and not args.overwrite:
        try:
            if list_captures(session_dir):
                p.error(f"{session_dir} already has captures; use --overwrite or a new --session-dir")
        except FileNotFoundError:
            pass
    opts = SweepOptions(
        sequence="grid",
        eye=args.eye,
        repeats=args.repeats,
        settle_s=args.settle_s,
        brightness_settle_s=args.brightness_settle_s,
        max_exposure_ms=args.max_exposure_ms,
        calibration_version=args.calibration_version,
        skip_dark=args.skip_dark,
        warmup_s=args.warmup_s,
        allow_mock=args.allow_mock,
        notes=args.notes,
    )

    from uprtek.mkusb import Meter, MkUsbError

    try:
        meter = Meter(args.mkusb_dir, optical_sn=args.meter_sn)
    except MkUsbError as exc:
        print(f"meter: {exc}", file=sys.stderr)
        return 2
    try:
        print(f"meter  {meter.info.model} SN {meter.info.optical_sn} {meter.info.fw_version}")
        try:
            host = HostClient(args.host, args.host_port)
        except OSError as exc:
            print(f"host: cannot connect to {args.host}:{args.host_port} ({exc}); "
                  "start chronolume_host with --control-port", file=sys.stderr)
            return 2
        with host:
            session = run_grid_session(meter, host, session_dir, opts, session_index=args.session_index,
                                       frames_dir=args.frames_dir, reseated=not args.not_reseated)
    except HostError as exc:
        print(f"host: {exc}", file=sys.stderr)
        return 2
    finally:
        meter.close()
    print(f"session {session['status']}: {session_dir}")
    return 0 if session["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())

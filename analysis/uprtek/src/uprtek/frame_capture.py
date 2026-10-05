"""Frame capture: the example application frames (uprtek.frames) on the glasses, with references.

Each frame is a block at the frame's own hardware brightness and the session's base duty cycle:
black, the frame, each uniform full-frame color the frame contains (same-session references), and
black again. A B255 anchor at brightness 8 runs at the start and after every block. Frames are
rendered at the host's framebuffer size and shown with the host's ``image`` command; a capture
starts only when the whole framebuffer reads back equal to the frame.

Run under 32-bit Python from the repository root, with chronolume_host fullscreen on the glasses::

    py -3.12-32 -m uprtek.frame_capture --session-index 1 --host-port 7777

``--dry-run`` renders the frames and prints the plan without a meter or host.
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
from uprtek.frames import BY_NAME, FRAMES, FrameSpec, describe, frame_hash, render, write_ppm
from uprtek.host_client import HostClient, HostError, image_readback_matches
from uprtek.mkusb import DEFAULT_MAX_EXPOSURE_MS
from uprtek.rawio import list_captures, write_capture, write_json
from uprtek.sequences import DUTY_PRESETS, WHITE_MAX_BRIGHTNESS, Step, is_white
from uprtek.sweep import (
    READBACK_PROMPTS,
    REFERENCE_ANCHOR_TRIES,
    RERUN_FLAGS,
    MeterLike,
    SweepOptions,
    _anchor,
    _ensure_duty,
    _measure,
    _run_step,
    _set_brightness,
    duty_acceptance,
    now_iso,
)

SESSION_SCHEMA = "chronolume-uprtek-frame-session/1"
MULTI_CHANNEL_MAX_BRIGHTNESS = 7


def reference_label(frame: str, rgb: tuple[int, int, int]) -> str:
    return f"{frame}_U{rgb[0]:03d}-{rgb[1]:03d}-{rgb[2]:03d}"


def check_frame(spec: FrameSpec, colors: list[tuple[int, int, int]]) -> None:
    """Refuse a frame whose colors would put white or a multi-channel stimulus above its limit."""
    for rgb in colors:
        if is_white(rgb) and spec.level > WHITE_MAX_BRIGHTNESS:
            raise ValueError(f"{spec.name}: white above brightness {WHITE_MAX_BRIGHTNESS}")
        if sum(1 for v in rgb if v > 0) > 1 and spec.level > MULTI_CHANNEL_MAX_BRIGHTNESS:
            raise ValueError(f"{spec.name}: multi-channel color {rgb} above brightness "
                             f"{MULTI_CHANNEL_MAX_BRIGHTNESS}")


def prepare_frames(frames_dir: Path, w: int, h: int) -> list[dict[str, Any]]:
    """Render every frame at w x h into ``frames_dir/<w>x<h>/`` (kept if already there with the same
    hash) and return each frame's description with its absolute file path."""
    out = frames_dir / f"{w}x{h}"
    records = []
    for spec in FRAMES:
        px = render(spec.name, w, h)
        rec = describe(spec, px, w, h)
        check_frame(spec, [tuple(c["rgb"]) for c in rec["colors"]])
        path = out / f"{spec.name}.ppm"
        header = f"P6\n{w} {h}\n255\n".encode("ascii")
        if not path.exists() or path.read_bytes() != header + px:
            write_ppm(path, w, h, px)
        records.append({**rec, "file": str(path.resolve())})
    (out / "frames.json").write_text(json.dumps({"schema": "chronolume-frames/1", "frames": records}, indent=1)
                                     + "\n", encoding="utf-8")
    return records


def frame_order(session_index: int) -> list[str]:
    """Frames in their listed order, reversed on even session indices (counterbalanced drift)."""
    names = [f.name for f in FRAMES]
    return names[::-1] if session_index % 2 == 0 else names


def _merge_meta(session_dir: Path, sub: str, label: str, fields: dict[str, Any]) -> None:
    path = session_dir / sub / f"{label}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(fields)
    write_json(path, meta)


def _show_frame(host: HostClient, rec: dict[str, Any], label: str, opts: SweepOptions, *,
                prompt: Callable[[str], str], sleep: Callable[[float], None]) -> tuple[dict[str, Any], list]:
    """Show the frame and require the whole framebuffer to read back equal to it."""
    fixes: list[dict[str, Any]] = []
    reply = host.show_image(rec["file"], label)
    for attempt in range(READBACK_PROMPTS + 2):
        if image_readback_matches(reply, rec["hash"]):
            return reply, fixes
        rb = reply.get("readback") or {}
        fixes.append({"attempt": attempt, "time": now_iso(), "readback": rb})
        if attempt == READBACK_PROMPTS + 1:
            break
        if attempt == 0:
            host.fullscreen(True)
        else:
            img = rb.get("image") or {}
            prompt(f"Frame {rec['name']} not shown pixel for pixel: framebuffer {rb.get('fb_w')}x{rb.get('fb_h')}, "
                   f"frame {img.get('w')}x{img.get('h')}, {img.get('mismatched_pixels')} pixels differ. Put the "
                   "stimulus window fullscreen on the glasses (P), then press Enter… ")
        sleep(opts.settle_s)
        reply = host.show_image(rec["file"], label)
    raise HostError(f"{label}: frame readback still does not match ({(reply.get('readback') or {}).get('image')})")


def _capture_frame(meter: MeterLike, host: HostClient, session_dir: Path, rec: dict[str, Any], spec: FrameSpec,
                   index: int, opts: SweepOptions, duty: int, *, prompt: Callable[[str], str],
                   sleep: Callable[[float], None], out: Callable[[str], None]) -> dict[str, Any]:
    label = spec.name
    reply, changed = _set_brightness(host, spec.level, opts)
    if not reply.get("ok") or reply["state"].get("brightness") != str(spec.level):
        raise HostError(f"brightness {spec.level} not applied ({reply.get('error')})")
    if changed:
        sleep(opts.brightness_settle_s)
    if _ensure_duty(host, opts, duty) is not None:
        sleep(opts.brightness_settle_s)
    reply, fixes = _show_frame(host, rec, label, opts, prompt=prompt, sleep=sleep)
    sleep(opts.settle_s)
    # The step's rgb is the frame's dominant lit color: it sets the dark-display check and the
    # analysis' spectral peak search, not the stimulus.
    step = Step(label, spec.nominal_rgb, spec.level, duty)
    discarded = []
    for attempt in range(opts.capture_retries + 1):
        if attempt:
            reply, more = _show_frame(host, rec, label, opts, prompt=prompt, sleep=sleep)
            fixes += more
            sleep(opts.settle_s)
        m = _measure(meter, host, step, opts, sleep=sleep)
        bad = sorted(RERUN_FLAGS & set(m["flags"]))
        if not bad or attempt == opts.capture_retries:
            break
        discarded.append({"attempt": attempt, **m})
        out(f"     {label}: {' '.join(bad)}; re-running capture ({attempt + 1}/{opts.capture_retries})")
        sleep(opts.retry_wait_s)
    flags = list(m["flags"])
    if reply["state"].get("duty_cycle") != str(duty):
        flags.append("DUTY_MISMATCH")
    wl0 = int(getattr(meter.info, "wavelength_start_nm", 380))
    wavelengths = [float(wl0 + i) for i in range(len(m["spectra"][0]))]
    meta = {
        "sequence": "frames",
        "step_index": index,
        "rgb": list(spec.nominal_rgb),
        "rgb_meaning": "dominant lit color of the frame; the stimulus is the image in `stimulus`",
        "stimulus": {"kind": "image", "frame": spec.name, "file": rec["file"], "hash": rec["hash"],
                     "width": rec["width"], "height": rec["height"]},
        "eye": opts.eye,
        "requested_brightness": spec.level,
        "brightness_source": "sdk",
        "white_brightness_capped": False,
        "requested_duty_cycle": duty,
        "duty_reapplied_from": None,
        "host_state": reply["state"],
        "host_frame": reply.get("frame"),
        "readback": reply.get("readback"),
        "readback_ok": True,
        "readback_fixes": fixes,
        "settle_s": opts.settle_s,
        "auto_exposure": opts.auto_exposure,
        "exposure_ms_requested": None if opts.auto_exposure else opts.exposure_ms,
        "max_exposure_ms": opts.max_exposure_ms if opts.auto_exposure else None,
        "priming": m["priming"],
        "rejected": m["rejected"],
        "repeats": m["repeats"],
        "flags": flags,
        "out_of_range_causes": m["causes"],
        "outlier_reps": m["outliers"],
        "capture_attempts": len(discarded) + 1,
        "discarded_attempts": discarded,
    }
    write_capture(session_dir, label, wavelengths, m["spectra"], meta)
    out(f"[{index + 1:02d}] {label:<16} frame {rec['hash']} bri={reply['state'].get('brightness')} "
        f"duty={reply['state'].get('duty_cycle')} lux={', '.join('-' if v is None else f'{v:.4g}' for v in m['lux'])} "
        f"{' '.join(flags)}")
    return meta


def run_frame_session(meter: MeterLike, host: HostClient, session_dir: Path, opts: SweepOptions, *,
                      session_index: int = 1, frames_dir: Path = Path("data/uprtek/frames"),
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
    if fb_w < 2 or fb_h < 1:
        raise HostError(f"framebuffer {fb_w}x{fb_h}: put the stimulus window fullscreen on the glasses")
    records = {r["name"]: r for r in prepare_frames(frames_dir, fb_w, fb_h)}
    initial_brightness = int(host_info["state"].get("brightness", "0"))
    initial_duty = host_info["state"].get("duty_cycle")

    opts = replace(opts, sequence="frames", base_brightness=None)
    if opts.calibration_version:
        host.set_calibration(opts.calibration_version)
    log_reply = host.log_start()
    meter_info = asdict(meter.info) if hasattr(meter.info, "__dataclass_fields__") else dict(meter.info)
    order_names = frame_order(session_index)
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
        "frames": [{k: v for k, v in records[n].items()} for n in order_names],
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
        out(f"frames: base duty {base_duty}, framebuffer {fb_w}x{fb_h}, "
            f"order {' '.join(order_names)}")

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

        def anchor(after: str | None, restore_level: int) -> dict[str, Any]:
            rec = _anchor(meter, host, session_dir, len(session["anchors"]), after, restore_level, reference,
                          opts, prompt=prompt, sleep=sleep, out=out, restore_duty=base_duty)
            session["anchors"].append(rec)
            write_json(session_path, session)
            return rec

        first_level = BY_NAME[order_names[0]].level
        for attempt in range(REFERENCE_ANCHOR_TRIES):
            rec = anchor(None, first_level)
            if not rec["flags"]:
                reference = rec["lux_median"]
                break
            if attempt + 1 < REFERENCE_ANCHOR_TRIES:
                prompt(f"Reference anchor failed ({' '.join(rec['flags'])}). Check the glasses are awake "
                       "and showing blue, then press Enter… ")
        else:
            raise HostError("reference anchor failed; check the glasses and probe")

        def record(label: str, rgb, level: int, role: str, block: int, frame: str, meta: dict[str, Any]) -> None:
            fields = {"role": role, "block_index": block, "frame": frame, "acquisition_order": index}
            _merge_meta(session_dir, "raw", label, fields)
            session["captures"].append({"label": label, "rgb": list(rgb), "level": level, "duty": base_duty,
                                        "flags": meta["flags"], **fields})
            write_json(session_path, session)

        def uniform(label: str, rgb: tuple[int, int, int], level: int, role: str, block: int, frame: str) -> None:
            nonlocal index
            meta = _run_step(meter, host, session_dir, Step(label, rgb, level, base_duty), index, opts, level,
                             prompt=prompt, sleep=sleep, out=out)
            index += 1
            record(label, rgb, level, role, block, frame, meta)

        for bi, name in enumerate(order_names):
            spec, rec = BY_NAME[name], records[name]
            refs = [tuple(c["rgb"]) for c in rec["colors"] if tuple(c["rgb"]) != (0, 0, 0)]
            labels = [f"K_{name}_start", name] + [reference_label(name, r) for r in refs] + [f"K_{name}_end"]
            session["blocks"].append({"index": bi, "frame": name, "level": spec.level, "duty": base_duty,
                                      "captures": labels})
            uniform(labels[0], (0, 0, 0), spec.level, "black_start", bi, name)
            meta = _capture_frame(meter, host, session_dir, rec, spec, index, opts, base_duty,
                                  prompt=prompt, sleep=sleep, out=out)
            index += 1
            record(name, spec.nominal_rgb, spec.level, "frame", bi, name, meta)
            for r in refs:
                uniform(reference_label(name, r), r, spec.level, "reference", bi, name)
            uniform(labels[-1], (0, 0, 0), spec.level, "black_end", bi, name)
            nxt = BY_NAME[order_names[bi + 1]].level if bi + 1 < len(order_names) else spec.level
            a = anchor(labels[-1], nxt)
            if a["flags"]:
                sleep(opts.retry_wait_s)
                a = anchor(labels[-1], nxt)
            if a["flags"]:
                for c in session["captures"]:
                    if c["label"] in labels and "ANCHOR_DRIFT" not in c["flags"]:
                        c["flags"].append("ANCHOR_DRIFT")
                        _merge_meta(session_dir, "raw", c["label"], {"flags": c["flags"]})
                write_json(session_path, session)
                prompt(f"Anchor {a['label']} failed ({' '.join(a['flags'])}); block {name} is flagged "
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
    lines = [f"frames session {session_index}, framebuffer {w}x{h}"]
    for name in frame_order(session_index):
        spec = BY_NAME[name]
        px = render(name, w, h)
        rec = describe(spec, px, w, h)
        refs = [tuple(c["rgb"]) for c in rec["colors"] if tuple(c["rgb"]) != (0, 0, 0)]
        lines.append(f"  {name} (level {spec.level}, lit {100 * rec['lit_fraction']:.1f}%, hash {frame_hash(px)}): "
                     f"K, frame, " + ", ".join(reference_label(name, r) for r in refs) + ", K, anchor")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--session-index", type=int, default=1,
                   help="1, 2, 3 … for reseated sessions; even indices run the frames in reverse order")
    p.add_argument("--frames-dir", type=Path, default=Path("data/uprtek/frames"),
                   help="where the rendered frames are written (default data/uprtek/frames)")
    p.add_argument("--dry-run", action="store_true", help="print the plan (at --width x --height) and exit")
    p.add_argument("--width", type=int, default=1920, help="framebuffer width for --dry-run")
    p.add_argument("--height", type=int, default=1080, help="framebuffer height for --dry-run")
    p.add_argument("--session-dir", type=Path, help="default: data/uprtek/frames_s<index>_<timestamp>")
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
        f"frames_s{args.session_index}_%Y%m%d_%H%M%S")
    if session_dir.exists() and not args.overwrite:
        try:
            if list_captures(session_dir):
                p.error(f"{session_dir} already has captures; use --overwrite or a new --session-dir")
        except FileNotFoundError:
            pass
    opts = SweepOptions(
        sequence="frames",
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
            session = run_frame_session(meter, host, session_dir, opts, session_index=args.session_index,
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

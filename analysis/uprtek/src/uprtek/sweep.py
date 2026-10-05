"""Automated UPRtek sweep: drive chronolume_host over its control port, capture with the meter.

Run under 32-bit Python from the repository root, with chronolume_host already running::

    py -3.12-32 -m uprtek.sweep --sequence hosi_set --host-port 7777
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import platform
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from uprtek import __version__
from uprtek.host_client import HostClient, HostError, readback_matches
from uprtek.mkusb import DEFAULT_MAX_EXPOSURE_MS, MANUAL_EXPOSURE_MAX_MS
from uprtek.rawio import list_captures, write_capture, write_json
from uprtek.sequences import (
    DEFAULT_BRIGHTNESS_LEVELS,
    DUTY_BASE,
    DUTY_PRESETS,
    WHITE_MAX_BRIGHTNESS,
    Step,
    build,
    is_white,
    parse_levels,
)

# Exposure in µs (instrument DATA_ExpTime). The auto-exposure maximum follows --max-exposure-ms.
EXPOSURE_RAW_MAX = DEFAULT_MAX_EXPOSURE_MS * 1000.0
# Saturation: a short exposure with real signal. In darkness auto-exposure can also drop to a
# few ms, but reads well under 1 lux (MK350X FW 1.1.1.B4).
SATURATION_MAX_EXPOSURE_RAW = 10000.0
SATURATION_MIN_LUX = 1.0
# A repeat is an outlier when its lux is this far from the median of its capture; captures whose
# median is below OUTLIER_MIN_LUX (near the ~0.02 lux dark floor) are not checked.
OUTLIER_FRACTION = 0.30
OUTLIER_MIN_LUX = 0.1
# The display primaries peak at 459, 539 and 631 nm; a non-black stimulus whose spectrum peaks at
# or above this is the meter's dark noise (the display was blank).
NO_LIGHT_PEAK_NM = 700.0
# A whole capture is re-run when one of these is raised. Both come from the capture's own readings
# (display dark, repeats disagreeing), never from comparison with an expected value.
RERUN_FLAGS = frozenset({"NO_DISPLAY_LIGHT", "REPEAT_OUTLIER"})
# Anchor: a fixed reference stimulus captured at the start and every --anchor-every steps. A
# display that stays dim for a whole capture passes the outlier check; it shows up as an anchor
# far from the first one.
ANCHOR_RGB = (0, 0, 255)
ANCHOR_BRIGHTNESS = 8
ANCHOR_TOLERANCE = 0.10
ANCHOR_BLOCK_RERUNS = 2
REFERENCE_ANCHOR_TRIES = 3
# Readback fixes before a stimulus is declared not visible: one automatic `fullscreen on`, then
# operator prompts. A minimized window reads back a 0x0 framebuffer of zeros.
READBACK_PROMPTS = 2


class MeterLike(Protocol):
    info: Any

    def dark(self) -> None: ...

    def set_max_exposure_ms(self, ms: int) -> None: ...

    def capture(self, *, auto_exposure: bool, exposure_ms: int = 100) -> dict: ...


@dataclass
class SweepOptions:
    sequence: str = "hosi_set"
    eye: str = "both"
    repeats: int = 3
    settle_s: float = 1.0
    brightness_settle_s: float = 3.0
    auto_exposure: bool = True
    exposure_ms: int = 50
    max_exposure_ms: int = DEFAULT_MAX_EXPOSURE_MS
    priming: int = 1
    max_retries: int = 5
    retry_wait_s: float = 2.0
    capture_retries: int = 3
    anchor_every: int = 0
    duty: int | None = None  # one of duty_presets; None: leave the glasses' duty cycle alone
    duty_presets: tuple[int, ...] = DUTY_PRESETS
    base_brightness: int | None = None  # brightness for steps without their own; None: leave alone
    brightness_levels: tuple[int, ...] = DEFAULT_BRIGHTNESS_LEVELS
    brightness_change_rgb: tuple[int, int, int] = (0, 0, 0)
    manual_brightness: bool = False
    calibration_version: str | None = None
    skip_dark: bool = False
    warmup_s: float = 0.0
    allow_mock: bool = False
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def default_session_dir(root: Path) -> Path:
    return root / _dt.datetime.now().strftime("session_%Y%m%d_%H%M%S")


def range_cause(cap: dict[str, Any], exposure_raw_max: float = EXPOSURE_RAW_MAX) -> str | None:
    """Cause of an out-of-range capture (light strength 2), or None when in range.

    The vendor guide calls 2 "over exposure", but this meter also returns it for weak signal.
    """
    if cap.get("light_strength") != 2:
        return None
    inst = cap.get("instrument") or {}
    exposure = inst.get("exposure_time_raw")
    lux = inst.get("lux")
    if exposure is not None and exposure >= exposure_raw_max:
        return "low"
    if (exposure is not None and exposure <= SATURATION_MAX_EXPOSURE_RAW
            and lux is not None and lux >= SATURATION_MIN_LUX):
        return "saturated"
    return "unknown"


def no_display_light(cap: dict[str, Any], rgb: tuple[int, int, int]) -> bool:
    if tuple(rgb) == (0, 0, 0):
        return False
    peak = (cap.get("instrument") or {}).get("lambda_peak_nm")
    return peak is not None and peak >= NO_LIGHT_PEAK_NM


def outlier_reps(lux: list[float | None]) -> list[int]:
    """Indices of repeats whose lux differs from the capture median by > OUTLIER_FRACTION."""
    values = sorted(v for v in lux if v is not None)
    if len(values) < 3:
        return []
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else 0.5 * (values[mid - 1] + values[mid])
    if median < OUTLIER_MIN_LUX:
        return []
    return [i for i, v in enumerate(lux) if v is not None and abs(v - median) > OUTLIER_FRACTION * median]


def run_sweep(
    meter: MeterLike,
    host: HostClient,
    session_dir: Path,
    opts: SweepOptions,
    *,
    prompt: Callable[[str], str] = input,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> dict[str, Any]:
    steps = build(opts.sequence, opts.brightness_levels)
    too_bright = [s.label for s in steps
                  if is_white(s.rgb) and s.brightness is not None and s.brightness > WHITE_MAX_BRIGHTNESS]
    if too_bright:
        raise ValueError(f"white above brightness {WHITE_MAX_BRIGHTNESS} is not allowed: {too_bright}")
    if opts.duty is not None and opts.duty not in opts.duty_presets:
        raise ValueError(f"duty must be one of {list(opts.duty_presets)} or None, got {opts.duty!r}")
    needs_duty_check = any(s.duty is not None for s in steps)
    session_dir.mkdir(parents=True, exist_ok=True)

    host_info = host.info()
    backend = host_info["state"].get("backend", "")
    if backend == "mock" and not opts.allow_mock:
        raise HostError("host is running --device mock; pass --allow-mock to measure anyway")
    if host_info.get("extra", {}).get("fullscreen") != "1":
        out("WARNING: stimulus window is not fullscreen (send `fullscreen on` or press P)")
    initial_brightness = int(host_info["state"].get("brightness", "0"))
    initial_duty = host_info["state"].get("duty_cycle")

    if opts.calibration_version:
        host.set_calibration(opts.calibration_version)
    log_reply = host.log_start()
    host_log = log_reply.get("extra", {}).get("log_path", "")

    meter_info = asdict(meter.info) if hasattr(meter.info, "__dataclass_fields__") else dict(meter.info)
    session: dict[str, Any] = {
        "schema": "chronolume-uprtek-session/1",
        "status": "running",
        "started": now_iso(),
        "finished": None,
        "package_version": __version__,
        "python": f"{platform.python_version()} {platform.architecture()[0]}",
        "meter": meter_info,
        "host": {
            "backend": backend,
            "device": host_info["state"].get("device"),
            "firmware": host_info.get("extra", {}).get("firmware"),
            "product_id": host_info.get("extra", {}).get("product_id"),
            "initial_brightness": initial_brightness,
            "initial_duty_cycle": initial_duty,
            "session_log": host_log,
        },
        "options": {k: v for k, v in asdict(opts).items()},
        "dark": None,
        "warmup": None,
        "duty": None,
        "duty_acceptance": None,
        "anchors": [],
        "block_reruns": [],
        "captures": [],
    }
    session_path = session_dir / "session.json"
    write_json(session_path, session)

    brightness_changed = False
    duty_changed = False
    try:
        if needs_duty_check:
            acceptance = duty_acceptance(host, opts.duty_presets, initial_duty)
            duty_changed = True
            session["duty_acceptance"] = acceptance
            write_json(session_path, session)
            duties = tuple(acceptance["duties"])
            out(f"duty check: presets {list(duties)} accepted")
            block_base = opts.duty if opts.duty is not None else _int_or_none(initial_duty)
            steps = build(opts.sequence, opts.brightness_levels, duties=duties, base_duty=block_base)

        meter.set_max_exposure_ms(opts.max_exposure_ms)
        session["meter_max_exposure_ms"] = opts.max_exposure_ms
        if not opts.skip_dark:
            # The sensor is capped, so the stimulus does not reach it; a lit panel keeps the glasses
            # from idling dark through the operator prompts.
            host.set_rgb(*ANCHOR_RGB, "dark_cal")
            prompt("Cap the meter sensor for dark calibration, then press Enter… ")
            t_dark = time.monotonic()
            meter.dark()
            session["dark"] = {"time": now_iso(), "method": "mk_Msr_Dark (capped sensor)",
                               "duration_s": round(time.monotonic() - t_dark, 2),
                               "display_rgb": list(ANCHOR_RGB)}
            host.mark(event="dark_calibration")
            prompt("Uncap and position the probe at the eye position, then press Enter… ")
            write_json(session_path, session)

        if opts.eye:
            host.set_eye(opts.eye)

        if opts.duty is not None:
            reply = host.set_duty(opts.duty, check=False)
            reported = reply["state"].get("duty_cycle")
            if not reply.get("ok") or reported != str(opts.duty):
                raise HostError(f"duty {opts.duty} not applied ({reply.get('error') or reported})")
            duty_changed = duty_changed or reported != initial_duty
            session["duty"] = {"requested": opts.duty, "reported": reported, "time": now_iso()}
            write_json(session_path, session)

        if opts.warmup_s > 0:
            host.set_rgb(0, 0, 255, "warmup")
            out(f"warm-up: (0,0,255) for {opts.warmup_s:.0f} s")
            t0 = now_iso()
            sleep(opts.warmup_s)
            session["warmup"] = {"start": t0, "end": now_iso(), "rgb": [0, 0, 255]}

        current_brightness = initial_brightness
        if opts.base_brightness is not None:
            reply = _change_brightness(host, opts.base_brightness, opts.brightness_change_rgb)
            reported = reply["state"].get("brightness")
            if not reply.get("ok") or reported != str(opts.base_brightness):
                raise HostError(f"brightness {opts.base_brightness} not applied "
                                f"({reply.get('error') or reported}); use --brightness keep")
            brightness_changed = True
            current_brightness = opts.base_brightness
            session["base_brightness"] = {"requested": opts.base_brightness, "reported": reported,
                                          "time": now_iso()}
            sleep(opts.brightness_settle_s)
        reference: float | None = None

        def anchor(after: str | None) -> dict[str, Any]:
            nonlocal brightness_changed
            rec = _anchor(meter, host, session_dir, len(session["anchors"]), after, current_brightness,
                          reference, opts, prompt=prompt, sleep=sleep, out=out)
            brightness_changed = True
            session["anchors"].append(rec)
            write_json(session_path, session)
            return rec

        if opts.anchor_every > 0:
            for attempt in range(REFERENCE_ANCHOR_TRIES):
                rec = anchor(None)
                if not rec["flags"]:
                    reference = rec["lux_median"]
                    break
                if attempt + 1 < REFERENCE_ANCHOR_TRIES:
                    prompt(f"Reference anchor failed ({' '.join(rec['flags'])}). Check the glasses are "
                           "awake and showing blue, then press Enter… ")
            else:
                raise HostError("reference anchor failed; check the glasses and probe, or pass --anchor-every 0")

        block = opts.anchor_every if opts.anchor_every > 0 else len(steps)
        start = 0
        reruns_here = 0
        while start < len(steps):
            end = min(start + block, len(steps))
            for index in range(start, end):
                step = steps[index]
                target = _step_brightness(step, opts, current_brightness, initial_brightness)
                meta = _run_step(
                    meter, host, session_dir, step, index, opts, target,
                    prompt=prompt, sleep=sleep, out=out,
                )
                if target is not None:
                    brightness_changed = True
                    current_brightness = target
                session["captures"] = [c for c in session["captures"] if c["label"] != step.label]
                session["captures"].append(
                    {"label": step.label, "rgb": list(step.rgb), "flags": meta["flags"]}
                )
                write_json(session_path, session)
            if opts.anchor_every > 0:
                rec = anchor(steps[end - 1].label)
                if rec["flags"]:
                    sleep(opts.retry_wait_s)
                    rec = anchor(steps[end - 1].label)
                if rec["flags"]:
                    labels = [s.label for s in steps[start:end]]
                    if reruns_here < ANCHOR_BLOCK_RERUNS:
                        reruns_here += 1
                        session["block_reruns"].append(
                            {"steps": labels, "anchor": rec["label"], "flags": rec["flags"], "time": now_iso()}
                        )
                        write_json(session_path, session)
                        prompt(f"Anchor {rec['label']} failed ({' '.join(rec['flags'])}, "
                               f"{rec['ratio_to_reference']:.2f}x reference). Check the glasses are awake and "
                               f"the probe has not moved, then press Enter to re-run {labels[0]}..{labels[-1]}… ")
                        continue
                    for label in labels:
                        _add_flag(session_dir, label, "ANCHOR_DRIFT")
                        for c in session["captures"]:
                            if c["label"] == label and "ANCHOR_DRIFT" not in c["flags"]:
                                c["flags"].append("ANCHOR_DRIFT")
                    write_json(session_path, session)
            start = end
            reruns_here = 0
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
            session["meter_max_exposure_restored_ms"] = DEFAULT_MAX_EXPOSURE_MS
        except Exception as exc:
            session.setdefault("cleanup_errors", []).append(f"max exposure not restored: {exc}")
        try:
            host.set_rgb(0, 0, 0, "black")
            if brightness_changed:
                reply = host.set_brightness(initial_brightness, check=False)
                session["host"]["restored_brightness"] = reply["state"].get("brightness")
            if duty_changed and initial_duty is not None:
                reply = host.set_duty(int(initial_duty), check=False)
                session["host"]["restored_duty_cycle"] = reply["state"].get("duty_cycle")
            host.log_stop()
        except (HostError, OSError) as exc:
            session.setdefault("cleanup_errors", []).append(str(exc))
        write_json(session_path, session)
    return session


def _run_step(
    meter: MeterLike,
    host: HostClient,
    session_dir: Path,
    step: Step,
    index: int,
    opts: SweepOptions,
    target: int | None,
    *,
    prompt: Callable[[str], str],
    sleep: Callable[[float], None],
    out: Callable[[str], None],
) -> dict[str, Any]:
    flags: list[str] = []
    brightness_source = "unchanged"
    nominal = step.brightness if step.brightness is not None else opts.base_brightness
    if target is not None:
        reply, changed = _set_brightness(host, target, opts)
        reported = reply["state"].get("brightness")
        if reply.get("ok") and reported == str(target):
            if step.brightness is not None or changed:
                brightness_source = "sdk"
        elif opts.manual_brightness:
            prompt(
                f"Host could not set brightness ({reply.get('error', 'reported ' + str(reported))}). "
                f"Set the glasses to brightness {target} by hand, then press Enter… "
            )
            brightness_source = "manual_operator"
            flags.append("BRIGHTNESS_MANUAL")
        else:
            raise HostError(
                f"brightness {target} not applied ({reply.get('error') or reported}); "
                "use --manual-brightness to set it by hand"
            )
        if changed:
            sleep(opts.brightness_settle_s)

    duty_target = step.duty if step.duty is not None else opts.duty
    duty_reapplied_from = _ensure_duty(host, opts, step.duty)
    if step.duty is not None and duty_reapplied_from is not None:
        sleep(opts.brightness_settle_s)

    reply, readback_fixes = _show(host, step.rgb, step.label, opts, prompt=prompt, sleep=sleep)
    readback_ok = readback_matches(reply, step.rgb, opts.eye)
    if duty_target is not None and reply["state"].get("duty_cycle") != str(duty_target):
        flags.append("DUTY_MISMATCH")
    sleep(opts.settle_s)

    m, discarded = _measure_with_reruns(meter, host, step, opts, prompt=prompt, sleep=sleep, out=out)
    flags += m["flags"]

    info = meter.info
    wl0 = int(getattr(info, "wavelength_start_nm", 380))
    wavelengths = [float(wl0 + i) for i in range(len(m["spectra"][0]))]
    meta = {
        "sequence": opts.sequence,
        "step_index": index,
        "rgb": list(step.rgb),
        "eye": opts.eye,
        "requested_brightness": step.brightness,
        "brightness_source": brightness_source,
        "white_brightness_capped": (is_white(step.rgb) and target == WHITE_MAX_BRIGHTNESS
                                    and nominal != WHITE_MAX_BRIGHTNESS),
        "requested_duty_cycle": duty_target,
        "duty_reapplied_from": duty_reapplied_from,
        "host_state": reply["state"],
        "host_frame": reply.get("frame"),
        "readback": reply.get("readback"),
        "readback_ok": readback_ok,
        "readback_fixes": readback_fixes,
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
    write_capture(session_dir, step.label, wavelengths, m["spectra"], meta)
    shown = [f"OUT_OF_RANGE({','.join(m['causes'])})" if f == "OUT_OF_RANGE" else f for f in flags]
    reruns = f" (capture re-run x{len(discarded)})" if discarded else ""
    capped = f" (white capped at {WHITE_MAX_BRIGHTNESS})" if meta["white_brightness_capped"] else ""
    out(
        f"[{index + 1:02d}] {step.label:<10} rgb={step.rgb} bri={reply['state'].get('brightness')}{capped} "
        f"duty={reply['state'].get('duty_cycle')} "
        f"lux={', '.join('-' if v is None else f'{v:.4g}' for v in m['lux'])} "
        f"{' '.join(shown)}{reruns}"
    )
    return meta


def _show(
    host: HostClient,
    rgb: tuple[int, int, int],
    label: str,
    opts: SweepOptions,
    *,
    prompt: Callable[[str], str],
    sleep: Callable[[float], None],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Present a stimulus and require the framebuffer readback to match before measuring.

    Returns the matching reply and the failed readbacks that were fixed. Raises HostError when the
    stimulus is still not visible after an automatic fullscreen and the operator prompts.
    """
    reply = host.set_rgb(*rgb, label)
    fixes: list[dict[str, Any]] = []
    for attempt in range(READBACK_PROMPTS + 2):
        rb = reply.get("readback") or {}
        if readback_matches(reply, rgb, opts.eye) and (rb.get("fb_w") or 0) >= 2:
            return reply, fixes
        fixes.append({"attempt": attempt, "time": now_iso(), "readback": rb})
        if attempt == READBACK_PROMPTS + 1:
            break
        if attempt == 0:
            host.fullscreen(True)
        else:
            prompt(f"Stimulus not visible: readback fb={rb.get('fb_w')}x{rb.get('fb_h')} "
                   f"left={rb.get('left')} right={rb.get('right')}, wanted {list(rgb)}. Put the stimulus "
                   "window fullscreen on the glasses (P), then press Enter… ")
        sleep(opts.settle_s)
        reply = host.set_rgb(*rgb, label)
    raise HostError(f"{label}: stimulus readback still {reply.get('readback')} after fullscreen and "
                    f"{READBACK_PROMPTS} prompts; check the stimulus window and display settings")


def _step_brightness(step: Step, opts: SweepOptions, current: int, kept: int) -> int | None:
    """Brightness to apply before a step, or None to leave it alone. White that would show above
    WHITE_MAX_BRIGHTNESS (from the base level, or the kept level with --brightness keep) is capped,
    and with --brightness keep the next step returns to the kept level; explicit white levels above
    it are refused before the sweep starts."""
    target = step.brightness if step.brightness is not None else opts.base_brightness
    if target is None and current != kept:
        target = kept
    if is_white(step.rgb) and (target if target is not None else current) > WHITE_MAX_BRIGHTNESS:
        return WHITE_MAX_BRIGHTNESS
    return target


def _change_brightness(host: HostClient, level: int,
                       rgb: tuple[int, int, int] = (0, 0, 0)) -> dict[str, Any]:
    """Set hardware brightness with the display black (or the dim --brightness-change-rgb).

    Raising brightness while full white was showing at duty 98 dropped the glasses' USB control
    link (SDK -3) twice; the stimulus is re-presented afterwards, before any capture.
    """
    host.set_rgb(*rgb, "brightness_change")
    return host.set_brightness(level, check=False)


def _set_brightness(host: HostClient, level: int, opts: SweepOptions) -> tuple[dict[str, Any], bool]:
    """Change brightness only when the glasses report another level; return (reply, changed)."""
    state = host.info()["state"]
    if state.get("brightness") == str(level):
        return {"ok": True, "state": state}, False
    return _change_brightness(host, level, opts.brightness_change_rgb), True


def _ensure_duty(host: HostClient, opts: SweepOptions, duty: int | None = None) -> str | None:
    """Apply ``duty`` (a step's own duty cycle) or re-apply the sweep's if the glasses report another
    value; return the value found. A step's own duty is changed with the display black (or the dim
    --brightness-change-rgb), as for brightness."""
    target = opts.duty if duty is None else duty
    if target is None:
        return None
    current = host.info()["state"].get("duty_cycle")
    if current == str(target):
        return None
    if duty is not None:
        host.set_rgb(*opts.brightness_change_rgb, "duty_change")
    host.set_duty(target, check=False)
    return current


def duty_acceptance(host: HostClient, duties: tuple[int, ...], restore: str | None) -> dict[str, Any]:
    """Request each duty cycle with the display black and read it back; HostError if any is not
    accepted. The duty found at the start (``restore``) is put back afterwards. This checks the SDK
    readback only; the optical duty is verified separately (photodiode)."""
    host.set_rgb(0, 0, 0, "duty_check")
    result: dict[str, Any] = {"time": now_iso(), "checks": []}
    try:
        for d in duties:
            reply = host.set_duty(d, check=False)
            reported = reply["state"].get("duty_cycle")
            ok = bool(reply.get("ok")) and reported == str(d)
            result["checks"].append({"requested": d, "reported": reported, "ok": ok, "error": reply.get("error")})
        failed = [c for c in result["checks"] if not c["ok"]]
        if failed:
            raise HostError(f"duty check failed for {list(duties)}: {failed}")
        result["duties"] = list(duties)
    finally:
        r = _int_or_none(restore)
        if r is not None:
            host.set_duty(r, check=False)
    return result


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _measure_with_reruns(
    meter: MeterLike,
    host: HostClient,
    step: Step,
    opts: SweepOptions,
    *,
    prompt: Callable[[str], str],
    sleep: Callable[[float], None],
    out: Callable[[str], None],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Measure a step; re-run the whole capture (all repeats) while it raises a RERUN_FLAGS flag."""
    discarded: list[dict[str, Any]] = []
    for attempt in range(opts.capture_retries + 1):
        if attempt:
            _show(host, step.rgb, step.label, opts, prompt=prompt, sleep=sleep)
            sleep(opts.settle_s)
        m = _measure(meter, host, step, opts, sleep=sleep)
        bad = sorted(RERUN_FLAGS & set(m["flags"]))
        if not bad or attempt == opts.capture_retries:
            break
        discarded.append({"attempt": attempt, **m})
        out(f"     {step.label}: {' '.join(bad)}; re-running capture ({attempt + 1}/{opts.capture_retries})")
        sleep(opts.retry_wait_s)
    return m, discarded


def _measure(
    meter: MeterLike,
    host: HostClient,
    step: Step,
    opts: SweepOptions,
    *,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    flags: list[str] = []
    priming: list[dict[str, Any]] = []
    # A priming capture already at the exposure ceiling cannot converge further; it is kept as
    # repeat 0 instead of spending another multi-second exposure.
    kept: tuple[str, str, dict[str, Any]] | None = None
    ceiling_raw = opts.max_exposure_ms * 1000.0
    if opts.auto_exposure:
        for n in range(opts.priming):
            t_start = now_iso()
            cap = meter.capture(auto_exposure=True, exposure_ms=opts.exposure_ms)
            t_end = now_iso()
            inst = cap.get("instrument") or {}
            exposure = inst.get("exposure_time_raw")
            pinned = exposure is not None and exposure >= ceiling_raw
            priming.append({
                "n": n,
                "t_start": t_start,
                "light_strength": cap.get("light_strength"),
                "exposure_time_raw": exposure,
                "lux": inst.get("lux"),
                "lambda_peak_nm": inst.get("lambda_peak_nm"),
                "kept_as_rep0": pinned,
            })
            host.mark(capture=step.label, priming=n, light_strength=cap.get("light_strength"))
            if pinned:
                kept = (t_start, t_end, cap)
                break

    repeats: list[dict[str, Any]] = []
    spectra: list[list[float]] = []
    causes: list[str] = []
    rejected: list[dict[str, Any]] = []
    for rep in range(opts.repeats):
        for attempt in range(opts.max_retries + 1):
            from_priming = kept is not None
            if kept is not None:
                t_start, t_end, cap = kept
                kept = None
            else:
                t_start = now_iso()
                cap = meter.capture(auto_exposure=opts.auto_exposure, exposure_ms=opts.exposure_ms)
                t_end = now_iso()
            dark = no_display_light(cap, step.rgb)
            mark = host.mark(capture=step.label, rep=rep, attempt=attempt,
                             light_strength=cap.get("light_strength"),
                             display_light="no" if dark else "yes")
            wear = mark.get("state", {}).get("wear")
            if not dark or attempt == opts.max_retries:
                break
            inst = cap.get("instrument") or {}
            rejected.append({
                "rep": rep,
                "attempt": attempt,
                "t_start": t_start,
                "wear": wear,
                "light_strength": cap.get("light_strength"),
                "exposure_time_raw": inst.get("exposure_time_raw"),
                "lux": inst.get("lux"),
                "lambda_peak_nm": inst.get("lambda_peak_nm"),
            })
            sleep(opts.retry_wait_s)
        if dark and "NO_DISPLAY_LIGHT" not in flags:
            flags.append("NO_DISPLAY_LIGHT")
        if wear == "0" and "WEAR_NOT_WORN" not in flags:
            flags.append("WEAR_NOT_WORN")
        spectra.append(cap.pop("spectrum"))
        cause = range_cause(cap, opts.max_exposure_ms * 1000.0)
        repeats.append({"rep": rep, "t_start": t_start, "t_end": t_end, **cap, "range_cause": cause,
                        "attempts": attempt + 1, "no_display_light": dark, "wear": wear,
                        "from_priming": from_priming})
        if cause is not None:
            if "OUT_OF_RANGE" not in flags:
                flags.append("OUT_OF_RANGE")
            if cause not in causes:
                causes.append(cause)

    lux = [r["instrument"].get("lux") for r in repeats]
    outliers = outlier_reps(lux)
    for i, r in enumerate(repeats):
        r["outlier"] = i in outliers
    if outliers:
        flags.append("REPEAT_OUTLIER")
    return {"flags": flags, "priming": priming, "rejected": rejected, "repeats": repeats,
            "spectra": spectra, "causes": causes, "outliers": outliers, "lux": lux}


def _anchor(
    meter: MeterLike,
    host: HostClient,
    session_dir: Path,
    n: int,
    after: str | None,
    restore_brightness: int,
    reference: float | None,
    opts: SweepOptions,
    *,
    prompt: Callable[[str], str],
    sleep: Callable[[float], None],
    out: Callable[[str], None],
    restore_duty: int | None = None,
) -> dict[str, Any]:
    """Capture the anchor stimulus, compare with the reference, and restore the step brightness.

    With ``restore_duty`` (model captures, whose blocks run at their own level and duty) the anchor
    runs at brightness 8 and the sweep's base duty, both checked by readback before measuring, and
    afterwards restores ``restore_brightness`` and ``restore_duty`` on black, checked by readback."""
    label = f"ANCHOR_{n:02d}"
    step = Step(label, ANCHOR_RGB, ANCHOR_BRIGHTNESS)
    explicit = restore_duty is not None
    reply, changed = _set_brightness(host, ANCHOR_BRIGHTNESS, opts)
    if not reply.get("ok") or reply["state"].get("brightness") != str(ANCHOR_BRIGHTNESS):
        raise HostError(f"anchor needs brightness {ANCHOR_BRIGHTNESS} ({reply.get('error')}); "
                        "pass --anchor-every 0 to run without anchors")
    if changed:
        sleep(opts.brightness_settle_s)
    if explicit and opts.duty is not None:
        duty_reapplied_from = _ensure_duty(host, opts, opts.duty)
        reported = host.info()["state"].get("duty_cycle")
        if reported != str(opts.duty):
            raise HostError(f"anchor needs base duty {opts.duty} (glasses report {reported})")
    else:
        duty_reapplied_from = _ensure_duty(host, opts)
    reply, readback_fixes = _show(host, ANCHOR_RGB, label, opts, prompt=prompt, sleep=sleep)
    anchor_state = {"brightness": reply["state"].get("brightness"),
                    "duty_cycle": reply["state"].get("duty_cycle")}
    sleep(opts.settle_s)
    m, discarded = _measure_with_reruns(meter, host, step, opts, prompt=prompt, sleep=sleep, out=out)
    flags = [f for f in m["flags"] if f in RERUN_FLAGS or f == "OUT_OF_RANGE"]
    if opts.duty is not None and reply["state"].get("duty_cycle") != str(opts.duty):
        flags.append("DUTY_MISMATCH")
    values = [v for v in m["lux"] if v is not None]
    median = statistics.median(values) if values else None
    ratio = median / reference if median is not None and reference else None
    if reference is not None and (ratio is None or abs(ratio - 1.0) > ANCHOR_TOLERANCE):
        flags.append("ANCHOR_DRIFT")

    wl0 = int(getattr(meter.info, "wavelength_start_nm", 380))
    wavelengths = [float(wl0 + i) for i in range(len(m["spectra"][0]))]
    write_capture(session_dir / "anchors", label, wavelengths, m["spectra"], {
        "rgb": list(ANCHOR_RGB), "requested_brightness": ANCHOR_BRIGHTNESS,
        "requested_duty_cycle": opts.duty, "host_state": reply["state"],
        "repeats": m["repeats"], "flags": flags, "capture_attempts": len(discarded) + 1,
    })

    restored_state = None
    if explicit:
        host.set_rgb(*opts.brightness_change_rgb, "anchor_restore")
        state = host.info()["state"]
        if state.get("brightness") != str(restore_brightness):
            host.set_brightness(restore_brightness, check=False)
            sleep(opts.brightness_settle_s)
        if state.get("duty_cycle") != str(restore_duty):
            host.set_duty(restore_duty, check=False)
        state = host.info()["state"]
        restored_state = {"brightness": state.get("brightness"), "duty_cycle": state.get("duty_cycle")}
        if restored_state != {"brightness": str(restore_brightness), "duty_cycle": str(restore_duty)}:
            raise HostError(f"anchor could not restore brightness {restore_brightness} and duty "
                            f"{restore_duty} (glasses report {restored_state})")
    # With a base brightness every step sets its own level, so restoring here would only add a change.
    elif restore_brightness != ANCHOR_BRIGHTNESS and opts.base_brightness is None:
        _change_brightness(host, restore_brightness, opts.brightness_change_rgb)
        sleep(opts.brightness_settle_s)
    shown = f" ratio={ratio:.3f}" if ratio is not None else ""
    out(f"[anchor] {label:<10} lux={', '.join('-' if v is None else f'{v:.4g}' for v in m['lux'])}"
        f"{shown} {' '.join(flags)}")
    return {
        "label": label,
        "after_step": after,
        "time": now_iso(),
        "lux": m["lux"],
        "lux_median": median,
        "ratio_to_reference": ratio,
        "flags": flags,
        "duty_reapplied_from": duty_reapplied_from,
        "anchor_state": anchor_state,
        "restored_state": restored_state,
        "readback": reply.get("readback"),
        "readback_fixes": readback_fixes,
        "capture_attempts": len(discarded) + 1,
    }


def _add_flag(session_dir: Path, label: str, flag: str) -> None:
    path = session_dir / "raw" / f"{label}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    if flag not in meta.get("flags", []):
        meta.setdefault("flags", []).append(flag)
        write_json(path, meta)


def _parse_exposure(text: str) -> tuple[bool, int]:
    if text.strip().lower() == "auto":
        return True, 50
    ms = int(text)
    if not 1 <= ms <= MANUAL_EXPOSURE_MAX_MS:
        raise argparse.ArgumentTypeError(f"exposure must be 'auto' or 1..{MANUAL_EXPOSURE_MAX_MS} ms")
    return False, ms


def _parse_max_exposure(text: str) -> int:
    ms = int(text)
    if not 1 <= ms <= 60000:
        raise argparse.ArgumentTypeError("max exposure must be 1..60000 ms")
    return ms


def _parse_brightness(text: str) -> int | None:
    if text.strip().lower() == "keep":
        return None
    level = int(text)
    if not 0 <= level <= 8:
        raise argparse.ArgumentTypeError("brightness must be 0..8 or 'keep'")
    return level


def _parse_rgb(text: str) -> tuple[int, int, int]:
    parts = [int(p) for p in text.split(",")]
    if len(parts) != 3 or not all(0 <= v <= 255 for v in parts):
        raise argparse.ArgumentTypeError("expected R,G,B with each 0..255")
    return (parts[0], parts[1], parts[2])


def _parse_duty(text: str) -> int | None:
    if text.strip().lower() == "keep":
        return None
    try:
        duty = int(text)
    except ValueError:
        duty = None
    if duty not in DUTY_PRESETS:
        raise argparse.ArgumentTypeError(f"duty must be one of {', '.join(map(str, DUTY_PRESETS))} or 'keep'")
    return duty


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sequence", default="hosi_set",
                   choices=["hosi_set", "color_set", "primaries", "blue_ladder", "brightness_sweep",
                            "repeatability"])
    p.add_argument("--session-dir", type=Path, help="default: data/uprtek/session_<timestamp>")
    p.add_argument("--data-root", type=Path, default=Path("data/uprtek"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--host-port", type=int, default=7777)
    p.add_argument("--allow-mock", action="store_true", help="measure even if the host is --device mock")
    p.add_argument("--eye", default="both", choices=["both", "left", "right"])
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--settle-s", type=float, default=1.0, help="dwell after each stimulus change")
    p.add_argument("--brightness-settle-s", type=float, default=3.0)
    p.add_argument("--exposure", default="auto",
                   help=f"'auto' or a fixed exposure in ms (meter limit {MANUAL_EXPOSURE_MAX_MS} ms)")
    p.add_argument("--max-exposure-ms", type=_parse_max_exposure, default=DEFAULT_MAX_EXPOSURE_MS,
                   help=f"auto-exposure ceiling for this sweep (default {DEFAULT_MAX_EXPOSURE_MS}; "
                        "restored afterwards)")
    p.add_argument("--priming", type=int, default=1,
                   help="discarded auto-exposure captures after each stimulus change (default 1)")
    p.add_argument("--max-retries", type=int, default=5,
                   help="re-captures per repeat when the display reads dark (default 5)")
    p.add_argument("--retry-wait-s", type=float, default=2.0, help="wait before each re-capture")
    p.add_argument("--capture-retries", type=int, default=3,
                   help="whole-capture re-runs on NO_DISPLAY_LIGHT or REPEAT_OUTLIER (default 3)")
    p.add_argument("--anchor-every", type=int, default=5,
                   help=f"capture the anchor (B255, brightness {ANCHOR_BRIGHTNESS}) at the start and after "
                        "every N steps; 0 disables (default 5)")
    p.add_argument("--brightness", type=_parse_brightness, default=8,
                   help="hardware brightness for steps without their own (primaries, ladder), 0-8, or "
                        "'keep' (default 8; the initial level is restored after)")
    p.add_argument("--duty", type=_parse_duty, default=DUTY_BASE,
                   help=f"duty cycle for steps without their own: an SDK preset "
                        f"({', '.join(map(str, DUTY_PRESETS))}) or 'keep' (default {DUTY_BASE}; the initial "
                        "duty is restored after)")
    p.add_argument("--brightness-levels", default="0-8", help="e.g. 0-8 or 0,4,8 (swept highest first)")
    p.add_argument("--brightness-change-rgb", type=_parse_rgb, default=(0, 0, 0),
                   help="stimulus shown while hardware brightness changes, R,G,B (default 0,0,0; a dim "
                        "color such as 0,0,16 keeps the panel lit but is untested against the duty-98 "
                        "USB drop)")
    p.add_argument("--manual-brightness", action="store_true",
                   help="prompt the operator when the host cannot set brightness")
    p.add_argument("--calibration-version", help="tag sent to the host and logged with every event")
    p.add_argument("--skip-dark", action="store_true", help="skip mk_Msr_Dark (capped-sensor) step")
    p.add_argument("--warmup-s", type=float, default=0.0, help="present (0,0,255) before the sweep")
    p.add_argument("--meter-sn", help="optical serial number if several meters are connected")
    p.add_argument("--mkusb-dir", help="folder containing mkusb.dll (default: uSpectrum VC example)")
    p.add_argument("--notes", default="")
    p.add_argument("--overwrite", action="store_true", help="allow writing into a session with captures")
    args = p.parse_args(argv)

    auto, ms = _parse_exposure(args.exposure)
    if args.repeats < 1:
        p.error("--repeats must be >= 1")
    if args.priming < 0:
        p.error("--priming must be >= 0")
    if args.max_retries < 0:
        p.error("--max-retries must be >= 0")
    if args.capture_retries < 0:
        p.error("--capture-retries must be >= 0")
    if args.anchor_every < 0:
        p.error("--anchor-every must be >= 0")
    session_dir = args.session_dir or default_session_dir(args.data_root)
    if session_dir.exists() and not args.overwrite:
        try:
            if list_captures(session_dir):
                p.error(f"{session_dir} already has captures; use --overwrite or a new --session-dir")
        except FileNotFoundError:
            pass

    opts = SweepOptions(
        sequence=args.sequence,
        eye=args.eye,
        repeats=args.repeats,
        settle_s=args.settle_s,
        brightness_settle_s=args.brightness_settle_s,
        auto_exposure=auto,
        exposure_ms=ms,
        max_exposure_ms=args.max_exposure_ms,
        priming=args.priming,
        max_retries=args.max_retries,
        retry_wait_s=args.retry_wait_s,
        capture_retries=args.capture_retries,
        anchor_every=args.anchor_every,
        duty=args.duty,
        base_brightness=args.brightness,
        brightness_levels=parse_levels(args.brightness_levels),
        brightness_change_rgb=args.brightness_change_rgb,
        manual_brightness=args.manual_brightness,
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
        print(f"meter  {meter.info.model} SN {meter.info.optical_sn} {meter.info.fw_version} "
              f"{meter.info.wavelength_start_nm}-{meter.info.wavelength_end_nm} nm")
        try:
            host = HostClient(args.host, args.host_port)
        except OSError as exc:
            print(f"host: cannot connect to {args.host}:{args.host_port} ({exc}); "
                  "start chronolume_host with --control-port", file=sys.stderr)
            return 2
        with host:
            session = run_sweep(meter, host, session_dir, opts)
    except HostError as exc:
        print(f"host: {exc}", file=sys.stderr)
        return 2
    finally:
        meter.close()
    print(f"session {session['status']}: {session_dir}")
    return 0 if session["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())

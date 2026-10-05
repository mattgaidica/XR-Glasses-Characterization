"""Exposure-model capture: the staged grids of the developer spec, one stage per session.

Separate from the characterization sweep (uprtek.sweep), whose measurement helpers it reuses. Every
setting carries its own brightness and duty; settings are grouped into blocks of one hardware state
(level, duty), each bracketed by black, in a seeded order counterbalanced across sessions; a B255
anchor at brightness 8 and the base duty runs after every block (``--anchor-every block``, the
default), so the hardware state never changes inside a block, or every N settings.

Run under 32-bit Python from the repository root, with chronolume_host already running::

    py -3.12-32 -m uprtek.model.capture --stage primaries --session-index 1 --host-port 7777

``--dry-run`` prints the plan without a meter or host.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from uprtek import __version__
from uprtek.host_client import HostClient, HostError
from uprtek.mkusb import DEFAULT_MAX_EXPOSURE_MS
from uprtek.rawio import list_captures, write_json
from uprtek.sequences import CHANNELS, WHITE_MAX_BRIGHTNESS, Step, is_white
from uprtek.sweep import (
    REFERENCE_ANCHOR_TRIES,
    MeterLike,
    SweepOptions,
    _anchor,
    _run_step,
    duty_acceptance,
    now_iso,
)

GRID_V1 = "model_grid_v1.json"
# v2 reduces the primaries stage to a signal-gated crossed design; mixtures and validation are v1's.
GRID_V2 = "model_grid_v2.json"
# v3 drops level 0 from the mixtures stage and adds the ladders stage (code ladders at more levels);
# primaries are v2's.
GRID_V3 = "model_grid_v3.json"
DEFAULT_GRID = GRID_V3
STAGES = ("primaries", "ladders", "mixtures", "validation")
ANCHOR_BLOCK = "block"
# Consecutive failed anchors (each already retried once) before the session stops.
ANCHOR_FAILURES_TO_STOP = 2


@dataclass(frozen=True)
class Setting:
    rgb: tuple[int, int, int]
    level: int
    duty: int
    group: str

    @property
    def label(self) -> str:
        r, g, b = self.rgb
        return f"C{r:03d}-{g:03d}-{b:03d}_L{self.level}_D{self.duty}"


@dataclass(frozen=True)
class Block:
    level: int
    duty: int
    settings: tuple[Setting, ...]


@dataclass
class ModelOptions:
    stage: str = "primaries"
    session_index: int = 1
    seed: int = 0
    grid: Path | None = None  # None: the packaged default grid (DEFAULT_GRID)
    anchor_every: int | str = ANCHOR_BLOCK  # "block": after every block; N: every N settings; 0: none
    wear_detection: str = "unknown"
    reseated: bool = True
    measure: SweepOptions = field(default_factory=SweepOptions)


def load_grid(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        text = resources.files("uprtek").joinpath("data").joinpath(DEFAULT_GRID).read_text(encoding="utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    grid = json.loads(text)
    if grid.get("schema") != "chronolume-model-grid/2":
        raise ValueError(f"unsupported grid schema {grid.get('schema')!r}")
    return grid


def stage_duties(grid: dict[str, Any], stage: str) -> tuple[int, ...]:
    """Duty values the stage needs checked, highest first: the grid's duties and the stage's."""
    duties = set(grid["duties"])
    for group in grid["stages"][stage]["groups"]:
        duties |= set(group["duties"])
    return tuple(sorted(duties, reverse=True))


def stage_settings(grid: dict[str, Any], stage: str) -> list[Setting]:
    """Every setting of a stage. Refuses white above WHITE_MAX_BRIGHTNESS and any multi-channel
    stimulus above the grid's limit."""
    if stage not in grid["stages"]:
        raise ValueError(f"unknown stage {stage!r}; choose from {sorted(grid['stages'])}")
    multi_max = int(grid.get("multi_channel_max_brightness", WHITE_MAX_BRIGHTNESS))
    out: list[Setting] = []
    for group in grid["stages"][stage]["groups"]:
        if "channels" in group:
            rgbs = [tuple(CHANNELS[c][i] * code for i in range(3))
                    for c in group["channels"] for code in group["codes"]]
        else:
            rgbs = [tuple(int(v) for v in comp) for comp in group["compositions"]]
        for rgb in rgbs:
            for level in group["levels"]:
                for duty in group["duties"]:
                    s = Setting(rgb, int(level), int(duty), group["name"])
                    if sum(1 for v in rgb if v > 0) > 1 and level > multi_max:
                        raise ValueError(f"{s.label}: multi-channel stimulus above brightness {multi_max}")
                    if is_white(rgb) and level > WHITE_MAX_BRIGHTNESS:
                        raise ValueError(f"{s.label}: white above brightness {WHITE_MAX_BRIGHTNESS}")
                    out.append(s)
    labels = [s.label for s in out]
    if len(labels) != len(set(labels)):
        raise ValueError(f"duplicate settings in stage {stage!r}")
    return out


def plan_blocks(settings: list[Setting], *, stage: str, seed: int, session_index: int) -> list[Block]:
    """Group settings by hardware state; order blocks and the settings within each block from a seeded
    permutation, reversed on even session indices so drift is counterbalanced across sessions."""
    by_state: dict[tuple[int, int], list[Setting]] = {}
    for s in settings:
        by_state.setdefault((s.level, s.duty), []).append(s)
    states = sorted(by_state)
    random.Random(f"{stage}:{seed}:blocks").shuffle(states)
    reverse = session_index % 2 == 0
    if reverse:
        states.reverse()
    blocks = []
    for level, duty in states:
        inner = sorted(by_state[(level, duty)], key=lambda s: s.label)
        random.Random(f"{stage}:{seed}:{level}:{duty}").shuffle(inner)
        if reverse:
            inner.reverse()
        blocks.append(Block(level, duty, tuple(inner)))
    return blocks


def _merge_meta(session_dir: Path, sub: str, label: str, fields: dict[str, Any]) -> None:
    path = session_dir / sub / f"{label}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(fields)
    write_json(path, meta)


def run_model_sweep(
    meter: MeterLike,
    host: HostClient,
    session_dir: Path,
    mopts: ModelOptions,
    *,
    prompt: Callable[[str], str] = input,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
) -> dict[str, Any]:
    grid = load_grid(mopts.grid)
    duties = stage_duties(grid, mopts.stage)
    settings = stage_settings(grid, mopts.stage)  # refuse a bad grid before touching hardware
    session_dir.mkdir(parents=True, exist_ok=True)

    host_info = host.info()
    backend = host_info["state"].get("backend", "")
    if backend == "mock" and not mopts.measure.allow_mock:
        raise HostError("host is running --device mock; pass --allow-mock to measure anyway")
    if host_info.get("extra", {}).get("fullscreen") != "1":
        out("WARNING: stimulus window is not fullscreen (send `fullscreen on` or press P)")
    initial_brightness = int(host_info["state"].get("brightness", "0"))
    initial_duty = host_info["state"].get("duty_cycle")

    opts = replace(mopts.measure, sequence=f"model_{mopts.stage}", base_brightness=None,
                   anchor_every=mopts.anchor_every)
    if opts.calibration_version:
        host.set_calibration(opts.calibration_version)
    log_reply = host.log_start()

    meter_info = asdict(meter.info) if hasattr(meter.info, "__dataclass_fields__") else dict(meter.info)
    session: dict[str, Any] = {
        "schema": "chronolume-uprtek-model-session/1",
        "status": "running",
        "started": now_iso(),
        "finished": None,
        "package_version": __version__,
        "python": f"{platform.python_version()} {platform.architecture()[0]}",
        "model_stage": mopts.stage,
        "stage_role": grid["stages"][mopts.stage]["role"],
        "grid": {"version": grid["version"], "path": str(mopts.grid) if mopts.grid else DEFAULT_GRID},
        "session_index": mopts.session_index,
        "seed": mopts.seed,
        "reseated": mopts.reseated,
        "wear_detection": mopts.wear_detection,
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
            "session_log": log_reply.get("extra", {}).get("log_path", ""),
        },
        "options": {k: (str(v) if isinstance(v, Path) else v)
                    for k, v in asdict(mopts).items() if k != "measure"},
        "measure_options": asdict(opts),
        "duty_acceptance": None,
        "base_duty": None,
        "dark": None,
        "warmup": None,
        "blocks": [],
        "anchors": [],
        "captures": [],
        "stop_reason": None,
    }
    session_path = session_dir / "session.json"
    write_json(session_path, session)

    try:
        acceptance = duty_acceptance(host, duties, initial_duty)
        session["duty_acceptance"] = acceptance
        base_duty = max(acceptance["duties"])
        opts = replace(opts, duty=base_duty)
        session["base_duty"] = base_duty
        blocks = plan_blocks(settings, stage=mopts.stage, seed=mopts.seed, session_index=mopts.session_index)
        session["blocks"] = [{"index": i, "level": b.level, "duty": b.duty,
                              "settings": [s.label for s in b.settings]} for i, b in enumerate(blocks)]
        write_json(session_path, session)
        n_settings = sum(len(b.settings) for b in blocks)
        out(f"stage {mopts.stage}: duty check {list(duties)} passed, base duty {base_duty}, "
            f"{len(blocks)} blocks, {n_settings} settings")

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

        order = 0
        reference: float | None = None

        def anchor(after: str | None, block: Block) -> dict[str, Any]:
            rec = _anchor(meter, host, session_dir, len(session["anchors"]), after, block.level,
                          reference, opts, prompt=prompt, sleep=sleep, out=out, restore_duty=block.duty)
            _merge_meta(session_dir, "anchors/raw", rec["label"],
                        {"role": "anchor", "acquisition_order": order, "model_stage": mopts.stage})
            session["anchors"].append(rec)
            write_json(session_path, session)
            return rec

        by_block = mopts.anchor_every == ANCHOR_BLOCK
        every = 0 if by_block else int(mopts.anchor_every)
        if by_block or every > 0:
            for attempt in range(REFERENCE_ANCHOR_TRIES):
                order += 1
                rec = anchor(None, blocks[0])
                if not rec["flags"]:
                    reference = rec["lux_median"]
                    break
                if attempt + 1 < REFERENCE_ANCHOR_TRIES:
                    prompt(f"Reference anchor failed ({' '.join(rec['flags'])}). Check the glasses are "
                           "awake and showing blue, then press Enter… ")
            else:
                raise HostError("reference anchor failed; check the glasses and probe, or pass --anchor-every 0")

        def capture(step: Step, role: str, block_index: int, setting: Setting | None) -> dict[str, Any]:
            nonlocal order
            order += 1
            meta = _run_step(meter, host, session_dir, step, order, opts, step.brightness,
                             prompt=prompt, sleep=sleep, out=out)
            fields = {"role": role, "model_stage": mopts.stage, "block_index": block_index,
                      "acquisition_order": order, "uniform_full_frame": True,
                      "group": setting.group if setting else None}
            _merge_meta(session_dir, "raw", step.label, fields)
            rec = {"label": step.label, "rgb": list(step.rgb), "level": step.brightness, "duty": step.duty,
                   "flags": meta["flags"], **fields}
            session["captures"].append(rec)
            write_json(session_path, session)
            return rec

        since_anchor: list[str] = []
        anchor_failures = 0

        def checked_anchor(after: str, restore: Block) -> None:
            """Anchor, retried once; on failure the settings since the last anchor are flagged."""
            nonlocal order, anchor_failures, since_anchor
            order += 1
            rec = anchor(after, restore)
            if rec["flags"]:
                sleep(opts.retry_wait_s)
                order += 1
                rec = anchor(after, restore)
            if rec["flags"]:
                anchor_failures += 1
                for c in session["captures"]:
                    if c["label"] in since_anchor and "ANCHOR_DRIFT" not in c["flags"]:
                        c["flags"].append("ANCHOR_DRIFT")
                        _merge_meta(session_dir, "raw", c["label"], {"flags": c["flags"]})
                if anchor_failures >= ANCHOR_FAILURES_TO_STOP:
                    session["stop_reason"] = (f"{anchor_failures} consecutive failed anchors "
                                              f"({' '.join(rec['flags'])})")
                    raise HostError(session["stop_reason"])
                prompt(f"Anchor {rec['label']} failed ({' '.join(rec['flags'])}); settings "
                       f"{since_anchor[0]}..{since_anchor[-1]} are flagged ANCHOR_DRIFT. Check the "
                       "glasses are awake and the probe has not moved, then press Enter… ")
            else:
                anchor_failures = 0
            since_anchor = []

        for bi, block in enumerate(blocks):
            tag = f"L{block.level}_D{block.duty}"
            capture(Step(f"K_{tag}_start", (0, 0, 0), block.level, block.duty), "black_start", bi, None)
            for setting in block.settings:
                step = Step(setting.label, setting.rgb, setting.level, setting.duty)
                capture(step, "stimulus", bi, setting)
                since_anchor.append(step.label)
                if every > 0 and len(since_anchor) >= every:
                    checked_anchor(step.label, block)
            capture(Step(f"K_{tag}_end", (0, 0, 0), block.level, block.duty), "black_end", bi, None)
            if by_block and since_anchor:
                # Restore the next block's state, so its opening black needs no further change.
                checked_anchor(block.settings[-1].label, blocks[bi + 1] if bi + 1 < len(blocks) else block)
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


def describe_plan(grid: dict[str, Any], stage: str, seed: int, session_index: int,
                  anchor_every: int | str) -> str:
    blocks = plan_blocks(stage_settings(grid, stage), stage=stage, seed=seed, session_index=session_index)
    n = sum(len(b.settings) for b in blocks)
    if anchor_every == ANCHOR_BLOCK:
        anchors = len(blocks) + 1
    else:
        anchors = (n // int(anchor_every) + 1) if int(anchor_every) > 0 else 0
    lines = [f"stage {stage} ({grid['stages'][stage]['role']}), duties {sorted({b.duty for b in blocks}, reverse=True)}, "
             f"session {session_index}, "
             f"seed {seed}: {len(blocks)} blocks, {n} settings, {2 * len(blocks)} black, ~{anchors} anchors"]
    for i, b in enumerate(blocks):
        lines.append(f"  block {i:02d} L{b.level} D{b.duty}: " + " ".join(s.label for s in b.settings))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stage", required=True, choices=STAGES)
    p.add_argument("--session-index", type=int, default=1,
                   help="1, 2, 3 … for reseated sessions of a stage; even indices run the reversed order")
    p.add_argument("--seed", type=int, default=0, help="seed for the block and setting order")
    p.add_argument("--grid", type=Path, help=f"grid JSON (default: packaged {DEFAULT_GRID}; {GRID_V2} keeps "
                                              f"level 0 in the mixtures, {GRID_V1} is the earlier full "
                                              "primaries grid)")
    p.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    p.add_argument("--session-dir", type=Path, help="default: data/uprtek/model_<stage>_<timestamp>")
    p.add_argument("--data-root", type=Path, default=Path("data/uprtek"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--host-port", type=int, default=7777)
    p.add_argument("--allow-mock", action="store_true", help="measure even if the host is --device mock")
    p.add_argument("--eye", default="both", choices=["both", "left", "right"])
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--settle-s", type=float, default=1.0, help="dwell after each code change")
    p.add_argument("--brightness-settle-s", type=float, default=3.0,
                   help="dwell after each brightness or duty change")
    p.add_argument("--max-exposure-ms", type=int, default=DEFAULT_MAX_EXPOSURE_MS)
    p.add_argument("--anchor-every", default=ANCHOR_BLOCK,
                   help="anchor (B255, brightness 8, base duty) after every block (`block`, default), or "
                        "after every N settings; 0 disables")
    p.add_argument("--calibration-version", help="tag sent to the host and logged with every event")
    p.add_argument("--skip-dark", action="store_true", help="skip mk_Msr_Dark (capped-sensor) step")
    p.add_argument("--warmup-s", type=float, default=0.0, help="present (0,0,255) before the stage")
    p.add_argument("--wear-detection", default="unknown", choices=["disabled", "enabled", "unknown"],
                   help="Wearer Detection setting (not readable over the SDK; recorded as given)")
    p.add_argument("--not-reseated", action="store_true",
                   help="the glasses were not reseated on the fixture since the previous session")
    p.add_argument("--meter-sn", help="optical serial number if several meters are connected")
    p.add_argument("--mkusb-dir", help="folder containing mkusb.dll (default: uSpectrum VC example)")
    p.add_argument("--notes", default="")
    p.add_argument("--overwrite", action="store_true", help="allow writing into a session with captures")
    args = p.parse_args(argv)

    if args.anchor_every != ANCHOR_BLOCK:
        try:
            args.anchor_every = int(args.anchor_every)
        except ValueError:
            p.error("--anchor-every must be `block` or an integer")
    grid = load_grid(args.grid)
    if args.dry_run:
        print(describe_plan(grid, args.stage, args.seed, args.session_index, args.anchor_every))
        return 0
    if args.repeats < 1:
        p.error("--repeats must be >= 1")
    if args.anchor_every != ANCHOR_BLOCK and args.anchor_every < 0:
        p.error("--anchor-every must be >= 0")
    import datetime as _dt
    session_dir = args.session_dir or args.data_root / _dt.datetime.now().strftime(
        f"model_{args.stage}_s{args.session_index}_%Y%m%d_%H%M%S")
    if session_dir.exists() and not args.overwrite:
        try:
            if list_captures(session_dir):
                p.error(f"{session_dir} already has captures; use --overwrite or a new --session-dir")
        except FileNotFoundError:
            pass

    mopts = ModelOptions(
        stage=args.stage,
        session_index=args.session_index,
        seed=args.seed,
        grid=args.grid,
        anchor_every=args.anchor_every,
        wear_detection=args.wear_detection,
        reseated=not args.not_reseated,
        measure=SweepOptions(
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
        ),
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
            session = run_model_sweep(meter, host, session_dir, mopts)
    except HostError as exc:
        print(f"host: {exc}", file=sys.stderr)
        return 2
    finally:
        meter.close()
    print(f"session {session['status']}: {session_dir}")
    return 0 if session["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())

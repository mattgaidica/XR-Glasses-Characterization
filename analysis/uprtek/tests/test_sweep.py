import argparse
import json

import pytest

from fakes import FakeHost, FakeMeter
from uprtek import sequences
from uprtek.host_client import HostClient, HostError
from uprtek.rawio import list_captures, read_capture
from uprtek.sequences import WHITE_MAX_BRIGHTNESS, Step
from uprtek.sweep import (
    EXPOSURE_RAW_MAX,
    SweepOptions,
    _parse_duty,
    no_display_light,
    outlier_reps,
    range_cause,
    run_sweep,
)


def _run(tmp_path, host, meter=None, prompts=None, **kw):
    kw.setdefault("repeats", 2)
    kw.setdefault("retry_wait_s", 0)
    opts = SweepOptions(settle_s=0, brightness_settle_s=0, **kw)
    meter = meter or FakeMeter(host)
    asked = prompts if prompts is not None else []
    with HostClient("127.0.0.1", host.port) as client:
        session = run_sweep(meter, client, tmp_path, opts, prompt=lambda t: asked.append(t) or "",
                            sleep=lambda _: None, out=lambda _: None)
    return session, meter


def test_hosi_set_writes_every_capture_and_restores_state(tmp_path):
    host = FakeHost()
    session, meter = _run(tmp_path, host, sequence="hosi_set", calibration_version="cal_v001")
    host.close()
    assert session["status"] == "complete"
    assert meter.dark_calls == 1
    labels = [p.stem for p in list_captures(tmp_path)]
    assert len(labels) == 19 and "B255_br8" in labels and "K" in labels
    cap = read_capture(tmp_path / "raw" / "B255_br3.csv")
    assert len(cap.repeats) == 2
    assert cap.meta["host_state"]["brightness"] == "3"
    assert cap.meta["host_state"]["calibration"] == "cal_v001"
    assert cap.meta["brightness_source"] == "sdk"
    assert cap.meta["readback_ok"] is True
    assert "brightness 5" in host.commands  # restored initial brightness
    assert host.commands[-2:] == ["brightness 5", "log stop"]
    saved = json.loads((tmp_path / "session.json").read_text())
    assert saved["status"] == "complete" and saved["meter"]["optical_sn"] == "FAKE0001"


def test_mock_backend_refused_unless_allowed(tmp_path):
    host = FakeHost(backend="mock")
    with pytest.raises(HostError):
        _run(tmp_path, host, sequence="primaries", skip_dark=True)
    host.close()


def test_brightness_failure_aborts_without_manual_flag(tmp_path):
    host = FakeHost(brightness_ok=False)
    with pytest.raises(HostError):
        _run(tmp_path, host, sequence="brightness_sweep", skip_dark=True, brightness_levels=(0, 1))
    host.close()
    assert json.loads((tmp_path / "session.json").read_text())["status"] == "error"


def test_manual_brightness_is_flagged(tmp_path):
    host = FakeHost(brightness_ok=False)
    session, _ = _run(tmp_path, host, sequence="brightness_sweep", skip_dark=True,
                      brightness_levels=(2,), manual_brightness=True)
    host.close()
    assert session["status"] == "complete"
    cap = read_capture(tmp_path / "raw" / "B255_br2.csv")
    assert cap.meta["brightness_source"] == "manual_operator"
    assert "BRIGHTNESS_MANUAL" in cap.meta["flags"]


def test_left_eye_readback(tmp_path):
    host = FakeHost()
    _run(tmp_path, host, sequence="primaries", skip_dark=True, eye="left")
    host.close()
    cap = read_capture(tmp_path / "raw" / "B255.csv")
    assert cap.meta["readback"]["right"] == [0, 0, 0]
    assert cap.meta["readback_ok"] is True


def _cap(strength, exposure, lux):
    return {"light_strength": strength, "instrument": {"exposure_time_raw": exposure, "lux": lux}}


def test_range_cause():
    assert range_cause(_cap(0, 120000.0, 21.0)) is None
    assert range_cause(_cap(2, EXPOSURE_RAW_MAX, 0.01)) == "low"
    assert range_cause(_cap(2, 3600.0, 20.08)) == "saturated"  # observed: B255 at brightness 8
    assert range_cause(_cap(2, 4800.0, 0.3)) == "unknown"  # dark noise at a short exposure
    assert range_cause(_cap(2, 120000.0, 5.0)) == "unknown"
    assert range_cause({"light_strength": 2}) == "unknown"


def test_out_of_range_is_flagged_with_cause(tmp_path):
    host = FakeHost()
    meter = FakeMeter(host)
    meter.light_strength = 2
    meter.exposure_time_raw = EXPOSURE_RAW_MAX
    meter.lux = 0.01
    _run(tmp_path, host, meter=meter, sequence="primaries", skip_dark=True)
    host.close()
    cap = read_capture(tmp_path / "raw" / "B255.csv")
    assert "OUT_OF_RANGE" in cap.meta["flags"]
    assert "OVER_EXPOSED" not in cap.meta["flags"]
    assert cap.meta["out_of_range_causes"] == ["low"]
    assert [r["range_cause"] for r in cap.meta["repeats"]] == ["low", "low"]


def test_max_exposure_is_set_and_restored(tmp_path):
    host = FakeHost()
    meter = FakeMeter(host)
    meter.light_strength = 2
    meter.exposure_time_raw = EXPOSURE_RAW_MAX  # 500 ms: below a 5 s ceiling, so not "low"
    meter.lux = 0.01
    session, _ = _run(tmp_path, host, meter=meter, sequence="primaries", skip_dark=True,
                      max_exposure_ms=5000)
    host.close()
    assert meter.max_exposure_calls == [5000, 500]
    assert session["meter_max_exposure_ms"] == 5000
    assert session["meter_max_exposure_restored_ms"] == 500
    cap = read_capture(tmp_path / "raw" / "B255.csv")
    assert cap.meta["max_exposure_ms"] == 5000
    assert cap.meta["out_of_range_causes"] == ["unknown"]
    assert range_cause(_cap(2, 5_000_000.0, 0.01), 5_000_000.0) == "low"


def test_outlier_reps():
    assert outlier_reps([0.0196, 20.08, 20.87]) == [0]
    assert outlier_reps([0.4976, 1.438, 1.440]) == [0]
    assert outlier_reps([13.52, 13.45, 13.48]) == []
    assert outlier_reps([0.0001, 0.0052, 0.0121]) == []  # black, below OUTLIER_MIN_LUX
    assert outlier_reps([1.0, None, 1.1]) == []
    assert outlier_reps([1.0, 2.0]) == []


def test_priming_capture_is_discarded(tmp_path):
    host = FakeHost()
    meter = FakeMeter(host)
    calls = []
    original = meter.capture

    def counting_capture(**kw):
        calls.append(kw)
        return original(**kw)

    meter.capture = counting_capture
    _run(tmp_path, host, meter=meter, sequence="primaries", skip_dark=True, priming=1)
    host.close()
    assert len(calls) == 5 * (1 + 2)  # 5 steps x (1 priming + 2 repeats)
    cap = read_capture(tmp_path / "raw" / "B255.csv")
    assert len(cap.repeats) == 2
    assert len(cap.meta["priming"]) == 1
    assert cap.meta["outlier_reps"] == []


def test_priming_at_exposure_ceiling_is_kept_as_first_repeat(tmp_path):
    host = FakeHost()
    meter = FakeMeter(host)
    meter.exposure_time_raw = 5_000_000.0  # pinned at a 5 s ceiling
    calls = []
    original = meter.capture

    def counting_capture(**kw):
        calls.append(kw)
        return original(**kw)

    meter.capture = counting_capture
    _run(tmp_path, host, meter=meter, sequence="primaries", skip_dark=True, priming=1,
         max_exposure_ms=5000)
    host.close()
    assert len(calls) == 5 * 2  # 5 steps x 2 repeats, the first taken by the priming capture
    cap = read_capture(tmp_path / "raw" / "K.csv")
    assert len(cap.repeats) == 2
    assert cap.meta["priming"][0]["kept_as_rep0"] is True
    assert [r["from_priming"] for r in cap.meta["repeats"]] == [True, False]


def test_dark_calibration_keeps_the_panel_lit(tmp_path):
    host = FakeHost()
    session, meter = _run(tmp_path, host, sequence="repeatability")
    host.close()
    assert meter.dark_calls == 1
    assert "rgb 0 0 255 dark_cal" in host.commands
    assert session["dark"]["duration_s"] >= 0


def test_anchor_at_base_brightness_makes_no_brightness_change(tmp_path):
    host = FakeHost()
    _run(tmp_path, host, sequence="primaries", skip_dark=True, anchor_every=2, base_brightness=8)
    host.close()
    # Only the white cap moves brightness off the base; the anchors themselves add no change.
    assert [c for c in host.commands if c.startswith("brightness")] == [
        "brightness 8", "brightness 7", "brightness 8", "brightness 5"]


def _white_levels(commands, initial=5):
    """Brightness level in force each time a white stimulus was shown."""
    level, levels = initial, []
    for c in commands:
        if c.startswith("brightness "):
            level = int(c.split()[1])
        elif c.startswith("rgb "):
            r, g, b = (int(v) for v in c.split()[1:4])
            if r == g == b > 0:
                levels.append(level)
    return levels


def test_white_is_never_shown_above_its_limit(tmp_path):
    host = FakeHost()
    _run(tmp_path, host, sequence="color_set", skip_dark=True, base_brightness=8, anchor_every=5)
    host.close()
    levels = _white_levels(host.commands)
    assert levels and max(levels) == WHITE_MAX_BRIGHTNESS
    w = read_capture(tmp_path / "raw" / "W255.csv").meta
    assert w["host_state"]["brightness"] == str(WHITE_MAX_BRIGHTNESS)
    assert w["white_brightness_capped"] is True
    assert read_capture(tmp_path / "raw" / "W192.csv").meta["white_brightness_capped"] is True
    assert read_capture(tmp_path / "raw" / "W255_br7.csv").meta["white_brightness_capped"] is False
    assert read_capture(tmp_path / "raw" / "R255.csv").meta["host_state"]["brightness"] == "8"
    assert not (tmp_path / "raw" / "W255_br8.csv").exists()


def test_white_capped_with_brightness_keep_then_level_restored(tmp_path):
    host = FakeHost()
    host.state["brightness"] = "8"
    _run(tmp_path, host, sequence="primaries", skip_dark=True, anchor_every=0)
    host.close()
    assert _white_levels(host.commands, initial=8) == [WHITE_MAX_BRIGHTNESS]
    assert read_capture(tmp_path / "raw" / "K.csv").meta["host_state"]["brightness"] == "8"


def test_explicit_white_above_limit_is_refused_before_anything_is_shown(tmp_path, monkeypatch):
    monkeypatch.setitem(sequences.SEQUENCES, "too_bright",
                        lambda levels=None: [Step("W255_br8", (255, 255, 255), brightness=8)])
    host = FakeHost()
    with pytest.raises(ValueError, match="white above brightness"):
        _run(tmp_path, host, sequence="too_bright", skip_dark=True)
    host.close()
    assert not any(c.startswith(("rgb", "brightness")) for c in host.commands)


def test_steps_without_own_brightness_return_to_base(tmp_path):
    host = FakeHost()
    _run(tmp_path, host, sequence="color_set", skip_dark=True, base_brightness=8,
         brightness_levels=(8, 3), anchor_every=0)
    host.close()
    k = read_capture(tmp_path / "raw" / "K.csv")
    assert k.meta["host_state"]["brightness"] == "8"
    assert k.meta["requested_brightness"] is None
    assert read_capture(tmp_path / "raw" / "W255_br3.csv").meta["host_state"]["brightness"] == "3"
    # Base 8; W255 and the W ladder drop to the white cap of 7 and return; the level-8 group needs no
    # change; level 3 is set once; the duty block (42 then 30, every preset but the kept 98) runs R, G,
    # B at 8 and W at 7; then the initial 5 is restored.
    duty_block = ["brightness 8", "brightness 7"] * 2
    assert [c for c in host.commands if c.startswith("brightness")] == [
            "brightness 8", "brightness 7", "brightness 8", "brightness 7", "brightness 8",
            "brightness 3", *duty_block, "brightness 5"]


def test_no_display_light():
    dark = {"instrument": {"lambda_peak_nm": 766.0}}
    blue = {"instrument": {"lambda_peak_nm": 459.0}}
    assert no_display_light(dark, (0, 0, 255))
    assert not no_display_light(blue, (0, 0, 255))
    assert not no_display_light(dark, (0, 0, 0))  # black is expected to read dark
    assert not no_display_light({"instrument": {"lambda_peak_nm": 631.0}}, (255, 0, 0))


def test_dark_display_is_retried(tmp_path):
    host = FakeHost()
    meter = FakeMeter(host)
    meter.dark_captures = 2  # first rep of the first step reads dark twice
    _run(tmp_path, host, meter=meter, sequence="blue_ladder", skip_dark=True, priming=0,
         max_retries=3, retry_wait_s=0)
    host.close()
    cap = read_capture(tmp_path / "raw" / "B016.csv")
    assert len(cap.meta["rejected"]) == 2
    assert cap.meta["repeats"][0]["attempts"] == 3
    assert "NO_DISPLAY_LIGHT" not in cap.meta["flags"]


def test_dark_display_flagged_when_retries_run_out(tmp_path):
    host = FakeHost()
    meter = FakeMeter(host)
    meter.dark_captures = 100
    _run(tmp_path, host, meter=meter, sequence="primaries", skip_dark=True, priming=0,
         max_retries=1, retry_wait_s=0)
    host.close()
    k = read_capture(tmp_path / "raw" / "K.csv")
    assert "NO_DISPLAY_LIGHT" not in k.meta["flags"]
    b = read_capture(tmp_path / "raw" / "B255.csv")
    assert "NO_DISPLAY_LIGHT" in b.meta["flags"]
    assert b.meta["repeats"][0]["attempts"] == 2
    assert b.meta["capture_attempts"] == 4  # 3 whole-capture re-runs, all dark
    assert len(b.meta["discarded_attempts"]) == 3


def test_minimized_window_is_restored_before_measuring(tmp_path):
    host = FakeHost()
    host.minimized = True
    prompts = []
    _run(tmp_path, host, prompts=prompts, sequence="primaries", skip_dark=True)
    host.close()
    r = read_capture(tmp_path / "raw" / "R255.csv")
    assert r.meta["readback"]["fb_w"] == 3840
    assert r.meta["readback_fixes"][0]["readback"]["fb_w"] == 0
    assert read_capture(tmp_path / "raw" / "G255.csv").meta["readback_fixes"] == []
    assert "fullscreen on" in host.commands
    assert prompts == []


def test_stimulus_not_visible_stops_sweep(tmp_path):
    host = FakeHost()
    host.minimized = True
    host.fullscreen_restores = False
    prompts = []
    with pytest.raises(HostError, match="R255: stimulus"):
        _run(tmp_path, host, prompts=prompts, sequence="primaries", skip_dark=True)
    host.close()
    assert len(prompts) == 2
    assert json.loads((tmp_path / "session.json").read_text())["status"] == "error"


def test_reference_anchor_requires_visible_stimulus(tmp_path):
    host = FakeHost()
    host.minimized = True
    host.fullscreen_restores = False
    with pytest.raises(HostError, match="ANCHOR_00"):
        _run(tmp_path, host, sequence="primaries", skip_dark=True, anchor_every=2)
    host.close()


def test_brightness_changes_happen_on_black(tmp_path):
    host = FakeHost()
    _run(tmp_path, host, sequence="hosi_set", skip_dark=True, anchor_every=5, base_brightness=7,
         brightness_levels=(7, 8))
    host.close()
    for i, cmd in enumerate(host.commands):
        if cmd.startswith("brightness"):
            assert host.commands[i - 1].startswith("rgb 0 0 0"), host.commands[i - 1]
    cap = read_capture(tmp_path / "raw" / "B255_br8.csv")
    assert cap.meta["readback"]["left"] == [0, 0, 255]  # stimulus re-presented before measuring


def test_base_brightness_is_set_and_restored(tmp_path):
    host = FakeHost()
    session, _ = _run(tmp_path, host, sequence="primaries", skip_dark=True, base_brightness=7)
    host.close()
    assert session["base_brightness"]["reported"] == "7"
    assert read_capture(tmp_path / "raw" / "B255.csv").meta["host_state"]["brightness"] == "7"
    assert session["host"]["restored_brightness"] == "5"


def test_duty_is_applied_logged_and_restored(tmp_path):
    host = FakeHost()
    session, _ = _run(tmp_path, host, sequence="primaries", skip_dark=True, duty=42)
    host.close()
    assert session["duty"]["reported"] == "42"
    cap = read_capture(tmp_path / "raw" / "B255.csv")
    assert cap.meta["host_state"]["duty_cycle"] == "42"
    assert cap.meta["requested_duty_cycle"] == 42
    assert "DUTY_MISMATCH" not in cap.meta["flags"]
    assert host.commands[-2:] == ["duty 98", "log stop"]
    assert session["host"]["restored_duty_cycle"] == "98"


def test_duty_reapplied_when_brightness_moves_it(tmp_path):
    host = FakeHost(duty_after_brightness="50")
    _run(tmp_path, host, sequence="brightness_sweep", skip_dark=True, duty=98, brightness_levels=(3,))
    host.close()
    cap = read_capture(tmp_path / "raw" / "B255_br3.csv")
    assert cap.meta["duty_reapplied_from"] == "50"
    assert cap.meta["host_state"]["duty_cycle"] == "98"
    assert "DUTY_MISMATCH" not in cap.meta["flags"]


def test_color_set_checks_presets_and_runs_duty_block(tmp_path):
    host = FakeHost()
    session, _ = _run(tmp_path, host, sequence="color_set", skip_dark=True, duty=98,
                      brightness_levels=(8,), base_brightness=8)
    host.close()
    assert session["status"] == "complete"
    assert session["duty_acceptance"]["duties"] == [98, 42, 30]
    assert session["duty"]["reported"] == "98"
    assert read_capture(tmp_path / "raw" / "B255.csv").meta["host_state"]["duty_cycle"] == "98"
    cap = read_capture(tmp_path / "raw" / "W255_br7_d42.csv")
    assert cap.meta["requested_duty_cycle"] == 42
    assert cap.meta["host_state"]["duty_cycle"] == "42"
    assert cap.meta["host_state"]["brightness"] == "7"
    assert "DUTY_MISMATCH" not in cap.meta["flags"]
    labels = {p.stem for p in list_captures(tmp_path)}
    assert {"B255_d42", "W255_br7_d30"} <= labels
    assert session["host"]["restored_duty_cycle"] == "98"


def test_duty_check_fails_when_a_preset_is_rejected(tmp_path):
    host = FakeHost(duty_rejects=frozenset({30}))
    with pytest.raises(HostError):
        _run(tmp_path, host, sequence="color_set", skip_dark=True, duty=98, brightness_levels=(8,))
    host.close()


def test_duty_must_be_a_preset(tmp_path):
    host = FakeHost()
    with pytest.raises(ValueError):
        _run(tmp_path, host, sequence="primaries", skip_dark=True, duty=60)
    host.close()
    assert _parse_duty("keep") is None and _parse_duty("42") == 42
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_duty("100")


def test_outlier_capture_is_rerun(tmp_path):
    host = FakeHost()
    meter = FakeMeter(host)
    meter.lux_queue = [1.0, 10.0, 10.0, 10.0, 10.1, 9.9]  # first attempt has an outlier
    _run(tmp_path, host, meter=meter, sequence="repeatability", skip_dark=True, priming=0, repeats=3)
    host.close()
    cap = read_capture(tmp_path / "raw" / "B128.csv")
    assert cap.meta["capture_attempts"] == 2
    assert cap.meta["discarded_attempts"][0]["flags"] == ["REPEAT_OUTLIER"]
    assert "REPEAT_OUTLIER" not in cap.meta["flags"]
    assert [r["instrument"]["lux"] for r in cap.meta["repeats"]] == [10.0, 10.1, 9.9]


def test_anchors_are_kept_out_of_raw_and_restore_brightness(tmp_path):
    host = FakeHost()
    session, _ = _run(tmp_path, host, sequence="primaries", skip_dark=True, anchor_every=2)
    host.close()
    assert [a["label"] for a in session["anchors"]] == [f"ANCHOR_{i:02d}" for i in range(4)]
    assert all(not a["flags"] for a in session["anchors"])
    assert [p.stem for p in list_captures(tmp_path)] == ["B255", "G255", "K", "R255", "W255"]
    assert (tmp_path / "anchors" / "raw" / "ANCHOR_00.csv").exists()
    cap = read_capture(tmp_path / "raw" / "G255.csv")
    assert cap.meta["host_state"]["brightness"] == "5"  # restored after ANCHOR_01


def test_anchor_drift_reruns_block(tmp_path):
    host = FakeHost()
    meter = FakeMeter(host)
    meter.dim_labels = {"ANCHOR_01": 2, "ANCHOR_02": 2}  # anchor and its retry read dim
    prompts = []
    session, _ = _run(tmp_path, host, meter=meter, prompts=prompts, sequence="primaries",
                      skip_dark=True, anchor_every=2, priming=0)
    host.close()
    assert session["status"] == "complete"
    assert session["block_reruns"][0]["steps"] == ["R255", "G255"]
    assert any("re-run R255..G255" in p for p in prompts)
    assert session["anchors"][3]["flags"] == []
    for label in ("R255", "G255", "B255"):
        assert "ANCHOR_DRIFT" not in read_capture(tmp_path / "raw" / f"{label}.csv").meta["flags"]


def test_persistent_anchor_drift_flags_block(tmp_path):
    host = FakeHost()
    meter = FakeMeter(host)
    meter.dim_labels = {f"ANCHOR_{i:02d}": 2 for i in range(1, 7)}
    session, _ = _run(tmp_path, host, meter=meter, sequence="primaries", skip_dark=True,
                      anchor_every=2, priming=0)
    host.close()
    assert len(session["block_reruns"]) == 2
    assert "ANCHOR_DRIFT" in read_capture(tmp_path / "raw" / "R255.csv").meta["flags"]
    assert "ANCHOR_DRIFT" in read_capture(tmp_path / "raw" / "G255.csv").meta["flags"]
    assert "ANCHOR_DRIFT" not in read_capture(tmp_path / "raw" / "B255.csv").meta["flags"]

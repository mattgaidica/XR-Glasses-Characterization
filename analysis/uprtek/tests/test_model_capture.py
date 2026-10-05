import json
from importlib import resources

import pytest

from fakes import FakeHost, FakeMeter
from uprtek.host_client import HostClient, HostError
from uprtek.model.capture import (
    GRID_V1,
    GRID_V2,
    ModelOptions,
    load_grid,
    plan_blocks,
    run_model_sweep,
    stage_duties,
    stage_settings,
)
from uprtek.sequences import WHITE_MAX_BRIGHTNESS, is_white
from uprtek.sweep import SweepOptions


def test_grid_counts_match_spec():
    grid = load_grid()
    assert grid["version"] == "v3"
    assert len(stage_settings(grid, "primaries")) == 39
    assert len(stage_settings(grid, "ladders")) == 36
    assert len(stage_settings(grid, "mixtures")) == 76
    assert len(stage_settings(grid, "validation")) == 48
    v2 = load_grid(resources.files("uprtek").joinpath("data").joinpath(GRID_V2))
    assert len(stage_settings(v2, "mixtures")) == 171
    assert v2["stages"]["primaries"] == grid["stages"]["primaries"]
    v1 = load_grid(resources.files("uprtek").joinpath("data").joinpath(GRID_V1))
    assert len(stage_settings(v1, "primaries")) == 108
    assert v1["stages"]["mixtures"] == v2["stages"]["mixtures"]
    assert v1["stages"]["validation"] == v2["stages"]["validation"]


def test_v3_mixtures_skip_level_0_and_validation_stays_inside_their_levels():
    grid = load_grid()
    mix = stage_settings(grid, "mixtures")
    assert {s.level for s in mix} == {4, 7}
    lo, hi = min(s.level for s in mix), max(s.level for s in mix)
    val = stage_settings(grid, "validation")
    multi = [s for s in val if sum(1 for v in s.rgb if v) > 1]
    assert multi and all(lo <= s.level <= hi for s in multi)
    cal = mix + stage_settings(grid, "primaries") + stage_settings(grid, "ladders")
    val_prim = [s for s in val if sum(1 for v in s.rgb if v) == 1]
    # validation primaries are at unseen levels, between two levels with code ladders below full code
    assert not {s.level for s in val_prim} & {s.level for s in cal}
    ladder_levels = sorted({s.level for s in cal if sum(1 for v in s.rgb if v) == 1 and max(s.rgb) < 255})
    assert all(ladder_levels[0] < s.level < ladder_levels[-1] for s in val_prim)
    # validation mixtures are unseen compositions at levels the mixtures were not calibrated at
    assert not {s.rgb for s in multi} & {s.rgb for s in cal}
    assert not {s.level for s in multi} & {s.level for s in mix}


def test_v2_primaries_skip_blue_below_full_code_at_level_4():
    settings = stage_settings(load_grid(), "primaries")
    assert not [s for s in settings if s.rgb[2] and s.rgb[2] < 255 and s.level == 4]
    # every channel keeps a code ladder at the bright state and full code at both levels
    for i in range(3):
        assert {s.rgb[i] for s in settings if s.rgb[i] and s.level == 8} == {64, 128, 192, 255}
        assert {s.level for s in settings if s.rgb[i] == 255} == {4, 8}


def test_v3_ladders_are_primaries_at_the_base_duty():
    settings = stage_settings(load_grid(), "ladders")
    assert all(sum(1 for v in s.rgb if v) == 1 and s.duty == 98 for s in settings)
    assert {s.level for s in settings} == {2, 4, 5, 7}


def test_model_grid_never_white_or_mixtures_above_7():
    grid = load_grid()
    for stage in ("primaries", "ladders", "mixtures", "validation"):
        for s in stage_settings(grid, stage):
            if sum(1 for v in s.rgb if v > 0) > 1:
                assert s.level <= 7, s.label
            if is_white(s.rgb):
                assert s.level <= WHITE_MAX_BRIGHTNESS, s.label


def test_bad_grid_with_white_at_8_is_refused(tmp_path):
    grid = load_grid()
    grid["stages"]["mixtures"]["groups"][0]["levels"] = [8]
    path = tmp_path / "grid.json"
    path.write_text(json.dumps(grid))
    with pytest.raises(ValueError):
        stage_settings(load_grid(path), "mixtures")


def test_every_stage_checks_the_presets():
    for stage in ("primaries", "ladders", "mixtures", "validation"):
        assert stage_duties(load_grid(), stage) == (98, 42, 30)


def test_block_order_seeded_and_counterbalanced():
    settings = stage_settings(load_grid(), "primaries")
    a = plan_blocks(settings, stage="primaries", seed=7, session_index=1)
    b = plan_blocks(settings, stage="primaries", seed=7, session_index=1)
    c = plan_blocks(settings, stage="primaries", seed=7, session_index=2)
    assert a == b
    assert [(x.level, x.duty) for x in c] == [(x.level, x.duty) for x in reversed(a)]
    assert all(len({(s.level, s.duty) for s in blk.settings}) == 1 for blk in a)
    assert sum(len(blk.settings) for blk in a) == 39


def _run(tmp_path, host, stage="validation", **kw):
    measure = SweepOptions(repeats=1, settle_s=0, brightness_settle_s=0, retry_wait_s=0, skip_dark=True)
    mopts = ModelOptions(stage=stage, measure=measure, **kw)
    with HostClient("127.0.0.1", host.port) as client:
        return run_model_sweep(FakeMeter(host), client, tmp_path, mopts, prompt=lambda _: "",
                               sleep=lambda _: None, out=lambda _: None)


def test_validation_stage_runs_with_black_brackets_and_anchors(tmp_path):
    host = FakeHost()
    session = _run(tmp_path, host)
    host.close()
    assert session["status"] == "complete"
    assert session["duty_acceptance"]["duties"] == [98, 42, 30]
    assert session["base_duty"] == 98
    assert {b["duty"] for b in session["blocks"]} == {42, 98}
    roles = [c["role"] for c in session["captures"]]
    assert roles.count("stimulus") == 48
    assert roles.count("black_start") == roles.count("black_end") == len(session["blocks"]) == 6
    # each block opens and closes with black at its own hardware state
    for blk in session["blocks"]:
        caps = [c for c in session["captures"] if c["block_index"] == blk["index"]]
        assert caps[0]["role"] == "black_start" and caps[-1]["role"] == "black_end"
        assert {(c["level"], c["duty"]) for c in caps} == {(blk["level"], blk["duty"])}
    assert len(session["anchors"]) == 1 + len(session["blocks"])  # reference, then one per block
    meta = json.loads((tmp_path / "raw" / "C192-096-080_L6_D42.json").read_text())
    assert meta["host_state"]["duty_cycle"] == "42" and meta["host_state"]["brightness"] == "6"
    assert meta["role"] == "stimulus" and meta["requested_duty_cycle"] == 42
    assert meta["acquisition_order"] > 0
    assert session["host"]["restored_duty_cycle"] == "98"
    assert session["host"]["restored_brightness"] == "5"


def test_anchors_run_at_base_state_and_restore_block_state(tmp_path):
    host = FakeHost()
    session = _run(tmp_path, host)
    host.close()
    blocks = session["blocks"]
    assert any(b["duty"] != session["base_duty"] for b in blocks)
    for a in session["anchors"]:
        assert a["anchor_state"] == {"brightness": "8", "duty_cycle": str(session["base_duty"])}
        if a["after_step"] is None:
            blk = blocks[0]
        else:
            # a block anchor follows the block's last setting and restores the next block's state
            i = next(b["index"] for b in blocks if a["after_step"] in b["settings"])
            assert a["after_step"] == blocks[i]["settings"][-1]
            blk = blocks[min(i + 1, len(blocks) - 1)]
        assert a["restored_state"] == {"brightness": str(blk["level"]), "duty_cycle": str(blk["duty"])}
    # the capture after an anchor still reads back its block's own state
    for c in session["captures"]:
        meta = json.loads((tmp_path / "raw" / f"{c['label']}.json").read_text())
        assert meta["host_state"]["duty_cycle"] == str(c["duty"])
        assert meta["host_state"]["brightness"] == str(c["level"])


def test_block_anchors_never_change_state_inside_a_block(tmp_path):
    host = FakeHost()
    session = _run(tmp_path, host)
    host.close()
    labels = [c["label"] for c in session["captures"]]
    for a in session["anchors"][1:]:
        nxt = labels.index(a["after_step"]) + 1
        assert session["captures"][nxt]["role"] == "black_end"


def test_anchor_every_n_settings_still_available(tmp_path):
    host = FakeHost()
    session = _run(tmp_path, host, anchor_every=5)
    host.close()
    assert session["status"] == "complete"
    assert len(session["anchors"]) == 1 + 48 // 5


def test_model_duty_check_failure_stops(tmp_path):
    host = FakeHost(duty_rejects=frozenset({42}))
    with pytest.raises(HostError):
        _run(tmp_path, host)
    host.close()
    assert json.loads((tmp_path / "session.json").read_text())["status"] == "error"

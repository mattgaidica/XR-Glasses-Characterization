import json

import pytest

from fakes import FakeHost, FakeMeter
from uprtek.frames import describe, frame_hash, histogram
from uprtek.grid_capture import block_labels, color_order, describe_plan, full_label, run_grid_session
from uprtek.grid_frames import (CELLS, COLORS, GRID_LEVEL, PATTERNS, cell_rect, describe_pattern, pattern_hash,
                                pattern_name, render)
from uprtek.host_client import HostClient
from uprtek.sweep import SweepOptions


def test_cells_tile_the_frame_and_light_one_ninth():
    for w, h in ((1920, 1080), (320, 180), (100, 50)):
        lit = [0] * (w * h)
        for _, row, col in CELLS:
            x, y, cw, ch = cell_rect(row, col, w, h)
            for yy in range(y, y + ch):
                for xx in range(x, x + cw):
                    lit[yy * w + xx] += 1
        assert set(lit) == {1}
    for color, rgb in COLORS:
        for cell, _, _ in CELLS:
            px = render(pattern_name(color, cell), 1920, 1080)
            assert px == render(pattern_name(color, cell), 1920, 1080)
            assert dict(histogram(px)) == {(0, 0, 0): 1920 * 1080 * 8 // 9, rgb: 1920 * 1080 // 9}
    assert len(PATTERNS) == 36 and {p.level for p in PATTERNS} == {GRID_LEVEL}
    center = render("G_B255_C", 9, 9)
    assert center[(4 * 9 + 4) * 3:(4 * 9 + 4) * 3 + 3] == b"\x00\x00\xff" and center[:3] == b"\x00\x00\x00"


def test_geometric_hash_and_description_match_the_rendered_pixels():
    for w, h in ((90, 45), (100, 50), (64, 36)):
        for spec in PATTERNS:
            px = render(spec.name, w, h)
            assert pattern_hash(spec.name, w, h) == frame_hash(px)
            assert describe_pattern(spec.name, w, h) == describe(spec, px, w, h)
    px = render("G_W255_BR", 1920, 1080)
    assert pattern_hash("G_W255_BR") == frame_hash(px)


def test_block_order_and_plan():
    assert color_order(1) == ["R255", "G255", "B255", "W255"]
    assert color_order(2) == color_order(1)[::-1]
    labels = block_labels("B255")
    assert labels[0] == "K_B255_start" and labels[1] == full_label("B255") and labels[-1] == "K_B255_end"
    assert labels[2:-1] == [pattern_name("B255", c) for c, _, _ in CELLS] and labels[2] == "G_B255_C"
    assert "48 captures + 5 anchors" in describe_plan(1, 1920, 1080)


def _run(tmp_path, host):
    opts = SweepOptions(repeats=1, settle_s=0, brightness_settle_s=0, retry_wait_s=0, skip_dark=True)
    with HostClient("127.0.0.1", host.port) as client:
        return run_grid_session(FakeMeter(host), client, tmp_path / "session", opts, frames_dir=tmp_path / "frames",
                                prompt=lambda _: "", sleep=lambda _: None, out=lambda _: None)


def test_grid_session_runs_blocks_with_readback(tmp_path):
    host = FakeHost()
    host.fb = (90, 45)
    session = _run(tmp_path, host)
    host.close()
    assert session["status"] == "complete"
    assert [b["color"] for b in session["blocks"]] == color_order(1)
    assert len(session["anchors"]) == 1 + len(COLORS)
    for blk in session["blocks"]:
        caps = [c for c in session["captures"] if c["block_index"] == blk["index"]]
        assert [c["label"] for c in caps] == block_labels(blk["color"])
        assert [c["role"] for c in caps] == ["black_start", "full"] + ["cell"] * 9 + ["black_end"]
        assert {c["level"] for c in caps} == {GRID_LEVEL}
        for c in caps:
            if c["role"] == "cell":
                meta = json.loads((tmp_path / "session" / "raw" / f"{c['label']}.json").read_text())
                assert meta["readback"]["image"]["match"]
                assert meta["stimulus"]["hash"] == frame_hash(render(c["label"], 90, 45))
                assert meta["cell"] == c["cell"] and meta["color"] == blk["color"]


def test_grid_metrics_recovers_a_weight_map():
    pytest.importorskip("numpy")
    from uprtek.grid_check import CELL_NAMES, grid_metrics

    rel = {"C": 1.8, "T": 1.2, "B": 1.2, "L": 1.1, "R": 1.1, "TL": 0.7, "TR": 0.7, "BL": 0.6, "BR": 0.6}
    full = 40.0
    boost = 1.25  # small lit areas read 25 % brighter
    cells = {c: boost * rel[c] * full / 9.0 for c in CELL_NAMES}
    m = grid_metrics(full, cells)
    assert m["relative"]["C"] == pytest.approx(boost * 1.8)
    assert m["sum_over_full"] == pytest.approx(boost * sum(rel.values()) / 9.0)
    assert grid_metrics(None, cells)["weights"] is None
    assert grid_metrics(full, {**cells, "C": None})["sum_over_full"] is None


def test_grid_check_reads_a_session_without_writing_into_it(tmp_path):
    pytest.importorskip("numpy")
    from uprtek.grid_check import grid_report

    host = FakeHost()
    host.fb = (90, 45)
    _run(tmp_path, host)
    host.close()
    report = grid_report([tmp_path / "session"])
    assert not (tmp_path / "session" / "analysis").exists()
    blue = report["colors"]["B255"]["sessions"][0]
    assert blue["full"] is not None and set(blue["cells"]) == {c for c, _, _ in CELLS}
    assert report["colors"]["B255"]["mean"]["lux"]["n_sessions"] == 1

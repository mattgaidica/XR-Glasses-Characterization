import json

import pytest

from fakes import FakeHost, FakeMeter
from uprtek.frame_capture import frame_order, prepare_frames, reference_label, run_frame_session
from uprtek.frames import (BY_NAME, FRAMES, ORB_RINGS, WEBAPP_NIGHT_BG, WEBAPP_NIGHT_TEXT, describe, frame_hash,
                           histogram, read_ppm, render, write_ppm)
from uprtek.host_client import HostClient, HostError, image_readback_matches
from uprtek.sweep import SweepOptions


def test_fnv1a64_matches_reference_values():
    assert frame_hash(b"") == "cbf29ce484222325"
    assert frame_hash(b"a") == "af63dc4c8601ec8c"


def test_frames_are_deterministic_and_use_their_declared_colors():
    for spec in FRAMES:
        a, b = render(spec.name, 320, 180), render(spec.name, 320, 180)
        assert a == b and len(a) == 320 * 180 * 3
    f1 = {rgb for rgb, _ in histogram(render("F1_morning_text"))}
    assert f1 == {(0, 0, 0), (0, 0, 255)}
    f2 = {rgb for rgb, _ in histogram(render("F2_orb"))}
    assert f2 == {(0, 0, 0)} | {(0, 0, code) for _, code in ORB_RINGS}
    f3 = {rgb for rgb, _ in histogram(render("F3_night_text"))}
    assert f3 == {WEBAPP_NIGHT_BG, WEBAPP_NIGHT_TEXT}
    f4 = {rgb for rgb, _ in histogram(render("F4_red_orb"))}
    assert f4 == {(0, 0, 0)} | {(code, 0, 0) for _, code in ORB_RINGS}
    d = describe(BY_NAME["F2_orb"], render("F2_orb"), 1920, 1080)
    assert sum(c["fraction"] for c in d["colors"]) == pytest.approx(1.0)
    assert 0.4 < d["lit_fraction"] < 0.5
    # the night slide is the morning slide's text in other colors
    p1, p3 = render("F1_morning_text", 640, 360), render("F3_night_text", 640, 360)
    lit1 = [p1[i:i + 3] != b"\x00\x00\x00" for i in range(0, len(p1), 3)]
    lit3 = [p3[i:i + 3] == bytes(WEBAPP_NIGHT_TEXT) for i in range(0, len(p3), 3)]
    assert any(lit1) and lit1 == lit3
    assert BY_NAME["F3_night_text"].level <= 7 and not BY_NAME["F3_night_text"].composed_to_guidance


def test_ppm_round_trip(tmp_path):
    px = render("F1_morning_text", 64, 36)
    write_ppm(tmp_path / "f.ppm", 64, 36, px)
    assert read_ppm(tmp_path / "f.ppm") == (64, 36, px)


def _run(tmp_path, host, **kw):
    opts = SweepOptions(repeats=1, settle_s=0, brightness_settle_s=0, retry_wait_s=0, skip_dark=True)
    with HostClient("127.0.0.1", host.port) as client:
        return run_frame_session(FakeMeter(host), client, tmp_path / "session", opts, frames_dir=tmp_path / "frames",
                                 prompt=lambda _: "", sleep=lambda _: None, out=lambda _: None, **kw)


def test_frame_session_blocks_references_and_readback(tmp_path):
    host = FakeHost()
    host.fb = (320, 180)
    session = _run(tmp_path, host)
    host.close()
    assert session["status"] == "complete"
    assert [b["frame"] for b in session["blocks"]] == frame_order(1)
    assert len(session["anchors"]) == 1 + len(FRAMES)
    for blk in session["blocks"]:
        caps = [c for c in session["captures"] if c["block_index"] == blk["index"]]
        assert [c["role"] for c in caps][0] == "black_start" and caps[-1]["role"] == "black_end"
        assert {c["level"] for c in caps} == {BY_NAME[blk["frame"]].level}
        frame = next(c for c in caps if c["role"] == "frame")
        meta = json.loads((tmp_path / "session" / "raw" / f"{frame['label']}.json").read_text())
        assert meta["stimulus"]["kind"] == "image" and meta["readback"]["image"]["match"]
        assert meta["stimulus"]["hash"] == frame_hash(render(blk["frame"], 320, 180))
        refs = {tuple(c["rgb"]) for c in caps if c["role"] == "reference"}
        desc = next(f for f in session["frames"] if f["name"] == blk["frame"])
        assert refs and refs == {tuple(c["rgb"]) for c in desc["colors"]} - {(0, 0, 0)}
    assert reference_label("F3_night_text", WEBAPP_NIGHT_BG) in [c["label"] for c in session["captures"]]
    assert frame_order(2) == frame_order(1)[::-1]


def test_frame_session_analysis_nets_frames_and_weights_references(tmp_path):
    pytest.importorskip("numpy")
    from uprtek.model.frame_check import measured_frames

    host = FakeHost()
    host.fb = (320, 180)
    _run(tmp_path, host)
    host.close()
    m = measured_frames(tmp_path / "session")
    assert set(m) == {f.name for f in FRAMES}
    for name, r in m.items():
        assert r["frame"] is not None and set(r["references"])
        assert r["reference_weighted"]["lux"] is not None
        assert r["background_drift"]["lux"] is not None


def test_frame_session_refuses_a_frame_that_does_not_read_back(tmp_path):
    host = FakeHost()
    host.fb = (320, 180)
    with HostClient("127.0.0.1", host.port) as client:
        prepare_frames(tmp_path / "frames", 64, 36)
        host.fb = (80, 36)  # the window changed size after the frames were rendered
        reply = client.show_image(str(tmp_path / "frames" / "64x36" / "F1_morning_text.ppm"), "F1")
        assert not image_readback_matches(reply, frame_hash(render("F1_morning_text", 64, 36)))
    host.close()
    host = FakeHost()
    host.fb = (320, 180)
    host.minimized = True
    host.fullscreen_restores = False
    with pytest.raises(HostError):
        _run(tmp_path, host)
    host.close()

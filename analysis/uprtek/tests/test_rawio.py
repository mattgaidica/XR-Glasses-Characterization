import pytest

from uprtek.rawio import list_captures, read_capture, write_capture


def test_round_trip(tmp_path):
    wl = [380.0, 381.0, 382.0]
    reps = [[1.0, 2.0, 3.0], [1.5, 2.5, 3.5]]
    path = write_capture(tmp_path, "B255", wl, reps, {"rgb": [0, 0, 255], "eye": "both"})
    cap = read_capture(path)
    assert cap.label == "B255"
    assert cap.wavelength_nm == wl
    assert cap.repeats == reps
    assert cap.meta["rgb"] == [0, 0, 255]
    assert list_captures(tmp_path) == [path]


def test_rejects_ragged_repeats(tmp_path):
    with pytest.raises(ValueError):
        write_capture(tmp_path, "x", [380.0, 381.0], [[1.0]], {})

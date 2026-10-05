from uprtek.sequences import (
    WHITE_MAX_BRIGHTNESS,
    build,
    hosi_set,
    is_white,
    parse_duty_label,
    parse_levels,
)


def test_hosi_set_labels_match_hosi_file_names():
    labels = [s.label for s in hosi_set()]
    assert labels[:4] == ["R255", "G255", "B255", "W255"]
    assert labels.count("B255") == 1
    assert labels[4:10] == ["B192", "B128", "B064", "B032", "B016", "K"]
    assert labels[10:] == [f"B255_br{n}" for n in range(8, -1, -1)]
    assert len(labels) == len(set(labels))


def test_brightness_sweep_holds_rgb_and_runs_highest_first():
    steps = build("brightness_sweep", (0, 4, 8))
    assert [s.brightness for s in steps] == [8, 4, 0]
    assert all(s.rgb == (0, 0, 255) for s in steps)


def test_color_set_covers_every_channel_alike():
    steps = build("color_set", (0, 8))
    labels = [s.label for s in steps]
    assert len(labels) == len(set(labels))
    assert labels[:4] == ["R255", "G255", "B255", "W255"]
    assert {"B016", "B032", "B064", "B128", "B192", "R064", "G128", "W192"} <= set(labels)
    sweep = [s for s in steps if s.brightness is not None and s.duty is None]
    assert [s.label for s in sweep] == ["R255_br8", "G255_br8", "B255_br8",
                                        "R255_br0", "G255_br0", "B255_br0", "W255_br0"]
    by_label = {s.label: s for s in steps}
    assert by_label["W255_br0"].rgb == (255, 255, 255)
    assert by_label["G128"].rgb == (0, 128, 0)
    assert labels[labels.index("B016") + 1] == "K"


def test_color_set_duty_block_skips_base_and_caps_white():
    steps = build("color_set", (8,), duties=(98, 42, 30), base_duty=98)
    block = [s for s in steps if s.duty is not None]
    assert [s.label for s in block] == ["R255_d42", "G255_d42", "B255_d42", "W255_br7_d42",
                                        "R255_d30", "G255_d30", "B255_d30", "W255_br7_d30"]
    assert all(s.brightness == (WHITE_MAX_BRIGHTNESS if is_white(s.rgb) else 8) for s in block)
    assert parse_duty_label("W255_br7_d42") == 42 and parse_duty_label("B255_br8") is None


def test_color_set_duty_block_defaults_to_presets():
    steps = build("color_set", (8,))
    assert sorted({s.duty for s in steps if s.duty is not None}) == [30, 42]


def test_no_sequence_requests_white_above_its_limit():
    for name in ("hosi_set", "color_set", "primaries", "blue_ladder", "brightness_sweep", "repeatability"):
        for s in build(name):
            if is_white(s.rgb) and s.brightness is not None:
                assert s.brightness <= WHITE_MAX_BRIGHTNESS, (name, s.label)


def test_ladder_leaves_brightness_alone():
    assert all(s.brightness is None for s in build("blue_ladder"))


def test_parse_levels():
    assert parse_levels("0-3") == (0, 1, 2, 3)
    assert parse_levels("0,4,8") == (0, 4, 8)

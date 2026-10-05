"""Named measurement sequences. Labels match the HOSI ground-truth file names
(``R255``, ``B255_br3``, ``B016`` …) so sessions can be compared side by side."""

from __future__ import annotations

from dataclasses import dataclass

BLUE_LADDER = (16, 32, 64, 128, 192, 255)
# Red, green and white ladders start at 64: codes 16 and 32 of the blue ladder already sit near or
# below the meter's range, at multi-second exposures.
COLOR_LADDER = (64, 128, 192)
DEFAULT_BRIGHTNESS_LEVELS = tuple(range(0, 9))
# Full white at brightness 8 (duty 98) collapses the Luma Ultra's output and drops its USB control
# link until the host is restarted; R, G and B alone are fine at 8. White never runs above this.
WHITE_MAX_BRIGHTNESS = 7
# Duty cycles: the manufacturer's SDK presets (H, M, L), the only duty cycles the Luma Ultra
# supports. The highest is the base duty of every sweep.
DUTY_PRESETS = (98, 42, 30)
DUTY_BASE = max(DUTY_PRESETS)
DUTY_BLOCK_BRIGHTNESS = 8
CHANNELS = {
    "R": (1, 0, 0),
    "G": (0, 1, 0),
    "B": (0, 0, 1),
    "W": (1, 1, 1),
}


@dataclass(frozen=True)
class Step:
    label: str
    rgb: tuple[int, int, int]
    brightness: int | None = None  # None: the sweep's base brightness (or leave alone if none)
    duty: int | None = None  # None: the sweep's duty cycle


def _rgb(channel: str, code: int) -> tuple[int, int, int]:
    r, g, b = CHANNELS[channel]
    return (r * code, g * code, b * code)


def is_white(rgb: tuple[int, int, int]) -> bool:
    return rgb[0] == rgb[1] == rgb[2] > 0


def black() -> Step:
    return Step("K", (0, 0, 0))


def primaries() -> list[Step]:
    """R, G, B and W at full code, then black. Black is not first: its multi-second exposures leave
    the panel dark, and the capture after a long dark stretch is where the display has blanked."""
    return [Step(f"{c}255", _rgb(c, 255)) for c in "RGBW"] + [black()]


def blue_ladder(levels: tuple[int, ...] = BLUE_LADDER) -> list[Step]:
    return [Step(f"B{b:03d}", (0, 0, b)) for b in levels]


def dim_ladder_then_black(seen: set[str]) -> list[Step]:
    """Blue ladder from bright to dim, then black. Black is subtracted from every capture and the
    dark reading drifts upward after dark calibration, so it is taken right after the dimmest codes."""
    steps = [s for s in blue_ladder(tuple(sorted(BLUE_LADDER, reverse=True))) if s.label not in seen]
    return steps + [black()]


def descending(levels: tuple[int, ...]) -> tuple[int, ...]:
    """Highest level first, so a sweep from base brightness 8 starts without a brightness change and
    the level-8 anchor sits next to the levels nearest it."""
    return tuple(sorted(levels, reverse=True))


def brightness_sweep(levels: tuple[int, ...] = DEFAULT_BRIGHTNESS_LEVELS) -> list[Step]:
    return [Step(f"B255_br{n}", (0, 0, 255), brightness=n) for n in descending(levels)]


def repeatability() -> list[Step]:
    return [Step("B128", (0, 0, 128)), Step("B255", (0, 0, 255))]


def hosi_set(brightness_levels: tuple[int, ...] = DEFAULT_BRIGHTNESS_LEVELS) -> list[Step]:
    """HOSI ground-truth set: primaries, blue ladder (bright to dim), black, then the B255
    brightness sweep."""
    steps = [s for s in primaries() if s.label != "K"]
    steps += dim_ladder_then_black({s.label for s in steps})
    steps += brightness_sweep(brightness_levels)
    return steps


def duty_block(duties: tuple[int, ...] = DUTY_PRESETS, base_duty: int | None = None) -> list[Step]:
    """R, G and B at code 255 and brightness DUTY_BLOCK_BRIGHTNESS, and W at WHITE_MAX_BRIGHTNESS,
    at every duty except the base (already measured by the rest of the sequence), highest first."""
    steps: list[Step] = []
    for d in sorted(set(duties) - {base_duty}, reverse=True):
        steps += [Step(f"{c}255_d{d}", _rgb(c, 255), brightness=DUTY_BLOCK_BRIGHTNESS, duty=d) for c in "RGB"]
        steps.append(Step(f"W255_br{WHITE_MAX_BRIGHTNESS}_d{d}", _rgb("W", 255),
                          brightness=WHITE_MAX_BRIGHTNESS, duty=d))
    return steps


def color_set(brightness_levels: tuple[int, ...] = DEFAULT_BRIGHTNESS_LEVELS,
              duties: tuple[int, ...] = DUTY_PRESETS, base_duty: int | None = None) -> list[Step]:
    """Every channel characterized alike: R, G, B, W at full code; the blue ladder and black; code
    ladders for R, G and W; a brightness sweep of all four at code 255, grouped by level so each
    level is set once; then the duty block. White is left out of levels above WHITE_MAX_BRIGHTNESS.

    Labels follow the HOSI pattern (``G128``, ``W255_br3``); the blue labels match hosi_set. Duty
    block labels end in ``_d<duty>`` (``B255_d42``, ``W255_br7_d42``). ``base_duty`` defaults to the
    highest of ``duties``.
    """
    steps = [s for s in primaries() if s.label != "K"]
    steps += dim_ladder_then_black({s.label for s in steps})
    for c in "RGW":
        steps += [Step(f"{c}{code:03d}", _rgb(c, code)) for code in sorted(COLOR_LADDER, reverse=True)]
    for n in descending(brightness_levels):
        channels = "RGBW" if n <= WHITE_MAX_BRIGHTNESS else "RGB"
        steps += [Step(f"{c}255_br{n}", _rgb(c, 255), brightness=n) for c in channels]
    steps += duty_block(duties, max(duties) if base_duty is None else base_duty)
    return steps


SEQUENCES = {
    "hosi_set": hosi_set,
    "color_set": color_set,
    "primaries": lambda levels=None: primaries(),
    "blue_ladder": lambda levels=None: blue_ladder(),
    "brightness_sweep": brightness_sweep,
    "repeatability": lambda levels=None: repeatability(),
}


# Sequences whose steps set their own duty cycle.
DUTY_SEQUENCES = frozenset({"color_set"})


def build(name: str, brightness_levels: tuple[int, ...] | None = None,
          duties: tuple[int, ...] | None = None, base_duty: int | None = None) -> list[Step]:
    if name not in SEQUENCES:
        raise ValueError(f"unknown sequence {name!r}; choose from {sorted(SEQUENCES)}")
    factory = SEQUENCES[name]
    kwargs: dict = {}
    if name in DUTY_SEQUENCES:
        if duties is not None:
            kwargs["duties"] = duties
        kwargs["base_duty"] = base_duty
    if brightness_levels is None:
        return factory(**kwargs)
    return factory(brightness_levels, **kwargs)


def parse_duty_label(label: str) -> int | None:
    """Duty of a duty-block label (``B255_d42`` -> 42), or None."""
    head, sep, tail = label.rpartition("_d")
    return int(tail) if sep and head and tail.isdigit() else None


def parse_levels(text: str) -> tuple[int, ...]:
    """``"0-8"`` or ``"0,2,4,8"``."""
    text = text.strip()
    if "-" in text and "," not in text:
        lo, hi = (int(p) for p in text.split("-", 1))
        if hi < lo:
            raise ValueError(f"bad range {text!r}")
        return tuple(range(lo, hi + 1))
    return tuple(int(p) for p in text.split(",") if p.strip())

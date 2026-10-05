"""Open the UPRtek meter, print its identity, take one capture, and optionally save it.

Run under 32-bit Python::

    py -3.12-32 -m uprtek.probe --output data/uprtek/probe
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

from uprtek.mkusb import DEFAULT_MAX_EXPOSURE_MS, Meter, MkUsbError
from uprtek.rawio import write_capture
from uprtek.sweep import _parse_exposure, _parse_max_exposure, now_iso


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--exposure", default="auto", help="'auto' or a fixed exposure in ms")
    p.add_argument("--max-exposure-ms", type=_parse_max_exposure, default=DEFAULT_MAX_EXPOSURE_MS,
                   help=f"auto-exposure ceiling (default {DEFAULT_MAX_EXPOSURE_MS}; restored afterwards)")
    p.add_argument("--dark", action="store_true", help="run mk_Msr_Dark first (cap the sensor)")
    p.add_argument("--output", type=Path, help="session folder to write raw/probe.csv into")
    p.add_argument("--label", default="probe")
    p.add_argument("--meter-sn")
    p.add_argument("--mkusb-dir")
    args = p.parse_args(argv)
    auto, ms = _parse_exposure(args.exposure)

    try:
        meter = Meter(args.mkusb_dir, optical_sn=args.meter_sn)
    except MkUsbError as exc:
        print(f"meter: {exc}", file=sys.stderr)
        return 2
    with meter:
        for k, v in asdict(meter.info).items():
            print(f"{k:<20} {v}")
        meter.set_max_exposure_ms(args.max_exposure_ms)
        try:
            if args.dark:
                input("Cap the sensor, then press Enter… ")
                meter.dark()
                print("dark calibration done")
            t0 = now_iso()
            cap = meter.capture(auto_exposure=auto, exposure_ms=ms)
        finally:
            meter.set_max_exposure_ms(DEFAULT_MAX_EXPOSURE_MS)
        spectrum = cap.pop("spectrum")
        print(f"light_strength       {cap['light_strength']} ({cap['light_strength_name']})")
        print(f"temperature_c        {cap['temperature_c']}")
        for k, v in cap["instrument"].items():
            print(f"{k:<20} {v}")
        peak_i = max(range(len(spectrum)), key=spectrum.__getitem__)
        print(f"spectrum             {len(spectrum)} pts, max {spectrum[peak_i]:.6g} at "
              f"{meter.info.wavelength_start_nm + peak_i} nm, sum {sum(spectrum):.6g}")
        if args.output:
            wl = [float(meter.info.wavelength_start_nm + i) for i in range(len(spectrum))]
            meta = {
                "sequence": "probe",
                "rgb": None,
                "max_exposure_ms": args.max_exposure_ms if auto else None,
                "repeats": [{"rep": 0, "t_start": t0, **cap}],
                "meter": asdict(meter.info),
                "flags": [],
            }
            path = write_capture(args.output, args.label, wl, [spectrum], meta)
            print(f"saved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

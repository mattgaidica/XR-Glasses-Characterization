"""ctypes wrapper for the UPRtek ``mkusb.dll`` (uSpectrum PC library, 32-bit, __stdcall).

Must run under 32-bit Python on Windows. Signatures follow ``mkusb.h`` / ``mk.h`` shipped in
``C:\\Program Files (x86)\\uSpectrum\\Library\\Example\\VC\\dll_test_vc2008``.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import struct
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LIB_DIR = Path(
    r"C:\Program Files (x86)\uSpectrum\Library\Example\VC\dll_test_vc2008\Debug"
)
LIB_DIR_ENV = "UPRTEK_MKUSB_DIR"

# mk.h data ids (subset used by Chronolume).
DATA_IDS: dict[str, int] = {
    "cie1931_x": 0x0001,
    "cie1931_y": 0x0002,
    "cie_X": 0x0003,
    "cie_Y": 0x0004,
    "cie_Z": 0x0005,
    "cie1976_u_prime": 0x0011,
    "cie1976_v_prime": 0x0012,
    "lux": 0x0101,
    "cct_K": 0x0103,
    "lux_scotopic": 0x0104,
    "lux_s_p_ratio": 0x0105,
    "purity": 0x0201,
    "lambda_peak_nm": 0x0202,
    "lambda_dominant_nm": 0x0203,
    "lambda_peak_value": 0x0204,
    "ppf": 0x0301,
    "exposure_time_raw": 0x0401,
}

INFO_IDS: dict[str, int] = {
    "model": 0x1001,
    "hw_version": 0x1002,
    "fw_version": 0x1003,
    "wavelength_start_nm": 0x1004,
    "wavelength_end_nm": 0x1005,
}

# The vendor guide labels 2 "over exposure"; MK350X FW 1.1.1.B4 also returns it for weak signal.
LIGHT_STRENGTH = {0: "low", 1: "normal", 2: "out_of_range"}

# mk_Msr_Capture and mk_Msr_SetMaxExpTime take microseconds although the guide says ms.
# The auto-exposure ceiling persists on the meter between sessions; 500 ms is the factory value
# observed on MK350X FW 1.1.1.B4.
MANUAL_EXPOSURE_MAX_MS = 65
DEFAULT_MAX_EXPOSURE_MS = 500


class MkUsbError(RuntimeError):
    pass


@dataclass
class MeterInfo:
    name: str
    optical_sn: str
    model: str
    hw_version: str
    fw_version: str
    wavelength_start_nm: int
    wavelength_end_nm: int
    library_version: str
    library_dir: str


def resolve_lib_dir(lib_dir: str | Path | None = None) -> Path:
    if lib_dir is not None:
        return Path(lib_dir)
    env = os.environ.get(LIB_DIR_ENV)
    return Path(env) if env else DEFAULT_LIB_DIR


class MkUsb:
    """Thin, call-for-call binding. Every DLL call runs with the library folder as CWD
    because the DLL reads ``si_info/CIEO.CFG`` relative to the working directory."""

    def __init__(self, lib_dir: str | Path | None = None) -> None:
        if sys.platform != "win32":
            raise MkUsbError("mkusb.dll is Windows-only")
        if struct.calcsize("P") != 4:
            raise MkUsbError(
                "mkusb.dll is 32-bit; run capture under 32-bit Python "
                "(e.g. `py -3.12-32 -m uprtek.sweep ...`)"
            )
        self.lib_dir = resolve_lib_dir(lib_dir).resolve()
        dll_path = self.lib_dir / "mkusb.dll"
        if not dll_path.exists():
            raise MkUsbError(f"mkusb.dll not found in {self.lib_dir} (set {LIB_DIR_ENV})")
        self._dll_dir_handle = os.add_dll_directory(str(self.lib_dir))
        with self._in_lib_dir():
            self._dll = ctypes.WinDLL(str(dll_path))
        self._bind()
        self._initialized = False

    @contextlib.contextmanager
    def _in_lib_dir(self) -> Iterator[None]:
        prev = os.getcwd()
        os.chdir(self.lib_dir)
        try:
            yield
        finally:
            os.chdir(prev)

    def _bind(self) -> None:
        d = self._dll
        b, i, u, us, f, c = (
            ctypes.c_bool,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_ushort,
            ctypes.c_float,
            ctypes.c_char_p,
        )
        pf = ctypes.POINTER(ctypes.c_float)

        def sig(name: str, restype, *argtypes) -> None:
            fn = getattr(d, name)
            fn.restype = restype
            fn.argtypes = list(argtypes)

        sig("mk_version", u)
        sig("mk_Init", b, i, i)
        sig("mk_Close", None)
        sig("mk_SpDevScan", b)
        sig("mk_GetDeviceCnt", i)
        sig("mk_FindFirst", b, c)
        sig("mk_FindNext", b, c)
        sig("mk_FindClose", b)
        sig("mk_OpenSpDev", i, c)
        sig("mk_OpenSpDev_OptSn", i, c)
        sig("mk_CloseSpDev", b, i)
        sig("mk_GetOptSn", b, i, c)
        sig("mk_Info_Get", b, i, i, c)
        sig("mk_Msr_Capture", b, i, us, us)
        sig("mk_Msr_Dark", b, i)
        sig("mk_Msr_SetMaxExpTime", b, i, u)
        sig("mk_GetData", b, i, i, pf)
        sig("mk_GetSpectrum", b, i, i, i, pf)
        sig("mk_GetLightStrnegth", i, i)
        sig("mk_Peri_GetTemp", b, i, pf)

    def _call(self, name: str, *args):
        with self._in_lib_dir():
            return getattr(self._dll, name)(*args)

    # Library lifecycle -------------------------------------------------------------------

    def version(self) -> str:
        v = int(self._call("mk_version"))
        return f"0x{v:X}"

    def init(self) -> None:
        if not self._call("mk_Init", 0, 300):
            raise MkUsbError("mk_Init failed")
        self._call("mk_SpDevScan")
        self._initialized = True

    def close(self) -> None:
        if self._initialized:
            self._call("mk_Close")
            self._initialized = False

    def device_names(self) -> list[str]:
        names: list[str] = []
        buf = ctypes.create_string_buffer(64)
        ok = self._call("mk_FindFirst", buf)
        while ok:
            names.append(buf.value.decode("ascii", "replace"))
            buf = ctypes.create_string_buffer(64)
            ok = self._call("mk_FindNext", buf)
        self._call("mk_FindClose")
        return names

    # Device ------------------------------------------------------------------------------

    def open(self, name: str) -> int:
        dev = int(self._call("mk_OpenSpDev", name.encode("ascii")))
        # mkusb returns -1 on failure; 0 is a valid id despite the guide saying ">0".
        if dev < 0:
            raise MkUsbError(f"mk_OpenSpDev({name!r}) failed")
        return dev

    def close_device(self, dev: int) -> None:
        self._call("mk_CloseSpDev", dev)

    def optical_sn(self, dev: int) -> str:
        buf = ctypes.create_string_buffer(32)
        if not self._call("mk_GetOptSn", dev, buf):
            return ""
        return buf.value.decode("ascii", "replace")

    def info(self, dev: int, key: str) -> str:
        buf = ctypes.create_string_buffer(128)
        if not self._call("mk_Info_Get", dev, INFO_IDS[key], buf):
            return ""
        return buf.value.decode("ascii", "replace").strip()

    def dark(self, dev: int) -> None:
        if not self._call("mk_Msr_Dark", dev):
            raise MkUsbError("mk_Msr_Dark failed")

    def capture(self, dev: int, *, auto_exposure: bool, exposure_ms: int = 100) -> None:
        if auto_exposure:
            exposure_us = 100
        else:
            if not 1 <= exposure_ms <= MANUAL_EXPOSURE_MAX_MS:
                raise ValueError(f"manual exposure must be 1..{MANUAL_EXPOSURE_MAX_MS} ms")
            exposure_us = exposure_ms * 1000
        if not self._call("mk_Msr_Capture", dev, 1 if auto_exposure else 0, exposure_us):
            raise MkUsbError("mk_Msr_Capture failed")

    def set_max_exposure_ms(self, dev: int, ms: int) -> None:
        if not 1 <= ms <= 60000:
            raise ValueError("max exposure must be 1..60000 ms")
        if not self._call("mk_Msr_SetMaxExpTime", dev, ms * 1000):
            raise MkUsbError("mk_Msr_SetMaxExpTime failed")

    def data(self, dev: int, key: str) -> float | None:
        v = ctypes.c_float(0.0)
        if not self._call("mk_GetData", dev, DATA_IDS[key], ctypes.byref(v)):
            return None
        return float(v.value)

    def all_data(self, dev: int) -> dict[str, float | None]:
        return {k: self.data(dev, k) for k in DATA_IDS}

    def spectrum(self, dev: int, start_nm: int, stop_nm: int) -> list[float]:
        n = stop_nm - start_nm + 1
        arr = (ctypes.c_float * n)()
        if not self._call("mk_GetSpectrum", dev, start_nm, stop_nm, arr):
            raise MkUsbError("mk_GetSpectrum failed")
        return [float(x) for x in arr]

    def light_strength(self, dev: int) -> int:
        return int(self._call("mk_GetLightStrnegth", dev))

    def temperature_c(self, dev: int) -> float | None:
        v = ctypes.c_float(0.0)
        try:
            ok = self._call("mk_Peri_GetTemp", dev, ctypes.byref(v))
        except OSError:
            return None
        return float(v.value) if ok else None


class Meter:
    """Opened MK350-family meter: open/close, dark, and capture with full readback."""

    def __init__(self, lib_dir: str | Path | None = None, optical_sn: str | None = None) -> None:
        self.lib = MkUsb(lib_dir)
        self.lib.init()
        names = self.lib.device_names()
        if not names:
            self.lib.close()
            raise MkUsbError("no UPRtek meter found (close uSpectrum if it is running)")
        self.dev = -1
        chosen = ""
        for name in names:
            dev = self.lib.open(name)
            sn = self.lib.optical_sn(dev)
            if optical_sn is None or sn == optical_sn:
                self.dev, chosen = dev, name
                break
            self.lib.close_device(dev)
        if self.dev < 0:
            self.lib.close()
            raise MkUsbError(f"no meter with optical SN {optical_sn!r} among {names}")
        self.info = MeterInfo(
            name=chosen,
            optical_sn=self.lib.optical_sn(self.dev),
            model=self.lib.info(self.dev, "model"),
            hw_version=self.lib.info(self.dev, "hw_version"),
            fw_version=self.lib.info(self.dev, "fw_version"),
            wavelength_start_nm=int(self.lib.info(self.dev, "wavelength_start_nm") or 380),
            wavelength_end_nm=int(self.lib.info(self.dev, "wavelength_end_nm") or 780),
            library_version=self.lib.version(),
            library_dir=str(self.lib.lib_dir),
        )

    def close(self) -> None:
        if self.dev >= 0:
            self.lib.close_device(self.dev)
            self.dev = -1
        self.lib.close()

    def __enter__(self) -> Meter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def dark(self) -> None:
        self.lib.dark(self.dev)

    def set_max_exposure_ms(self, ms: int) -> None:
        self.lib.set_max_exposure_ms(self.dev, ms)

    def capture(self, *, auto_exposure: bool, exposure_ms: int = 100) -> dict:
        self.lib.capture(self.dev, auto_exposure=auto_exposure, exposure_ms=exposure_ms)
        strength = self.lib.light_strength(self.dev)
        return {
            "auto_exposure": auto_exposure,
            "exposure_ms_requested": None if auto_exposure else exposure_ms,
            "light_strength": strength,
            "light_strength_name": LIGHT_STRENGTH.get(strength, "unknown"),
            "temperature_c": self.lib.temperature_c(self.dev),
            "instrument": self.lib.all_data(self.dev),
            "spectrum": self.lib.spectrum(
                self.dev, self.info.wavelength_start_nm, self.info.wavelength_end_nm
            ),
        }

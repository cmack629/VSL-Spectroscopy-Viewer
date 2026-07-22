"""
Avantes AvaSpec spectrometer — native driver (SensLine / NIRLine).

A self-contained ctypes wrapper over the Avantes AvaSpec library (`libavs`),
modelled on drivers/hr4000.py so it is used the same way for data collection:
a thread-safe class with a `read()` that returns one full Spectrum snapshot,
plus integration time, averaging, on-device smoothing and dynamic-dark control.

Runs NATIVELY on macOS, Linux and Windows — the Avantes 9.14 SDK ships a native
library for each (incl. an arm64 macOS `libavs.dylib`), so no Docker/VM is
needed. The struct layouts and call signatures below are taken verbatim from the
vendor's own ctypes wrapper (`avaspec.py`) and `avaspec.h` in the 9.14 SDK; this
module just drops avaspec.py's PyQt5/`globals` dependencies and adds flexible,
non-fatal library discovery.

Library discovery order (first that loads wins):
  1. $AVANTES_LIB                       (explicit path to the lib file)
  2. drivers/libavs/<platform lib>      (vendored — macOS dylib already here)
  3. common system locations            (/usr/local/lib/libavs.0.dylib, …)
macOS/Linux additionally need libusb (`brew install libusb` / `apt install libusb-1.0-0`).

Hardware chain:  PC <--USB/Ethernet--> AvaSpec (AS5216 / Mini / AS7010 / AS7007)
  * SensLine = UV/VIS CCD line (e.g. AvaSpec-ULS2048CL).
  * NIRLine  = InGaAs NIR line (e.g. AvaSpec-NIR256/512-1.7/2.5).
  Both use the same AvaSpec library; the driver auto-detects pixels/range.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import (CDLL, POINTER, byref, c_bool, c_char, c_double, c_int,
                    c_uint, c_uint8, c_uint16, c_uint32, c_float)
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# AvaSpec constants (from avaspec.py / avaspec.h, SDK 9.14.0.0)
# ---------------------------------------------------------------------------

AVS_SERIAL_LEN = 10
USER_ID_LEN = 64
VERSION_LEN = 16
INVALID_AVS_HANDLE_VALUE = 1000
MAX_NR_PIXELS = 4096

# Detector sensor-type codes → whether the line is NIR (InGaAs) vs UV/VIS (CCD).
_SENS_NAMES = {
    4: "Hamamatsu S9201 (CCD)", 5: "Toshiba TCD1304 (CCD)",
    17: "Hamamatsu S11639 InGaAs (NIR)", 18: "Sony ILX511 (CCD)",
    22: "Hamamatsu S11639 (CCD)", 24: "Hamamatsu G9208-512 InGaAs (NIR)",
    26: "Hamamatsu S13496 (CCD)", 30: "Hamamatsu S11155 (CCD)",
}


@dataclass(frozen=True)
class SpectrometerLine:
    name: str
    detector: str
    typical_range: str
    note: str = ""


SENSLINE = SpectrometerLine(
    name="SensLine", detector="CCD / back-thinned CCD (UV-VIS)",
    typical_range="~200–1100 nm",
    note="High-sensitivity UV/VIS line (e.g. AvaSpec-ULS2048CL-EVO).")
NIRLINE = SpectrometerLine(
    name="NIRLine", detector="InGaAs array (NIR)",
    typical_range="~900–2500 nm",
    note="NIR line (e.g. AvaSpec-NIR256/512-1.7/2.5), often TE-cooled.")


# ---------------------------------------------------------------------------
# Library discovery (flexible, non-fatal)
# ---------------------------------------------------------------------------

def _candidate_lib_paths() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    vend = os.path.join(here, "libavs")
    paths: list[str] = []
    env = os.environ.get("AVANTES_LIB")
    if env:
        paths.append(env)
    if sys.platform == "darwin":
        paths += [os.path.join(vend, "libavs.dylib"),
                  os.path.join(vend, "libavs.9.14.0.0.dylib"),
                  "/usr/local/lib/libavs.0.dylib",
                  "/usr/local/lib/libavs.dylib",
                  "/opt/homebrew/lib/libavs.dylib"]
    elif sys.platform.startswith("linux"):
        # amd64 and arm64 builds are both vendored under their own filename
        # (a single repo checkout is shared across coworkers on either kind
        # of Linux box) — whichever doesn't match this machine's
        # architecture just fails to load below and we fall through to the
        # next candidate, same as the multiple Windows DLL names above.
        paths += [os.path.join(vend, "libavs.so.amd64"),
                  os.path.join(vend, "libavs.so.arm64"),
                  os.path.join(vend, "libavs.so"),
                  os.path.join(vend, "libavs.so.0"),
                  "/usr/local/lib/libavs.so.0", "/usr/local/lib/libavs.so",
                  "/usr/lib/libavs.so.0"]
    else:  # win32
        paths += [os.path.join(vend, "avaspecx64.dll"),
                  os.path.join(vend, "avaspec.dll"),
                  os.path.join(vend, "avaspec_production_x64.dll"),
                  os.path.join(vend, "avaspec_production.dll"),
                  "avaspecx64.dll", "avaspec.dll"]
    return paths


def _load_library():
    """Return (lib, path, error). lib is None and error is set if none loaded."""
    last_err = ""
    for p in _candidate_lib_paths():
        if not (p and os.path.exists(p)) and not p.endswith(".dll"):
            continue
        try:
            loader = ctypes.WinDLL if sys.platform.startswith("win") else CDLL
            return loader(p), p, ""
        except Exception as exc:               # e.g. missing libusb
            last_err = f"{p}: {exc}"
    hint = ("Avantes library (libavs) not found/loadable.\n"
            "  • macOS: brew install libusb   (the SDK dylib is vendored in "
            "drivers/libavs/)\n"
            "  • Linux: install libavs (.deb/.so from the SDK) + libusb-1.0-0\n"
            "  • or set $AVANTES_LIB to the library file.\n")
    return None, "", (last_err or "no candidate found") + "\n" + hint


_LIB, _LIB_PATH, _LIB_ERR = _load_library()
_AVS_OK = _LIB is not None


# ---------------------------------------------------------------------------
# Structures (verbatim layout from avaspec.py, _pack_ = 1)
# ---------------------------------------------------------------------------

class AvsIdentityType(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("SerialNumber", c_char * AVS_SERIAL_LEN),
                ("UserFriendlyName", c_char * USER_ID_LEN),
                ("Status", c_char)]


class MeasConfigType(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("m_StartPixel", c_uint16),
                ("m_StopPixel", c_uint16),
                ("m_IntegrationTime", c_float),          # milliseconds
                ("m_IntegrationDelay", c_uint32),
                ("m_NrAverages", c_uint32),
                ("m_CorDynDark_m_Enable", c_uint8),
                ("m_CorDynDark_m_ForgetPercentage", c_uint8),
                ("m_Smoothing_m_SmoothPix", c_uint16),
                ("m_Smoothing_m_SmoothModel", c_uint8),
                ("m_SaturationDetection", c_uint8),
                ("m_Trigger_m_Mode", c_uint8),
                ("m_Trigger_m_Source", c_uint8),
                ("m_Trigger_m_SourceType", c_uint8),
                ("m_Control_m_StrobeControl", c_uint16),
                ("m_Control_m_LaserDelay", c_uint32),
                ("m_Control_m_LaserWidth", c_uint32),
                ("m_Control_m_LaserWaveLength", c_float),
                ("m_Control_m_StoreToRam", c_uint16)]


def _bind():
    """Set argtypes/restypes on the needed AVS_* functions."""
    L = _LIB
    L.AVS_Init.argtypes = [c_int]; L.AVS_Init.restype = c_int
    L.AVS_Done.restype = c_int
    L.AVS_UpdateUSBDevices.restype = c_int
    L.AVS_GetList.argtypes = [c_uint, POINTER(c_uint), POINTER(AvsIdentityType)]
    L.AVS_GetList.restype = c_int
    L.AVS_Activate.argtypes = [POINTER(AvsIdentityType)]; L.AVS_Activate.restype = c_int
    L.AVS_Deactivate.argtypes = [c_int]; L.AVS_Deactivate.restype = c_bool
    L.AVS_UseHighResAdc.argtypes = [c_int, c_bool]; L.AVS_UseHighResAdc.restype = c_int
    L.AVS_GetNumPixels.argtypes = [c_int, POINTER(c_uint16)]; L.AVS_GetNumPixels.restype = c_int
    L.AVS_GetLambda.argtypes = [c_int, POINTER(c_double)]; L.AVS_GetLambda.restype = c_int
    L.AVS_PrepareMeasure.argtypes = [c_int, POINTER(MeasConfigType)]; L.AVS_PrepareMeasure.restype = c_int
    L.AVS_Measure.argtypes = [c_int, c_int, c_uint16]; L.AVS_Measure.restype = c_int
    L.AVS_PollScan.argtypes = [c_int]; L.AVS_PollScan.restype = c_int
    L.AVS_StopMeasure.argtypes = [c_int]; L.AVS_StopMeasure.restype = c_int
    L.AVS_GetScopeData.argtypes = [c_int, POINTER(c_uint32), POINTER(c_double)]
    L.AVS_GetScopeData.restype = c_int
    L.AVS_GetVersionInfo.argtypes = [c_int, c_char * VERSION_LEN,
                                     c_char * VERSION_LEN, c_char * VERSION_LEN]
    L.AVS_GetVersionInfo.restype = c_int


if _AVS_OK:
    try:
        _bind()
    except Exception as exc:           # symbol mismatch on an unexpected lib version
        _AVS_OK = False
        _LIB_ERR = f"AvaSpec library loaded but binding failed: {exc}"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Spectrum:
    wavelengths: list
    intensities: list
    integration_time_ms: float
    n_averaged: int
    smooth_pix: int
    max_counts: float
    timestamp: float
    peak_index: int = 0
    peak_wavelength: float = 0.0
    peak_intensity: float = 0.0
    total_counts: float = 0.0
    saturated: bool = False

    @property
    def pixels(self) -> int:
        return len(self.intensities)


class AvantesError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

# AVS_Init port modes
PORT_USB = 0
PORT_ETHERNET = 256
PORT_ALL = -1

# Process-wide guard: AVS_Init/AVS_Done are global to the library.
_init_lock = threading.Lock()
_init_count = 0


class Avantes:
    """
    Native interface to an Avantes AvaSpec spectrometer (SensLine or NIRLine).

    Usage:
        with Avantes() as spec:
            spec.integration_time_ms = 20
            s = spec.read()
            print(s.peak_wavelength, s.peak_intensity)
    """

    def __init__(self, serial: str = "auto", port_mode: int = PORT_USB,
                 high_res_adc: bool = True):
        if not _AVS_OK:
            raise AvantesError(_LIB_ERR)

        global _init_count
        self._lock = threading.RLock()
        self._handle = None
        self.high_res_adc = high_res_adc

        with _init_lock:
            if _init_count == 0:
                if _LIB.AVS_Init(port_mode) < 0:
                    raise AvantesError("AVS_Init failed (check connection/driver)")
            _init_count += 1
        self._inited = True

        try:
            ndev = _LIB.AVS_UpdateUSBDevices()
            if ndev <= 0:
                raise AvantesError("No Avantes spectrometer found on the bus")
            devs = self._get_list(ndev)
            ident = self._pick(devs, serial)
            self._serial = ident.SerialNumber.decode(errors="replace").strip("\x00")
            self._name = ident.UserFriendlyName.decode(errors="replace").strip("\x00")

            handle = _LIB.AVS_Activate(byref(ident))
            if handle < 0 or handle == INVALID_AVS_HANDLE_VALUE:
                raise AvantesError(f"AVS_Activate failed (handle={handle})")
            self._handle = handle

            _LIB.AVS_UseHighResAdc(handle, c_bool(high_res_adc))
            self.max_counts = 65535.0 if high_res_adc else 16383.0

            npix = c_uint16(0)
            _LIB.AVS_GetNumPixels(handle, byref(npix))
            self.pixels = int(npix.value)
            lam = (c_double * self.pixels)()
            _LIB.AVS_GetLambda(handle, lam)
            self._wavelengths = [float(x) for x in lam]
            self.fpga, self.fw, self.libver = self._version(handle)
        except Exception:
            self.close()
            raise

        # Acquisition settings.
        self._integration_ms = 10.0
        self.nr_averages = 1
        self.smooth_pix = 0
        self.dark_correction = False

        # Identify the product line from the wavelength coverage.
        self.line = NIRLINE if self._wavelengths[-1] > 1050.0 else SENSLINE

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_list(ndev: int) -> list[AvsIdentityType]:
        arr = (AvsIdentityType * ndev)()
        req = c_uint(0)
        _LIB.AVS_GetList(c_uint(ctypes.sizeof(arr)), byref(req), arr)
        return list(arr)

    @staticmethod
    def _pick(devs, serial):
        if serial == "auto":
            return devs[0]
        for d in devs:
            if d.SerialNumber.decode(errors="replace").strip("\x00") == serial:
                return d
        raise AvantesError(f"Spectrometer serial '{serial}' not found")

    @staticmethod
    def _version(handle):
        a = (c_char * VERSION_LEN)(); b = (c_char * VERSION_LEN)(); c = (c_char * VERSION_LEN)()
        try:
            _LIB.AVS_GetVersionInfo(handle, a, b, c)
            dec = lambda x: bytes(x).split(b"\x00")[0].decode(errors="replace")
            return dec(a), dec(b), dec(c)
        except Exception:
            return "", "", ""

    @staticmethod
    def list_devices() -> list[str]:
        if not _AVS_OK:
            return []
        out = []
        try:
            with _init_lock:
                _LIB.AVS_Init(PORT_USB)
                n = _LIB.AVS_UpdateUSBDevices()
                if n > 0:
                    for d in Avantes._get_list(n):
                        out.append(d.SerialNumber.decode(errors="replace").strip("\x00"))
                _LIB.AVS_Done()
        except Exception:
            pass
        return out

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def name(self) -> str:
        return self._name

    @property
    def wavelengths(self) -> list:
        return list(self._wavelengths)

    @property
    def integration_time_ms(self) -> float:
        return self._integration_ms

    @integration_time_ms.setter
    def integration_time_ms(self, ms: float) -> None:
        self.set_integration_time(ms)

    def set_integration_time(self, ms: float) -> None:
        self._integration_ms = max(0.002, min(600_000.0, float(ms)))

    def set_averaging(self, n: int) -> None:
        self.nr_averages = max(1, int(n))

    def set_smoothing(self, pixels: int) -> None:
        self.smooth_pix = max(0, int(pixels))

    def set_dark_correction(self, enable: bool) -> None:
        self.dark_correction = bool(enable)

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def _meas_config(self) -> MeasConfigType:
        mc = MeasConfigType()
        mc.m_StartPixel = 0
        mc.m_StopPixel = self.pixels - 1
        mc.m_IntegrationTime = c_float(self._integration_ms)
        mc.m_IntegrationDelay = 0
        mc.m_NrAverages = self.nr_averages
        mc.m_CorDynDark_m_Enable = 1 if self.dark_correction else 0
        mc.m_CorDynDark_m_ForgetPercentage = 100
        mc.m_Smoothing_m_SmoothPix = self.smooth_pix
        mc.m_Smoothing_m_SmoothModel = 0
        mc.m_SaturationDetection = 0
        mc.m_Trigger_m_Mode = 0
        mc.m_Trigger_m_Source = 0
        mc.m_Trigger_m_SourceType = 0
        mc.m_Control_m_StrobeControl = 0
        mc.m_Control_m_LaserDelay = 0
        mc.m_Control_m_LaserWidth = 0
        mc.m_Control_m_LaserWaveLength = c_float(0.0)
        mc.m_Control_m_StoreToRam = 0
        return mc

    def read(self) -> Spectrum:
        """Acquire one spectrum (blocking) and return a Spectrum snapshot."""
        if self._handle is None:
            raise AvantesError("Not connected")
        with self._lock:
            mc = self._meas_config()
            rc = _LIB.AVS_PrepareMeasure(self._handle, byref(mc))
            if rc != 0:
                raise AvantesError(f"AVS_PrepareMeasure failed (rc={rc})")
            rc = _LIB.AVS_Measure(self._handle, 0, 1)
            if rc != 0:
                raise AvantesError(f"AVS_Measure failed (rc={rc})")

            timeout = (self._integration_ms * self.nr_averages) / 1000.0 + 2.0
            t0 = time.monotonic()
            while _LIB.AVS_PollScan(self._handle) != 1:
                if time.monotonic() - t0 > timeout:
                    raise TimeoutError("Timed out waiting for scan data")
                time.sleep(0.001)

            tl = c_uint32(0)
            data = (c_double * self.pixels)()
            rc = _LIB.AVS_GetScopeData(self._handle, byref(tl), data)
            if rc != 0:
                raise AvantesError(f"AVS_GetScopeData failed (rc={rc})")
            y = [float(v) for v in data]

        peak_i = max(range(len(y)), key=y.__getitem__)
        return Spectrum(
            wavelengths=list(self._wavelengths), intensities=y,
            integration_time_ms=self._integration_ms, n_averaged=self.nr_averages,
            smooth_pix=self.smooth_pix, max_counts=self.max_counts,
            timestamp=time.time(), peak_index=peak_i,
            peak_wavelength=self._wavelengths[peak_i], peak_intensity=y[peak_i],
            total_counts=float(sum(y)),
            saturated=max(y) >= 0.99 * self.max_counts,
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        global _init_count
        try:
            if self._handle is not None:
                try:
                    _LIB.AVS_StopMeasure(self._handle)
                except Exception:
                    pass
                _LIB.AVS_Deactivate(self._handle)
                self._handle = None
        finally:
            if getattr(self, "_inited", False):
                with _init_lock:
                    _init_count -= 1
                    if _init_count <= 0:
                        _init_count = 0
                        try:
                            _LIB.AVS_Done()
                        except Exception:
                            pass
                self._inited = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Library:", _LIB_PATH or "(not loaded)")
    if not _AVS_OK:
        print(_LIB_ERR)
        sys.exit(1)
    print("Devices:", Avantes.list_devices())
    with Avantes() as spec:
        print(f"{spec.name or spec.line.name}  S/N {spec.serial}  {spec.pixels} px  "
              f"{spec.wavelengths[0]:.1f}-{spec.wavelengths[-1]:.1f} nm  "
              f"line={spec.line.name}  FW {spec.fw}")
        spec.integration_time_ms = 20
        for _ in range(3):
            s = spec.read()
            print(f"  peak {s.peak_intensity:8.0f} @ {s.peak_wavelength:7.2f} nm  "
                  f"sat={s.saturated}")
            time.sleep(0.2)

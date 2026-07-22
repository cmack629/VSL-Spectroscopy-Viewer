"""
Ocean Optics HR4000CG-UV-NIR spectrometer — starter driver.

Acquires intensity-vs-wavelength spectra from an Ocean Optics HR4000CG-UV-NIR
(3648-pixel Toshiba TCD1304AP linear CCD, USB) using the python-seabreeze
library. Mirrors the PM400 power-meter driver (drivers/pm400.py) so it can be
used the same way for data collection — a thread-safe class with a `read()` that
returns one full snapshot, plus host-side averaging / boxcar smoothing / dark
correction.

About this specific model (the "CG-UV-NIR" suffix is OPTICS, not electronics —
nothing below hardcodes it; the values are read from the unit's calibration):
  * "CG" = composite grating → a single broadband sweep, ~200–1100 nm (UV-NIR).
  * Resolution is coarse and varies across the range (~0.5–1.5 nm FWHM typ.),
    unlike a narrow-band single-grating HR4000 — a few-pixel boxcar is harmless.
  * Broadband composite-grating units can show 2nd-order artifacts (a strong NIR
    line ghosting into the UV/visible). That is optical; use an order-sorting
    filter if it matters. The detector, ADC and USB protocol are identical to any
    HR4000, so this driver/UI is unchanged from the base model.

Hardware chain:  PC <--USB--> HR4000CG-UV-NIR  (Ocean Optics VID 0x2457, PID 0x1012)

Requirements:
    pip install seabreeze            # pulls in numpy
    python -m seabreeze_os_setup     # (Linux: udev rules; not needed on macOS)
seabreeze's pure-python pyusb backend works on macOS/Linux/Windows via libusb
(`brew install libusb` if libusb is missing).

Quick use:
    with HR4000() as spec:
        spec.integration_time_us = 10000     # 10 ms
        s = spec.read()
        print(s.peak_wavelength, s.peak_intensity)

SeaBreeze reference: github.com/ap--/python-seabreeze
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# python-seabreeze (and its numpy dependency) are optional at import time so the
# web app can still start and show an install hint when they are missing — the
# same pattern controller.py / pm400.py use for their optional backends.
try:
    import numpy as np
    import seabreeze
    # seabreeze defaults to its compiled cseabreeze extension, which needs a
    # C++ toolchain + libusb headers to build from source and has no
    # prebuilt Linux wheel. Force the pure-Python pyseabreeze backend (pyusb
    # + libusb, ctypes-only) instead — must run before spectrometers loads.
    seabreeze.use("pyseabreeze")
    import seabreeze.spectrometers as sb
    _SEABREEZE_OK = True
    _IMPORT_ERR = ""
except Exception as _exc:                       # pragma: no cover
    _SEABREEZE_OK = False
    _IMPORT_ERR = str(_exc)


_INSTALL_HINT = (
    "python-seabreeze is required for the HR4000.\n"
    "  pip install seabreeze\n"
    "  python -m seabreeze_os_setup   # Linux only (udev rules)\n"
    "On macOS install libusb if needed:  brew install libusb"
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Spectrum:
    """A single acquired spectrum plus derived statistics."""
    wavelengths: list          # nm, one per pixel
    intensities: list          # counts, one per pixel (averaged/smoothed)
    integration_time_us: int
    n_averaged: int
    boxcar: int
    max_intensity: float       # saturation level (full-scale counts)
    timestamp: float           # time.time() when acquired

    peak_index: int = 0
    peak_wavelength: float = 0.0
    peak_intensity: float = 0.0
    total_counts: float = 0.0
    saturated: bool = False

    @property
    def pixels(self) -> int:
        return len(self.intensities)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class HR4000:
    """
    High-level, thread-safe interface to an Ocean Optics HR4000CG-UV-NIR
    spectrometer (works with any HR4000 — the variant only changes the optics).

    All USB I/O is serialized behind a lock so a polling thread and a control
    thread (e.g. a Flask request) can share one instance — but, like the other
    USB drivers in this project, prefer to keep all reads on ONE thread (see
    server/hr4000_app.py, which uses a single sampler thread + command queue).
    """

    def __init__(self, serial: str = "auto"):
        if not _SEABREEZE_OK:
            raise RuntimeError(f"{_INSTALL_HINT}\n\n(import error: {_IMPORT_ERR})")

        self._lock = threading.RLock()

        if serial == "auto":
            devs = sb.list_devices()
            if not devs:
                raise RuntimeError(
                    "No Ocean Optics spectrometer found on USB.\n"
                    "Check the cable/power and that seabreeze can see it:\n"
                    "  python -c \"import seabreeze.spectrometers as s; "
                    "print(s.list_devices())\""
                )
            self._spec = sb.Spectrometer(devs[0])
        else:
            self._spec = sb.Spectrometer.from_serial_number(serial)

        self.model = getattr(self._spec, "model", "HR4000CG-UV-NIR")
        self.serial = getattr(self._spec, "serial_number", "")
        self.pixels = int(self._spec.pixels)
        self._wavelengths = self._spec.wavelengths()
        try:
            self.max_intensity = float(self._spec.max_intensity)
        except Exception:
            self.max_intensity = 16383.0          # HR4000 is a 14-bit ADC
        try:
            lo, hi = self._spec.integration_time_micros_limits
            self.integration_limits_us = (int(lo), int(hi))
        except Exception:
            self.integration_limits_us = (3800, 10_000_000)

        # Acquisition settings (host-side averaging / smoothing / corrections).
        self._integration_us = max(self.integration_limits_us[0], 10_000)
        self.scans_to_average = 1
        self.boxcar_width = 0
        self.correct_dark = False
        self.correct_nonlinearity = False
        self._dark_supported = True
        self._nl_supported = True

        self.set_integration_time(self._integration_us)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @staticmethod
    def list_devices() -> list[str]:
        if not _SEABREEZE_OK:
            return []
        try:
            return [f"{d.model}  S/N {d.serial_number}" for d in sb.list_devices()]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @property
    def integration_time_us(self) -> int:
        return self._integration_us

    @integration_time_us.setter
    def integration_time_us(self, micros: float) -> None:
        self.set_integration_time(micros)

    def set_integration_time(self, micros: float) -> None:
        lo, hi = self.integration_limits_us
        micros = int(max(lo, min(hi, float(micros))))
        with self._lock:
            self._spec.integration_time_micros(micros)
        self._integration_us = micros

    def set_averaging(self, scans: int) -> None:
        self.scans_to_average = max(1, int(scans))

    def set_boxcar(self, width: int) -> None:
        self.boxcar_width = max(0, int(width))

    def set_dark_correction(self, enable: bool) -> None:
        self.correct_dark = bool(enable) and self._dark_supported

    def set_nonlinearity_correction(self, enable: bool) -> None:
        self.correct_nonlinearity = bool(enable) and self._nl_supported

    @property
    def wavelengths(self) -> list:
        return self._wavelengths.tolist()

    # ------------------------------------------------------------------
    # Acquisition
    # ------------------------------------------------------------------

    def _raw_intensities(self):
        """One frame of intensities, applying device corrections if supported."""
        try:
            return self._spec.intensities(
                correct_dark_counts=self.correct_dark,
                correct_nonlinearity=self.correct_nonlinearity,
            )
        except Exception:
            # Feature unsupported on this unit — disable and read uncorrected.
            self._dark_supported = self._nl_supported = False
            self.correct_dark = self.correct_nonlinearity = False
            return self._spec.intensities()

    def _acquire(self):
        """Acquire (and host-average) intensities, then boxcar-smooth."""
        acc = None
        n = self.scans_to_average
        for _ in range(n):
            y = self._raw_intensities()
            acc = y if acc is None else acc + y
        y = acc / n
        if self.boxcar_width > 0:
            k = 2 * self.boxcar_width + 1
            kernel = np.ones(k) / k
            y = np.convolve(y, kernel, mode="same")
        return y

    def read(self) -> Spectrum:
        """Acquire one full spectrum snapshot with derived statistics."""
        with self._lock:
            y = self._acquire()
        peak_i = int(np.argmax(y))
        spec = Spectrum(
            wavelengths=self._wavelengths.tolist(),
            intensities=y.tolist(),
            integration_time_us=self._integration_us,
            n_averaged=self.scans_to_average,
            boxcar=self.boxcar_width,
            max_intensity=self.max_intensity,
            timestamp=time.time(),
            peak_index=peak_i,
            peak_wavelength=float(self._wavelengths[peak_i]),
            peak_intensity=float(y[peak_i]),
            total_counts=float(np.sum(y)),
            saturated=bool(np.max(y) >= 0.99 * self.max_intensity),
        )
        return spec

    # ------------------------------------------------------------------
    # Context manager / cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        try:
            self._spec.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Devices:", HR4000.list_devices())
    with HR4000() as spec:
        print(f"Model {spec.model}  S/N {spec.serial}  {spec.pixels} px  "
              f"{spec.wavelengths[0]:.1f}–{spec.wavelengths[-1]:.1f} nm  "
              f"max {spec.max_intensity:.0f} counts")
        spec.integration_time_us = 10000
        for _ in range(3):
            s = spec.read()
            print(f"  peak {s.peak_intensity:8.0f} @ {s.peak_wavelength:7.2f} nm   "
                  f"total {s.total_counts:.3e}   sat={s.saturated}")
            time.sleep(0.2)

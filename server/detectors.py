"""
Detector abstraction + auto-detection for the scan engine.

The scan engine records one signal value per actuator step. This module lets it
do that with whichever detector is attached, behind a uniform `Detector`
interface, and an `DetectorManager` that probes all backends at startup:

  * PM400 power meter        (power_monitor)  → signal = optical power (W)
  * Ocean Optics HR4000      (hr4000_app)     → signal = a scalar reduced from
  * Avantes SensLine/NIRLine (avantes_app)      each spectrum: peak / integrated
                                                / intensity @ a chosen wavelength
                                                (full spectra are also captured)

Mirrors server/actuators.py. The user setup normally has one detector; if more
than one is connected the UI can switch between them.
"""

from __future__ import annotations

import threading

from server import power_monitor as power
from server import hr4000_app as hr
from server import avantes_app as av


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------

class Detector:
    id: str = ""
    name: str = ""
    kind: str = ""          # "power" | "spectrometer"

    def connected(self) -> bool: raise NotImplementedError
    def error(self) -> str: return ""

    def metrics(self) -> list[dict]:
        """Selectable signal metrics; for a power meter just one."""
        return []

    def wavelength_range(self):
        """(min,max) nm for spectrometers, else None."""
        return None

    def y_meta(self, metric_key: str) -> dict:
        """Plot axis metadata: {label, unit, kind}. kind = 'power' | 'counts'."""
        raise NotImplementedError

    def measure(self, metric_key: str, wavelength, dwell_s: float) -> dict | None:
        """One settled measurement → {value, unit, std, n, spectrum?}."""
        raise NotImplementedError

    def live(self, metric_key: str, wavelength):
        """Instantaneous signal value for the header readout (or None)."""
        return None

    def begin_scan(self) -> None: ...
    def end_scan(self) -> None: ...

    def set_acquisition(self, integration_ms=None, averages=None) -> bool:
        """Set integration time (ms) / averaging. False if not applicable."""
        return False

    def acquisition_state(self) -> dict:
        return {}

    def status(self, metric_key: str, wavelength) -> dict:
        return {"connected": self.connected(), "value": self.live(metric_key, wavelength)}


# ---------------------------------------------------------------------------
# PM400 power meter
# ---------------------------------------------------------------------------

class PowerDetector(Detector):
    id = "power"
    name = "PM400 power meter"
    kind = "power"

    def connected(self) -> bool: return power.is_connected()
    def error(self) -> str: return power.connect_error()

    def metrics(self) -> list[dict]:
        return [{"key": "power", "label": "Optical power", "unit": "W"}]

    def y_meta(self, metric_key: str) -> dict:
        return {"label": "Power", "unit": "W", "kind": "power"}

    def measure(self, metric_key, wavelength, dwell_s):
        a = power.collect_average(dwell_s=dwell_s)
        if a is None:
            return None
        return {"value": a["mean_w"], "unit": "W", "std": a["std_w"],
                "vmin": a["min_w"], "vmax": a["max_w"], "n": a["n"]}

    def live(self, metric_key, wavelength):
        return power.latest_power_w()


# ---------------------------------------------------------------------------
# Spectrometer detectors (HR4000 / Avantes share one adapter)
# ---------------------------------------------------------------------------

# Default metric is the integrated area over ALL wavelengths (∫ I dλ). Per-band
# integrated areas are computed in post-processing (see scanner /api/scan/bands).
_SPECTRO_METRICS = [
    {"key": "area", "label": "Integrated area (all λ)", "unit": "counts·nm"},
    {"key": "peak", "label": "Peak intensity", "unit": "counts"},
    {"key": "at_wavelength", "label": "Intensity @ λ", "unit": "counts"},
]


class SpectrometerDetector(Detector):
    kind = "spectrometer"

    def __init__(self, module, det_id: str, name: str):
        self._m = module
        self.id = det_id
        self.name = name

    def connected(self) -> bool: return self._m.is_connected()
    def error(self) -> str: return self._m.connect_error()

    def metrics(self) -> list[dict]:
        return [dict(m) for m in _SPECTRO_METRICS]

    def wavelength_range(self):
        wl = self._m.wavelengths()
        return (round(wl[0], 2), round(wl[-1], 2)) if wl else None

    def y_meta(self, metric_key: str) -> dict:
        m = next((x for x in _SPECTRO_METRICS if x["key"] == metric_key), None)
        label = m["label"] if m else "Intensity"
        unit = m["unit"] if m else "counts"
        return {"label": label, "unit": unit, "kind": "counts"}

    def measure(self, metric_key, wavelength, dwell_s):
        return self._m.collect_metric(metric_key, wavelength, dwell_s=dwell_s)

    def live(self, metric_key, wavelength):
        return self._m.latest_metric(metric_key, wavelength)

    def set_acquisition(self, integration_ms=None, averages=None) -> bool:
        self._m.scan_set_acquisition(integration_ms=integration_ms, averages=averages)
        return True

    def acquisition_state(self) -> dict:
        return self._m.scan_acquisition_state()

    def wavelengths(self) -> list:
        return self._m.wavelengths()


# ---------------------------------------------------------------------------
# Manager — auto-detect + active selection
# ---------------------------------------------------------------------------

class DetectorManager:
    def __init__(self):
        self._detectors: list[Detector] = [
            PowerDetector(),
            SpectrometerDetector(hr, "hr4000", "Ocean Optics HR4000"),
            SpectrometerDetector(av, "avantes", "Avantes AvaSpec"),
        ]
        self._active_id: str | None = None
        self._lock = threading.Lock()

    def detect(self) -> None:
        """Probe every detector backend (each handles its own failure)."""
        print("Detecting detectors…")
        for label, fn in (("PM400", power._init_monitor),
                          ("HR4000", hr._init_spectrometer),
                          ("Avantes", av._init_spectrometer)):
            try:
                fn()
            except Exception as exc:
                print(f"  {label} probe error: {exc}")
        connected = [d for d in self._detectors if d.connected()]
        with self._lock:
            if self._active_id is None or not self._by_id(self._active_id).connected():
                self._active_id = connected[0].id if connected else None
        names = ", ".join(d.name for d in connected) or "none"
        print(f"Detectors connected: {names}  | active: {self._active_id}")

    def _by_id(self, did) -> Detector | None:
        return next((d for d in self._detectors if d.id == did), None)

    def all(self) -> list[Detector]:
        return list(self._detectors)

    def active(self) -> Detector | None:
        with self._lock:
            return self._by_id(self._active_id) if self._active_id else None

    def set_active(self, did: str) -> tuple[bool, str]:
        d = self._by_id(did)
        if d is None:
            return False, f"Unknown detector '{did}'"
        if not d.connected():
            return False, f"{d.name} is not connected"
        with self._lock:
            self._active_id = did
        return True, did


manager = DetectorManager()

"""
Thorlabs PM400 Optical Power / Energy Meter controller.

Talks to a PM400 console (with an S120VC photodiode sensor, or any other
Thorlabs sensor) over USBTMC using SCPI, via PyVISA + the pure-python
pyvisa-py backend — no NI-VISA and no Thorlabs TLPM.dll required, so it works
on macOS/Linux as well as Windows.

Hardware chain:  PC <--USB(USBTMC)--> PM400 <--DB9--> S120VC photodiode

Requirements:
    pip install pyvisa pyvisa-py pyusb
    (libusb is already present on macOS via the system; `brew install libusb`
     if pyusb cannot find a backend.)

The PM400 enumerates as Thorlabs VID 0x1313, PID 0x8075 and appears to PyVISA
as e.g.  USB0::4883::32885::P5007939::0::INSTR

Quick use:
    with PM400() as pm:
        pm.wavelength = 635          # nm
        print(pm.read_power())       # watts
        print(pm.read())             # full Measurement snapshot

SCPI reference: Thorlabs PM100x/PM400 SCPI Programmer's manual, and the
Light_Analysis_Examples repo (github.com/Thorlabs/Light_Analysis_Examples).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

import pyvisa


# ---------------------------------------------------------------------------
# USB identity
# ---------------------------------------------------------------------------

THORLABS_VID = 0x1313
# Known Thorlabs power-meter console PIDs (PM100x / PM200 / PM400 family).
PM_PIDS = {
    0x8072: "PM100USB",
    0x8078: "PM100D",
    0x8079: "PM100A",
    0x807A: "PM160",
    0x807B: "PM160T",
    0x80B0: "PM200",
    0x8075: "PM400",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SensorInfo:
    """Parsed reply of SYST:SENSor:IDN? (the connected sensor head)."""
    name: str          # e.g. "S120VC"
    serial: str        # sensor serial number
    cal_date: str      # last calibration date string
    type: int          # sensor type code (1 = photodiode, 2 = thermal, 3 = pyro)
    subtype: int       # subtype code
    flags: int         # capability flag bitmask

    TYPE_NAMES = {0: "None", 1: "Photodiode", 2: "Thermal", 3: "Pyroelectric"}

    @property
    def type_name(self) -> str:
        return self.TYPE_NAMES.get(self.type, f"Type {self.type}")

    @property
    def connected(self) -> bool:
        return self.type != 0 and bool(self.name)


@dataclass
class Measurement:
    """A single snapshot of everything we read each poll."""
    power_w: float              # measured power, watts
    current_a: Optional[float]  # photodiode current, amps (photodiode sensors only)
    wavelength_nm: float
    power_range_w: float        # current upper range limit, watts
    auto_range: bool
    overrange: bool
    timestamp: float            # time.time() when sampled

    @property
    def power_dbm(self) -> float:
        """Power in dBm (10·log10(P/1mW)). -inf for non-positive power."""
        if self.power_w <= 0:
            return float("-inf")
        return 10.0 * math.log10(self.power_w / 1e-3)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class PM400:
    """
    High-level, thread-safe interface to a Thorlabs PM400 power meter.

    All VISA I/O is serialized behind a lock so a polling thread and a control
    thread (e.g. a Flask request) can share one instance safely.
    """

    def __init__(self, resource: str = "auto", timeout_ms: int = 3000):
        self._lock = threading.RLock()
        self._rm = pyvisa.ResourceManager("@py")

        if resource == "auto":
            resource = self._find_resource(self._rm)

        self._resource_name = resource
        self._inst = self._rm.open_resource(resource)
        self._inst.timeout = timeout_ms
        # USBTMC: make sure reads stop on the device's line terminator.
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"

        # Identify and configure a sane default measurement state.
        self.idn = self._query("*IDN?")
        # vendor, model, serial, firmware
        parts = [p.strip() for p in self.idn.split(",")]
        self.vendor   = parts[0] if len(parts) > 0 else "THORLABS"
        self.model    = parts[1] if len(parts) > 1 else "PM400"
        self.serial   = parts[2] if len(parts) > 2 else ""
        self.firmware = parts[3] if len(parts) > 3 else ""

        # Always work internally in linear watts; dBm is derived host-side so
        # statistics stay consistent regardless of the display unit.
        self._write("SENS:POW:UNIT W")
        # Configure the console to measure power.
        self._write("CONF:POW")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _find_resource(rm: pyvisa.ResourceManager) -> str:
        # Thorlabs PIDs in VISA's decimal form: 0x1313 = 4883.
        for res in rm.list_resources():
            if res.startswith("USB") and "::4883::" in res:
                return res
        # Fall back to any USB instrument if exactly one is present.
        usb = [r for r in rm.list_resources() if r.startswith("USB")]
        if len(usb) == 1:
            return usb[0]
        raise RuntimeError(
            "No Thorlabs power meter (USB VID 0x1313) found.\n"
            "Plug in the PM400 and check it appears in:\n"
            "  python -c \"import pyvisa; "
            "print(pyvisa.ResourceManager('@py').list_resources())\"\n"
            f"Resources seen: {rm.list_resources()}"
        )

    @staticmethod
    def list_devices() -> list[str]:
        """Return VISA resource strings for connected Thorlabs power meters."""
        rm = pyvisa.ResourceManager("@py")
        return [r for r in rm.list_resources()
                if r.startswith("USB") and "::4883::" in r]

    # ------------------------------------------------------------------
    # Low-level SCPI (locked)
    # ------------------------------------------------------------------

    def _query(self, cmd: str) -> str:
        with self._lock:
            return self._inst.query(cmd).strip()

    def _query_float(self, cmd: str) -> float:
        return float(self._query(cmd))

    def _write(self, cmd: str) -> None:
        with self._lock:
            self._inst.write(cmd)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        try:
            self._inst.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Sensor info
    # ------------------------------------------------------------------

    def get_sensor_info(self) -> SensorInfo:
        """Parse SYST:SENSor:IDN? — info about the connected sensor head."""
        raw = self._query("SYST:SENS:IDN?")
        # e.g.  "S120VC","251111104","12-NOV-2025",1,18,289
        fields = [f.strip().strip('"') for f in raw.split(",")]
        def _int(i):
            try:
                return int(fields[i])
            except (IndexError, ValueError):
                return 0
        return SensorInfo(
            name=fields[0] if fields else "",
            serial=fields[1] if len(fields) > 1 else "",
            cal_date=fields[2] if len(fields) > 2 else "",
            type=_int(3),
            subtype=_int(4),
            flags=_int(5),
        )

    # ------------------------------------------------------------------
    # Wavelength correction
    # ------------------------------------------------------------------

    @property
    def wavelength(self) -> float:
        """Active wavelength-correction setting, nm."""
        return self._query_float("SENS:CORR:WAV?")

    @wavelength.setter
    def wavelength(self, nm: float) -> None:
        lo, hi = self.wavelength_range
        nm = max(lo, min(hi, float(nm)))
        self._write(f"SENS:CORR:WAV {nm:.1f}")

    @property
    def wavelength_range(self) -> tuple[float, float]:
        """(min, max) wavelength the connected sensor supports, nm."""
        lo = self._query_float("SENS:CORR:WAV? MIN")
        hi = self._query_float("SENS:CORR:WAV? MAX")
        return lo, hi

    # ------------------------------------------------------------------
    # Averaging
    # ------------------------------------------------------------------

    @property
    def averaging(self) -> int:
        """Number of samples averaged per reading (1 sample ≈ 3 ms)."""
        return int(round(self._query_float("SENS:AVER:COUN?")))

    @averaging.setter
    def averaging(self, count: int) -> None:
        count = max(1, int(count))
        self._write(f"SENS:AVER:COUN {count}")

    # ------------------------------------------------------------------
    # Power range
    # ------------------------------------------------------------------

    @property
    def auto_range(self) -> bool:
        return self._query("SENS:POW:RANG:AUTO?").startswith(("1", "ON"))

    @auto_range.setter
    def auto_range(self, on: bool) -> None:
        self._write(f"SENS:POW:RANG:AUTO {'ON' if on else 'OFF'}")

    @property
    def power_range(self) -> float:
        """Current upper power-range limit, watts."""
        return self._query_float("SENS:POW:RANG:UPP?")

    @power_range.setter
    def power_range(self, watts: float) -> None:
        """Set the manual upper power range (disables auto-range)."""
        self._write(f"SENS:POW:RANG:UPP {watts:.6e}")

    # ------------------------------------------------------------------
    # Dark / zero adjustment
    # ------------------------------------------------------------------

    def zero(self) -> float:
        """
        Perform a dark-current (zero) adjustment. BLOCK the beam first — this
        captures the present reading as the zero offset. Returns the magnitude.
        """
        self._write("SENS:CORR:COLL:ZERO:INIT")
        # The console needs a moment to settle the zero acquisition.
        time.sleep(0.6)
        return self.zero_magnitude

    @property
    def zero_magnitude(self) -> float:
        return self._query_float("SENS:CORR:COLL:ZERO:MAGN?")

    def clear_zero(self) -> None:
        self._write("SENS:CORR:COLL:ZERO:STAT 0")

    # ------------------------------------------------------------------
    # Measurement
    # ------------------------------------------------------------------

    def read_power(self) -> float:
        """Single power measurement, watts."""
        return self._query_float("MEAS:POW?")

    def read_current(self) -> Optional[float]:
        """Photodiode current, amps. None if the sensor has no current reading."""
        try:
            return self._query_float("MEAS:CURR?")
        except Exception:
            return None

    def read(self) -> Measurement:
        """
        Full snapshot read in one locked burst: power, current, range and the
        over-range flag. Wavelength is included from cache-light queries so the
        UI gets a coherent picture each poll.
        """
        with self._lock:
            power = float(self._inst.query("MEAS:POW?").strip())
            try:
                current = float(self._inst.query("MEAS:CURR?").strip())
            except Exception:
                current = None
            wl = float(self._inst.query("SENS:CORR:WAV?").strip())
            rng = float(self._inst.query("SENS:POW:RANG:UPP?").strip())
            auto = self._inst.query("SENS:POW:RANG:AUTO?").strip().startswith(("1", "ON"))
            # Questionable-condition register, bit-set indicates over-range.
            try:
                qcond = int(float(self._inst.query("STAT:QUES:COND?").strip()))
            except Exception:
                qcond = 0
        overrange = bool(qcond & 0x0001) or (power >= rng > 0)
        return Measurement(
            power_w=power,
            current_a=current,
            wavelength_nm=wl,
            power_range_w=rng,
            auto_range=auto,
            overrange=overrange,
            timestamp=time.time(),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def format_power(watts: float) -> tuple[float, str]:
        """
        Scale a wattage to an engineering value + unit string, the way the
        Thorlabs Optical Power Monitor auto-ranges the big readout.
        Returns (value, unit) e.g. (1.234, 'µW').
        """
        a = abs(watts)
        if a == 0 or math.isnan(a):
            return 0.0, "W"
        for thresh, scale, unit in (
            (1e-9, 1e12, "pW"),
            (1e-6, 1e9,  "nW"),
            (1e-3, 1e6,  "µW"),
            (1e0,  1e3,  "mW"),
        ):
            if a < thresh:
                return watts * scale, unit
        return watts, "W"

    @staticmethod
    def power_density(watts: float, beam_diameter_mm: float) -> float:
        """W/cm² for a given beam diameter (mm). 0 if diameter invalid."""
        if beam_diameter_mm <= 0:
            return 0.0
        area_cm2 = math.pi * (beam_diameter_mm / 20.0) ** 2  # r in cm = d_mm/2/10
        return watts / area_cm2


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Devices:", PM400.list_devices())
    with PM400() as pm:
        print("IDN     :", pm.idn)
        s = pm.get_sensor_info()
        print("Sensor  :", s.name, s.serial, s.cal_date, f"({s.type_name})")
        lo, hi = pm.wavelength_range
        print(f"Wave    : {pm.wavelength:.0f} nm  (range {lo:.0f}-{hi:.0f} nm)")
        print("Averaging:", pm.averaging)
        print("Auto rng:", pm.auto_range)
        for _ in range(5):
            m = pm.read()
            val, unit = PM400.format_power(m.power_w)
            print(f"  {val:8.3f} {unit:>3}   ({m.power_dbm:7.2f} dBm)   "
                  f"I={m.current_a}")
            time.sleep(0.3)

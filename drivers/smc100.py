"""
Newport SMC100CC single-axis motion controller — driver + actuator profiles.

Talks to an SMC100CC (closed-loop DC-servo) controller over RS-232 using the
Newport address-prefixed ASCII command protocol, via pyserial. Built for the
MKS / Newport **LTA-HS** high-speed motorized actuator (50 mm travel, 5 mm/s,
0.1 µm minimum incremental motion, DC servo with a 2048-count encoder).

Note on terminology: the LTA-HS is a *motorized* (DC-servo lead-screw) actuator,
not a piezo — it is driven by the SMC100CC, whereas a piezo flexure stage would
use a piezo controller. This module models it accurately as a motorized stage.

Hardware chain:  PC <--USB/RS-232--> SMC100CC <--motor+encoder--> LTA-HS

Serial settings (SMC100 default): 57600 baud, 8 data bits, no parity, 1 stop
bit, XON/XOFF software flow control, commands terminated with CR+LF.

Command syntax:  <addr><CMD><param>\\r\\n   e.g.  "1PA10.5"  (move axis 1 to 10.5 mm)
Query syntax:    <addr><CMD>?            e.g.  "1VA?"      (read velocity)
A few status queries take no '?':  TP (position), TS (state), TE (last error).

Requirements:  pip install pyserial

References:
  * Newport SMC100CC & SMC100PP User's Manual / Command Interface Manual.
  * Newport LTA-HS product page & LTA Series user manual.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import serial
import serial.tools.list_ports


# ---------------------------------------------------------------------------
# Actuator profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActuatorProfile:
    """Static description of a motorized actuator driven by an SMC100."""
    name: str
    controller: str
    motor_type: str
    travel_mm: float
    min_incremental_motion_um: float   # smallest reliable step
    max_velocity_mm_s: float
    default_velocity_mm_s: float
    encoder_counts_per_rev: int
    notes: str = ""


# MKS / Newport LTA-HS — high-speed motorized actuator.
# Specs from the Newport LTA-HS datasheet (50 mm travel, 5 mm/s max speed,
# 0.1 µm min. incremental motion, miniature DC servo motor + 2048-count encoder).
LTA_HS = ActuatorProfile(
    name="LTA-HS",
    controller="Newport SMC100CC",
    motor_type="DC servo (lead screw)",
    travel_mm=50.0,
    min_incremental_motion_um=0.1,
    max_velocity_mm_s=5.0,
    default_velocity_mm_s=2.0,
    encoder_counts_per_rev=2048,
    notes="High-speed motorized actuator. Push 50 N / pull 40 N. "
          "Uni-directional repeatability 0.15 µm typ. Requires a home search "
          "(OR) after power-up before absolute moves.",
)

# Registry so a UI can offer a dropdown later.
PROFILES = {p.name: p for p in (LTA_HS,)}


# ---------------------------------------------------------------------------
# Controller state machine
# ---------------------------------------------------------------------------

class SMCState(Enum):
    NOT_REFERENCED = "NOT REFERENCED"
    CONFIGURATION  = "CONFIGURATION"
    HOMING         = "HOMING"
    MOVING         = "MOVING"
    READY          = "READY"
    DISABLE        = "DISABLE"
    JOGGING        = "JOGGING"
    UNKNOWN        = "UNKNOWN"


# TS returns 6 hex chars: 4 = positioner-error bits, 2 = controller state.
# Map of the documented 2-hex state codes (lower-cased) → state.
_STATE_CODES = {
    "0a": SMCState.NOT_REFERENCED,  # from reset
    "0b": SMCState.NOT_REFERENCED,  # from HOMING
    "0c": SMCState.NOT_REFERENCED,  # from CONFIGURATION
    "0d": SMCState.NOT_REFERENCED,  # from DISABLE
    "0e": SMCState.NOT_REFERENCED,  # from READY
    "0f": SMCState.NOT_REFERENCED,  # from MOVING
    "10": SMCState.NOT_REFERENCED,  # from JOGGING / no parameters in memory
    "11": SMCState.NOT_REFERENCED,
    "14": SMCState.CONFIGURATION,
    "1e": SMCState.HOMING,          # commanded from RS-232-C
    "1f": SMCState.HOMING,          # commanded by SMC-RC
    "28": SMCState.MOVING,
    "32": SMCState.READY,           # from HOMING
    "33": SMCState.READY,           # from MOVING
    "34": SMCState.READY,           # from DISABLE
    "35": SMCState.READY,           # from JOGGING
    "36": SMCState.READY,
    "37": SMCState.READY,
    "38": SMCState.READY,
    "3c": SMCState.DISABLE,         # from READY
    "3d": SMCState.DISABLE,         # from MOVING
    "3e": SMCState.DISABLE,         # from JOGGING
    "46": SMCState.JOGGING,         # from READY
    "47": SMCState.JOGGING,         # from DISABLE
}

# TE — last command error (single letter). Confident subset; others fall back.
_TE_ERRORS = {
    "@": "No error",
    "A": "Unknown command code or wrong (floating-point) controller address",
    "B": "Controller address not correct",
    "C": "Parameter missing or out of range",
    "D": "Command not allowed",
    "E": "Home search already in progress",
    "G": "Displacement out of software limits",
}


@dataclass
class SMCStatus:
    """Parsed snapshot of the controller state."""
    state: SMCState
    state_code: str            # raw 2-hex state code
    error_bits: int            # 16-bit positioner-error word from TS

    @property
    def is_moving(self) -> bool:
        return self.state in (SMCState.MOVING, SMCState.HOMING, SMCState.JOGGING)

    @property
    def is_referenced(self) -> bool:
        return self.state not in (SMCState.NOT_REFERENCED, SMCState.CONFIGURATION,
                                  SMCState.UNKNOWN)

    @property
    def is_ready(self) -> bool:
        return self.state == SMCState.READY


class SMC100Error(RuntimeError):
    """Raised when the controller reports a command error via TE."""


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class SMC100:
    """
    High-level interface to a Newport SMC100CC controller + LTA-HS actuator.

    Thread-safe: all serial I/O is serialized behind a re-entrant lock so a
    polling thread (web status) and a control thread (web request) can share one
    instance. Moves are non-blocking — PA/PR return immediately and the stage
    reports MOVING via TS until it settles; use wait_until_ready() to block.

    Usage:
        with SMC100() as smc:
            smc.ensure_referenced()      # home search if needed (~tens of s)
            smc.set_velocity(2.0)        # mm/s
            smc.move_absolute(10.0)      # mm
            smc.wait_until_ready()
            print(smc.get_position())    # mm
    """

    BAUD = 57600
    WRITE_SETTLE_S = 0.05   # gap after a write before the controller answers a query
    # USB-RS232 adapters commonly used with the SMC100 (FTDI / Prolific / Keyspan).
    _PORT_HINTS = ("usbserial", "usbmodem", "ftdi", "prolific", "keyspan",
                   "newport", "ttyusb", "ttyacm")

    def __init__(
        self,
        port: str = "auto",
        address: int = 1,
        profile: ActuatorProfile = LTA_HS,
        timeout: float = 2.0,
    ):
        self._addr = int(address)
        self.profile = profile
        self._lock = threading.RLock()
        self._timeout = timeout

        if port == "auto":
            port = self._find_port()
        self._port = port

        self._ser = serial.Serial(
            port=port,
            baudrate=self.BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            xonxoff=True,          # SMC100 uses XON/XOFF software flow control
            rtscts=False,
            timeout=timeout,
        )
        time.sleep(0.15)
        self._ser.reset_input_buffer()

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @classmethod
    def _candidate_ports(cls) -> list:
        ports = list(serial.tools.list_ports.comports())
        likely = [p for p in ports
                  if any(h in (p.device + " " + (p.description or "")).lower()
                         for h in cls._PORT_HINTS)]
        return likely or ports

    @classmethod
    def _find_port(cls) -> str:
        cands = cls._candidate_ports()
        if len(cands) == 1:
            return cands[0].device
        if not cands:
            raise RuntimeError(
                "No serial ports found. Plug in the SMC100's USB/RS-232 adapter."
            )
        listing = "\n".join(f"  {p.device}  [{p.description}]" for p in cands)
        raise RuntimeError(
            "Multiple serial ports found — pass an explicit port=...:\n" + listing
        )

    @classmethod
    def list_devices(cls) -> list[str]:
        return [f"{p.device}  [{p.description}]"
                for p in serial.tools.list_ports.comports()]

    # ------------------------------------------------------------------
    # Low-level protocol
    # ------------------------------------------------------------------

    def _write(self, cmd: str) -> None:
        """
        Send a write-only command (no reply expected), e.g. 'PA10.5'.

        The SMC100 needs a brief gap after a set command before it will answer
        the next query — firing a query immediately after a write (e.g. VA then
        VA?, or PA then TE) makes the query time out. We settle for
        WRITE_SETTLE_S after every write (outside the lock so the status poller
        isn't blocked).
        """
        with self._lock:
            self._ser.write(f"{self._addr}{cmd}\r\n".encode("ascii"))
        if self.WRITE_SETTLE_S:
            time.sleep(self.WRITE_SETTLE_S)

    def _query(self, code: str, suffix: str = "") -> str:
        """
        Send a query and return the payload after the '<addr><code>' echo.
        e.g. _query('TP') sends '1TP', reads '1TP12.345' → '12.345'.
             _query('VA', '?') sends '1VA?', reads '1VA2.0' → '2.0'.
        """
        prefix = f"{self._addr}{code}"
        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write(f"{prefix}{suffix}\r\n".encode("ascii"))
            line = self._ser.readline().decode("ascii", errors="replace").strip()
        if not line:
            raise TimeoutError(f"No reply to {prefix}{suffix}")
        if line.startswith(prefix):
            return line[len(prefix):]
        return line   # unexpected framing — hand back raw for the caller to see

    def _query_float(self, code: str, suffix: str = "") -> float:
        return float(self._query(code, suffix))

    # ------------------------------------------------------------------
    # Identity / info
    # ------------------------------------------------------------------

    def get_firmware(self) -> str:
        """Controller firmware/revision string (VE)."""
        return self._query("VE")

    def get_stage_id(self) -> str:
        """Connected stage identifier (ID?). Empty if the stage is not ESP-coded."""
        try:
            return self._query("ID", "?")
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> SMCStatus:
        """Parse TS → controller state + positioner-error word."""
        raw = self._query("TS")
        raw = raw.strip()
        if len(raw) >= 6:
            try:
                err = int(raw[:4], 16)
            except ValueError:
                err = 0
            code = raw[4:6].lower()
        else:
            err, code = 0, raw.lower()
        return SMCStatus(state=_STATE_CODES.get(code, SMCState.UNKNOWN),
                         state_code=code, error_bits=err)

    def get_last_error(self) -> tuple[str, str]:
        """
        Last command error (TE): (letter, human description). '@' means no error.
        Returns ('?', …) instead of raising if the controller doesn't answer the
        TE query, so a write-only move command is never reported as failed just
        because the follow-up query timed out.
        """
        try:
            letter = self._query("TE").strip() or "@"
        except TimeoutError:
            return "?", "No reply to TE query"
        return letter, _TE_ERRORS.get(letter, f"Error code '{letter}'")

    def get_position(self) -> float:
        """Current position in mm (TP)."""
        return self._query_float("TP")

    def get_target(self) -> float:
        """Commanded target position in mm (PA?)."""
        return self._query_float("PA", "?")

    # ------------------------------------------------------------------
    # Motion parameters
    # ------------------------------------------------------------------

    def get_velocity(self) -> float:
        """Profile velocity in mm/s (VA?)."""
        return self._query_float("VA", "?")

    def set_velocity(self, mm_s: float) -> None:
        v = max(0.0, min(self.profile.max_velocity_mm_s, float(mm_s)))
        self._write(f"VA{v:.6g}")

    def get_acceleration(self) -> float:
        return self._query_float("AC", "?")

    def set_acceleration(self, mm_s2: float) -> None:
        self._write(f"AC{float(mm_s2):.6g}")

    def get_limits(self) -> tuple[float, float]:
        """Software travel limits in mm: (negative SL, positive SR)."""
        try:
            return self._query_float("SL", "?"), self._query_float("SR", "?")
        except Exception:
            return 0.0, self.profile.travel_mm

    # ------------------------------------------------------------------
    # Motion
    # ------------------------------------------------------------------

    def move_absolute(self, position_mm: float) -> None:
        """Move to an absolute position in mm (PA). Non-blocking."""
        lo, hi = 0.0, self.profile.travel_mm
        if not lo - 1e-6 <= position_mm <= hi + 1e-6:
            raise ValueError(f"Position {position_mm} mm out of travel 0–{hi} mm")
        self._write(f"PA{float(position_mm):.6g}")

    def move_relative(self, delta_mm: float) -> None:
        """Move by a relative displacement in mm (PR). Non-blocking."""
        self._write(f"PR{float(delta_mm):.6g}")

    def home(self) -> None:
        """Execute home search (OR) → drives toward the reference, then READY."""
        self._write("OR")

    def stop(self) -> None:
        """Stop motion (ST)."""
        self._write("ST")

    def set_enabled(self, enable: bool) -> None:
        """Leave/enter DISABLE state (MM1 = enable, MM0 = disable)."""
        self._write("MM1" if enable else "MM0")

    def reset(self) -> None:
        """Reset the controller (RS). Returns to NOT REFERENCED; takes a few s."""
        self._write("RS")

    # ------------------------------------------------------------------
    # Configuration state (rarely needed — changing stored stage params)
    # ------------------------------------------------------------------

    def enter_config(self) -> None:
        self._write("PW1")

    def leave_config(self) -> None:
        self._write("PW0")
        time.sleep(0.5)

    # ------------------------------------------------------------------
    # Blocking helpers
    # ------------------------------------------------------------------

    def wait_until_ready(self, timeout_s: float = 60.0,
                         poll_s: float = 0.2) -> bool:
        """Poll TS until the controller reaches READY (or timeout)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            st = self.get_status()
            if st.is_ready:
                return True
            if st.state in (SMCState.NOT_REFERENCED, SMCState.DISABLE,
                            SMCState.UNKNOWN):
                # Not going to become READY on its own — bail out.
                if st.state != SMCState.UNKNOWN:
                    return False
            time.sleep(poll_s)
        return False

    def ensure_referenced(self, timeout_s: float = 90.0) -> bool:
        """
        Make sure the stage is homed/referenced. If NOT REFERENCED, run a home
        search and wait for READY. Returns True if READY afterwards.
        """
        st = self.get_status()
        if st.state == SMCState.DISABLE:
            self.set_enabled(True)
            time.sleep(0.2)
            st = self.get_status()
        if st.is_referenced:
            return True
        self.home()
        return self.wait_until_ready(timeout_s=timeout_s)

    # ------------------------------------------------------------------
    # Context manager / cleanup
    # ------------------------------------------------------------------

    @property
    def port(self) -> str:
        return self._port

    @property
    def address(self) -> int:
        return self._addr

    def close(self) -> None:
        try:
            if self._ser.is_open:
                self._ser.close()
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
    print("Serial ports:", SMC100.list_devices())
    with SMC100() as smc:
        print("Port      :", smc.port, " addr", smc.address)
        print("Firmware  :", smc.get_firmware())
        print("Stage ID  :", smc.get_stage_id())
        st = smc.get_status()
        print("State     :", st.state.value, f"(0x{st.state_code})")
        print("Position  :", smc.get_position(), "mm")
        print("Velocity  :", smc.get_velocity(), "mm/s")
        print("Limits    :", smc.get_limits(), "mm")
        print("Last error:", smc.get_last_error())

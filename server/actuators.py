"""
Actuator abstraction + auto-detection for the merged Scan app.

The scan engine should not care *which* controller/actuator is attached. This
module wraps each backend module (the BPC301/DRV517 piezo in `app`, and the
Newport SMC100/LTA-HS in `smc100_app`) behind a uniform `Actuator` interface,
and provides an `ActuatorManager` that probes both at startup and tracks which
are connected. The user setup normally has one attached; if both are present
the UI can switch between them.

Each Actuator exposes:
  * axes()            — scan axes it offers (e.g. piezo: position µm + voltage V;
                        SMC100: position mm)
  * check_ready(axis) — whether a scan can start on that axis (and why not)
  * prepare(axis)     — pre-scan setup (e.g. enable / open-loop)
  * begin_scan/end_scan — busy gating
  * move(axis, sp, settle) — step to a setpoint, return the actual position/value
  * a "ready action" (Zero for piezo, Home for SMC100) with progress state
  * status()          — a live snapshot for the UI
"""

from __future__ import annotations

import threading

from server import piezo as piezo
from server import smc100_app as smc


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------

class Actuator:
    id: str = ""
    name: str = ""
    controller: str = ""

    # — connection —
    def connected(self) -> bool: raise NotImplementedError
    def error(self) -> str: return ""

    # — scan axes —
    def axes(self) -> list[dict]: raise NotImplementedError

    # — scan lifecycle —
    def begin_scan(self) -> None: ...
    def end_scan(self) -> None: ...
    def check_ready(self, axis_key: str) -> tuple[bool, str]: return True, ""
    def prepare(self, axis_key: str) -> None: ...
    def move(self, axis_key: str, setpoint: float, settle_s: float) -> float:
        raise NotImplementedError

    # — readiness action (Zero / Home) —
    def ready_action_label(self, axis_key: str) -> str | None: return None
    def start_ready_action(self) -> tuple[bool, str]: return False, "n/a"
    def ready_action_state(self) -> dict: return {}

    # — live status for the UI —
    def status(self) -> dict: raise NotImplementedError

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "controller": self.controller,
                "connected": self.connected(), "error": self.error(),
                "axes": self.axes()}


# ---------------------------------------------------------------------------
# Piezo (BPC301 / DRV517) adapter
# ---------------------------------------------------------------------------

class PiezoActuator(Actuator):
    id = "piezo"
    name = "DRV517 piezo"
    controller = "Thorlabs BPC301"

    def connected(self) -> bool: return piezo.is_connected()
    def error(self) -> str: return piezo.connect_error()

    def axes(self) -> list[dict]:
        return [
            {"key": "position", "label": "Displacement (µm)", "unit": "µm",
             "min": 0.0, "max": round(piezo.max_travel_um(), 3), "kind": "position"},
            {"key": "voltage", "label": "Drive voltage (V)", "unit": "V",
             "min": 0.0, "max": round(piezo.max_voltage_v(), 3), "kind": "voltage"},
        ]

    def begin_scan(self) -> None: piezo.scan_begin("scanning")
    def end_scan(self) -> None: piezo.scan_end()

    def check_ready(self, axis_key: str) -> tuple[bool, str]:
        if axis_key == "position":
            return piezo.scan_check_position_ready()
        return True, ""

    def prepare(self, axis_key: str) -> None:
        if axis_key == "voltage":
            piezo.scan_enable_open_loop()

    def move(self, axis_key: str, setpoint: float, settle_s: float) -> float:
        if axis_key == "position":
            return piezo.scan_move_position(setpoint, settle_s=settle_s)
        return piezo.scan_set_voltage(setpoint, settle_s=settle_s)

    def ready_action_label(self, axis_key: str) -> str | None:
        return "Zero & close loop" if axis_key == "position" else None

    def start_ready_action(self) -> tuple[bool, str]:
        return piezo.start_auto_zero()

    def ready_action_state(self) -> dict:
        z = piezo.zero_state()
        return {"running": z.get("running", False),
                "ready": z.get("zeroed", False) and z.get("closed_loop", False),
                "message": z.get("message", ""), "error": z.get("error", "")}

    def status(self) -> dict:
        return piezo.scan_live_status()


# ---------------------------------------------------------------------------
# Newport SMC100 / LTA-HS adapter
# ---------------------------------------------------------------------------

class SMC100Actuator(Actuator):
    id = "smc100"
    name = "LTA-HS actuator"
    controller = "Newport SMC100CC"

    def connected(self) -> bool: return smc.is_connected()
    def error(self) -> str: return smc.connect_error()

    def axes(self) -> list[dict]: return smc.scan_axes()

    def begin_scan(self) -> None: smc.scan_begin("scanning")
    def end_scan(self) -> None: smc.scan_end()

    def check_ready(self, axis_key: str) -> tuple[bool, str]:
        return smc.scan_check_ready(axis_key)

    def prepare(self, axis_key: str) -> None: smc.scan_prepare(axis_key)

    def move(self, axis_key: str, setpoint: float, settle_s: float) -> float:
        return smc.scan_move_position(setpoint, settle_s=settle_s)

    def ready_action_label(self, axis_key: str) -> str | None:
        return "Home (reference)"

    def start_ready_action(self) -> tuple[bool, str]:
        return smc.start_home()

    def ready_action_state(self) -> dict:
        h = smc.home_state()
        return {"running": h.get("running", False), "ready": h.get("ready", False),
                "message": h.get("message", ""), "error": h.get("error", "")}

    def status(self) -> dict:
        return smc.scan_live_status()


# ---------------------------------------------------------------------------
# Manager — auto-detect + active selection
# ---------------------------------------------------------------------------

class ActuatorManager:
    def __init__(self):
        # Registered in priority order; the first *connected* one becomes active.
        self._actuators: list[Actuator] = [PiezoActuator(), SMC100Actuator()]
        self._active_id: str | None = None
        self._lock = threading.Lock()

    def detect(self) -> None:
        """Probe every backend (each handles its own connection failure)."""
        print("Detecting actuators…")
        try:
            piezo._init_controller()
        except Exception as exc:
            print(f"  piezo probe error: {exc}")
        try:
            smc._init_controller()
        except Exception as exc:
            print(f"  SMC100 probe error: {exc}")
        connected = [a for a in self._actuators if a.connected()]
        with self._lock:
            if self._active_id is None or not self._by_id(self._active_id).connected():
                self._active_id = connected[0].id if connected else None
        names = ", ".join(f"{a.name} ({a.controller})" for a in connected) or "none"
        print(f"Actuators connected: {names}  | active: {self._active_id}")

    def _by_id(self, aid: str) -> Actuator | None:
        return next((a for a in self._actuators if a.id == aid), None)

    def all(self) -> list[Actuator]:
        return list(self._actuators)

    def active(self) -> Actuator | None:
        with self._lock:
            return self._by_id(self._active_id) if self._active_id else None

    def set_active(self, aid: str) -> tuple[bool, str]:
        a = self._by_id(aid)
        if a is None:
            return False, f"Unknown actuator '{aid}'"
        if not a.connected():
            return False, f"{a.name} is not connected"
        with self._lock:
            self._active_id = aid
        return True, aid


# Single shared instance used by scanner.py.
manager = ActuatorManager()

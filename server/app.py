"""
BPC301 / DRV517 piezo controller — Flask blueprint.

This module exposes the piezo controller as a blueprint (`piezo_bp`, mounted at
/api/piezo/*) plus a small set of scan helpers used by the merged Scan app
(scanner.py). It can still be run standalone:  python app.py  → http://localhost:5001

Closed-loop notes (hard-won — see memory/bpc301-hardware-quirks):
  * The controller locks if anything talks to it over USB while it is zeroing or
    servoing a move. So while a closed-loop operation is in progress we set a
    global _busy flag and the status poller returns the last snapshot WITHOUT
    touching the controller. The Scan loop sets _busy too (via scan_begin), so the
    poller stays silent for the whole sweep.
  * Zeroing duration varies, so it is a two-step user-confirmed flow: zero/start
    issues SET_ZERO and goes silent; the user watches the front-panel 'Zeroed' LED;
    zero/confirm does a single status read and engages closed loop.
  * Moves hold the device lock through a short silent settle, then read once.
"""

import threading
import time

from flask import Blueprint, Flask, jsonify, render_template, request

from drivers.controller import DRV517, POS_RANGE_UM

piezo_bp = Blueprint("piezo", __name__)

# ---------------------------------------------------------------------------
# Global controller state
# ---------------------------------------------------------------------------

_drv: DRV517 | None = None
_drv_lock = threading.Lock()
_device_info = None
_max_travel_um = POS_RANGE_UM
_sweep_active = False
_sweep_thread: threading.Thread | None = None
_connect_error: str = ""

# _busy is set while a closed-loop operation (zeroing/moving/scanning) owns the
# USB link. The status poller checks it and returns _last_snapshot instead of
# reading.
_busy: str | None = None
_last_snapshot: dict = {}

# Auto-zero (no user confirm) progress, polled by the UI.
_zero_state: dict = {"running": False, "zeroed": False, "closed_loop": False,
                     "elapsed": 0.0, "message": "", "error": ""}
_zero_thread: threading.Thread | None = None


def _flush_usb_cache() -> None:
    """Flush pyftdi's USB device cache so a fresh open always works."""
    try:
        from pyftdi.usbtools import UsbTools
        UsbTools.flush_cache()
    except Exception:
        pass
    time.sleep(0.15)


def _init_controller() -> None:
    global _drv, _device_info, _connect_error, _max_travel_um, _busy
    _flush_usb_cache()
    _busy = None
    try:
        d = DRV517(max_voltage_v=75.0)
        info = d.get_hardware_info()
        d.enable_open_loop()
        try:
            t = d.get_max_travel()
            if t > 0:
                _max_travel_um = t
        except Exception:
            pass
        _drv = d
        _device_info = info
        _connect_error = ""
        print(f"Connected: {info.model.strip()}  S/N {info.serial_number}  "
              f"FW {info.fw_major}.{info.fw_interim}.{info.fw_minor}  "
              f"travel {_max_travel_um:.0f} µm")
    except Exception as exc:
        _connect_error = str(exc)
        print(f"Piezo connection failed: {exc}")


# ---------------------------------------------------------------------------
# Scan helpers — used by scanner.py to drive a coordinated step-and-measure sweep
# while respecting the USB timing rules (silent during moves, single read after).
# ---------------------------------------------------------------------------

def is_connected() -> bool:
    return _drv is not None


def connect_error() -> str:
    return _connect_error


def max_travel_um() -> float:
    return _max_travel_um


def max_voltage_v() -> float:
    return _drv._max_v if _drv is not None else 75.0


def scan_begin(tag: str = "scanning") -> None:
    """Take the USB link for a scan so the status poller stays silent."""
    global _busy
    _busy = tag


def scan_end() -> None:
    global _busy
    _busy = None


def scan_check_position_ready() -> tuple[bool, str]:
    """Single status read: is the piezo zeroed AND in closed-loop position mode?"""
    if _drv is None:
        return False, "Piezo not connected"
    with _drv_lock:
        st = _drv.get_status()
    if not st.zeroed:
        return False, "Piezo not zeroed — zero it on the Piezo page first"
    if not st.position_control_mode:
        return False, "Piezo not in closed-loop mode — zero it on the Piezo page first"
    return True, ""


def scan_move_position(pos_um: float, settle_s: float = 3.0) -> float:
    """
    Closed-loop move used by the scan loop. Holds the device lock through the
    silent settle (no USB chatter while servoing), then reads the settled
    position once. Returns the actual position in µm. Caller must hold the scan
    (_busy) lock via scan_begin().
    """
    if _drv is None:
        raise RuntimeError("Piezo not connected")
    pos_um = max(0.0, min(_max_travel_um, float(pos_um)))
    with _drv_lock:
        _drv.move_to(pos_um)
        _drv.wait_for_position(pos_um, settle_s=settle_s)
        return _drv.last_position_um


def scan_set_voltage(volts: float, settle_s: float = 0.5) -> float:
    """
    Open-loop voltage step used by the scan loop. Sets the voltage, waits the
    settle quietly, then reads it back once. Returns the read-back voltage (V).
    """
    if _drv is None:
        raise RuntimeError("Piezo not connected")
    volts = max(0.0, min(_drv._max_v, float(volts)))
    with _drv_lock:
        _drv.set_voltage(volts)
    time.sleep(settle_s)
    with _drv_lock:
        return _drv.get_voltage()


def scan_enable_open_loop() -> None:
    if _drv is None:
        raise RuntimeError("Piezo not connected")
    with _drv_lock:
        _drv.enable_open_loop()


# ---------------------------------------------------------------------------
# Routes — API (mounted at /api/piezo)
# ---------------------------------------------------------------------------

@piezo_bp.route("/info")
def api_info():
    if _drv is None:
        return jsonify({"connected": False, "error": _connect_error})
    info = _device_info
    return jsonify({
        "connected": True,
        "model": info.model.strip(),
        "serial": info.serial_number,
        "firmware": f"{info.fw_major}.{info.fw_interim}.{info.fw_minor}",
        "max_voltage": _drv._max_v,
        "max_travel": round(_max_travel_um, 1),
        "port": _drv._conn._ser.name,
        "channel": _drv._chan,
    })


@piezo_bp.route("/status")
def api_status():
    global _last_snapshot
    if _drv is None:
        return jsonify({"connected": False, "error": _connect_error})

    # While a closed-loop operation owns the link, do NOT touch the controller —
    # return the last good snapshot so the poller stays silent.
    if _busy is not None:
        snap = dict(_last_snapshot)
        snap["connected"] = True
        snap["busy"] = _busy
        return jsonify(snap)

    try:
        with _drv_lock:
            if _busy is not None:                 # set just as we acquired the lock
                snap = dict(_last_snapshot)
                snap["connected"] = True
                snap["busy"] = _busy
                return jsonify(snap)
            voltage = _drv.get_voltage()
            status = _drv.get_status()
            position = _drv.last_position_um
        snap = {
            "connected": True,
            "busy": None,
            "voltage": round(voltage, 3),
            "position": round(position, 4),
            "piezo_connected": status.piezo_connected,
            "strain_gauge_connected": status.strain_gauge_connected,
            "closed_loop": status.position_control_mode,
            "zeroed": status.zeroed,
            "zeroing": status.zeroing,
            "hw_max_voltage": status.hw_max_voltage,
            "sweep_active": _sweep_active,
        }
        _last_snapshot = snap
        return jsonify(snap)
    except TimeoutError:
        return jsonify({"connected": False,
                        "error": "Controller not responding — power-cycle required"})
    except Exception as exc:
        return jsonify({"connected": False, "error": str(exc)})


@piezo_bp.route("/voltage", methods=["POST"])
def api_set_voltage():
    if _drv is None:
        return jsonify({"error": "Not connected"}), 503
    if _busy is not None:
        return jsonify({"error": f"Busy ({_busy})"}), 409
    data = request.get_json(force=True)
    try:
        v = float(data["voltage"])
        with _drv_lock:
            _drv.set_voltage(v)
        return jsonify({"ok": True})
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400


@piezo_bp.route("/identify", methods=["POST"])
def api_identify():
    if _drv is None:
        return jsonify({"error": "Not connected"}), 503
    if _busy is not None:
        return jsonify({"error": f"Busy ({_busy})"}), 409
    with _drv_lock:
        _drv.identify()
    return jsonify({"ok": True})


@piezo_bp.route("/output", methods=["POST"])
def api_output():
    if _drv is None:
        return jsonify({"error": "Not connected"}), 503
    if _busy is not None:
        return jsonify({"error": f"Busy ({_busy})"}), 409
    data = request.get_json(force=True)
    enable = bool(data.get("enable", True))
    with _drv_lock:
        if enable:
            _drv.enable_output()
        else:
            _drv.disable_output()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Closed-loop position control
# ---------------------------------------------------------------------------

@piezo_bp.route("/mode/open", methods=["POST"])
def api_mode_open():
    """Leave closed loop and return to open-loop voltage control."""
    if _drv is None:
        return jsonify({"error": "Not connected"}), 503
    if _busy is not None:
        return jsonify({"error": f"Busy ({_busy})"}), 409
    with _drv_lock:
        _drv.enable_open_loop()
    return jsonify({"ok": True})


@piezo_bp.route("/zero/start", methods=["POST"])
def api_zero_start():
    """
    Begin zeroing. Issues SET_ZERO, then goes silent (sets _busy) so the poller
    stops touching the controller. The UI then asks the user to watch the
    'Zeroed' LED and call zero/confirm when it is solid.
    """
    global _busy
    if _drv is None:
        return jsonify({"error": "Not connected"}), 503
    if _sweep_active:
        return jsonify({"error": "Stop the sweep first"}), 409
    if _busy is not None:
        return jsonify({"error": f"Busy ({_busy})"}), 409
    _busy = "zeroing"
    try:
        with _drv_lock:
            _drv.start_zero()
    except Exception as exc:
        _busy = None
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True,
                    "message": "Zeroing — press Confirm when the 'Zeroed' LED is solid."})


@piezo_bp.route("/zero/confirm", methods=["POST"])
def api_zero_confirm():
    """Finish zeroing: one status read, engage closed loop, resume polling."""
    global _busy
    if _drv is None:
        return jsonify({"error": "Not connected"}), 503
    if _busy != "zeroing":
        return jsonify({"error": "Not zeroing"}), 409
    try:
        with _drv_lock:
            zeroed = _drv.finish_zero()
            status = _drv.get_status()
            if zeroed and not status.position_control_mode:
                _drv.enable_closed_loop()
                time.sleep(0.2)
                status = _drv.get_status()
            position = _drv.last_position_um
    finally:
        _busy = None
    return jsonify({"ok": True, "zeroed": zeroed,
                    "closed_loop": status.position_control_mode,
                    "position": round(position, 4)})


@piezo_bp.route("/zero/cancel", methods=["POST"])
def api_zero_cancel():
    """Abort a zeroing wait and resume normal polling (controller untouched)."""
    global _busy
    if _busy == "zeroing":
        _busy = None
    return jsonify({"ok": True})


def start_auto_zero() -> tuple[bool, str]:
    """
    Zero the strain gauge and engage closed loop with NO user confirmation.
    Runs on a background thread (holding _busy so the status poller stays
    silent); detects completion by slow single-threaded status polls. Progress
    is tracked in _zero_state. Returns (ok, message). Reusable by the merged Scan
    app's actuator abstraction as well as the /zero/auto route.
    """
    global _busy, _zero_thread
    if _drv is None:
        return False, "Not connected"
    if _busy is not None:
        return False, f"Busy ({_busy})"

    _busy = "zeroing"
    _zero_state.update({"running": True, "zeroed": False, "closed_loop": False,
                        "elapsed": 0.0, "error": "",
                        "message": "Zeroing — please wait (~15–25 s)…"})

    def _run():
        global _busy
        try:
            def _progress(elapsed, st):
                _zero_state["elapsed"] = round(elapsed, 1)
                _zero_state["message"] = (
                    f"Zeroing… {elapsed:.0f}s"
                    + (" (settling)" if st.zeroing else ""))
            with _drv_lock:
                closed = _drv.auto_zero(progress=_progress)
                st = _drv.get_status()
            _zero_state.update({
                "zeroed": st.zeroed,
                "closed_loop": closed,
                "message": ("Zeroed — closed loop engaged." if closed
                            else "Zeroing finished but closed loop not active."),
            })
        except Exception as exc:
            _zero_state["error"] = str(exc)
            _zero_state["message"] = f"Zeroing failed: {exc}"
        finally:
            _zero_state["running"] = False
            _busy = None

    _zero_thread = threading.Thread(target=_run, daemon=True, name="auto-zero")
    _zero_thread.start()
    return True, "Auto-zeroing started."


def zero_state() -> dict:
    """Current auto-zero progress dict (for the UI / actuator abstraction)."""
    return dict(_zero_state)


def scan_live_status() -> dict:
    """
    Actuator-agnostic status snapshot for the Scan UI. Respects _busy (returns
    the last snapshot without touching the controller while it is operating).
    """
    if _drv is None:
        return {"connected": False, "error": _connect_error}
    if _busy is not None:
        snap = dict(_last_snapshot)
        snap.update({"connected": True, "busy": _busy})
        return snap
    try:
        with _drv_lock:
            if _busy is not None:
                snap = dict(_last_snapshot)
                snap.update({"connected": True, "busy": _busy})
                return snap
            status = _drv.get_status()
            position = _drv.last_position_um
        return {"connected": True, "busy": None,
                "position": round(position, 4),
                "zeroed": status.zeroed,
                "closed_loop": status.position_control_mode,
                "ready": status.zeroed and status.position_control_mode}
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


@piezo_bp.route("/zero/auto", methods=["POST"])
def api_zero_auto():
    """Zero + engage closed loop, no user confirmation. Poll /zero/state."""
    if _drv is None:
        return jsonify({"error": "Not connected"}), 503
    ok, msg = start_auto_zero()
    if not ok:
        return jsonify({"error": msg}), 409
    return jsonify({"ok": True, "message": msg})


@piezo_bp.route("/zero/state")
def api_zero_state():
    """Progress of an auto-zero run (polled by the UI)."""
    return jsonify(_zero_state)


@piezo_bp.route("/move", methods=["POST"])
def api_move():
    """
    Closed-loop absolute move (µm). Holds the device lock through a short silent
    settle so nothing else talks to the controller while it servos, then reads
    the settled position once.
    """
    global _busy
    if _drv is None:
        return jsonify({"error": "Not connected"}), 503
    if _busy is not None:
        return jsonify({"error": f"Busy ({_busy})"}), 409
    data = request.get_json(force=True)
    try:
        pos = float(data["position"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    if not 0.0 <= pos <= _max_travel_um:
        return jsonify({"error": f"Position out of range 0–{_max_travel_um:.0f} µm"}), 400

    _busy = "moving"
    try:
        with _drv_lock:
            if not _drv.get_status().position_control_mode:
                return jsonify({"error": "Not in closed-loop mode — zero first"}), 409
            _drv.move_to(pos)
            reached = _drv.wait_for_position(pos, settle_s=3.0)
            actual = _drv.last_position_um
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        _busy = None
    return jsonify({"ok": True, "position": round(actual, 4), "reached": reached})


@piezo_bp.route("/reconnect", methods=["POST"])
def api_reconnect():
    global _drv, _device_info, _busy
    _busy = None
    if _drv is not None:
        try:
            _drv.close()
        except Exception:
            pass
        _drv = None
        _device_info = None
    _flush_usb_cache()
    _init_controller()
    return jsonify({"connected": _drv is not None, "error": _connect_error})


# ---------------------------------------------------------------------------
# Sweep (open-loop voltage)
# ---------------------------------------------------------------------------

@piezo_bp.route("/sweep/start", methods=["POST"])
def api_sweep_start():
    global _sweep_active, _sweep_thread
    if _drv is None:
        return jsonify({"error": "Not connected"}), 503
    if _busy is not None:
        return jsonify({"error": f"Busy ({_busy})"}), 409
    if _sweep_active:
        return jsonify({"error": "Sweep already running"}), 409

    data = request.get_json(force=True)
    start_v  = float(data.get("start",  0.0))
    stop_v   = float(data.get("stop",   _drv._max_v))
    steps    = max(2, int(data.get("steps",  50)))
    dwell    = max(0.01, float(data.get("dwell", 0.1)))
    repeat   = bool(data.get("repeat", False))

    _sweep_active = True

    def _run():
        global _sweep_active
        try:
            voltages = [
                start_v + (stop_v - start_v) * i / (steps - 1)
                for i in range(steps)
            ]
            while _sweep_active:
                for v in voltages:
                    if not _sweep_active:
                        break
                    with _drv_lock:
                        _drv.set_voltage(v)
                    time.sleep(dwell)
                if not repeat:
                    break
        finally:
            _sweep_active = False

    _sweep_thread = threading.Thread(target=_run, daemon=True, name="sweep")
    _sweep_thread.start()
    return jsonify({"ok": True})


@piezo_bp.route("/sweep/stop", methods=["POST"])
def api_sweep_stop():
    global _sweep_active
    _sweep_active = False
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Standalone app (python app.py) — same prefixes as the merged app
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(piezo_bp, url_prefix="/api/piezo")

    @app.route("/")
    def index():
        return render_template("index.html")

    return app


if __name__ == "__main__":
    _init_controller()
    create_app().run(host="0.0.0.0", port=5001, threaded=True, debug=False)

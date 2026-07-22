"""
Newport SMC100CC / LTA-HS motion controller — Flask blueprint + web UI.

Exposes the SMC100 driver as a blueprint (`smc_bp`, mounted at /api/smc/*) and
serves a control page. Can be run standalone:

    python -m server.smc100_app    →  http://localhost:5003

All instrument I/O is serialized behind the driver's lock. Unlike the piezo
APT link, the SMC100's RS-232 protocol is robust to continuous polling, and
moves are non-blocking (PA/PR return immediately; TS reports MOVING until the
stage settles), so the status poller can read the controller directly. A short
status cache keeps the poll rate off the serial line.
"""

import threading
import time

from flask import Blueprint, Flask, jsonify, render_template, request

from drivers.smc100 import SMC100, LTA_HS, SMCState

smc_bp = Blueprint("smc", __name__)

# ---------------------------------------------------------------------------
# Global controller state
# ---------------------------------------------------------------------------

_smc: SMC100 | None = None
_smc_lock = threading.Lock()
_connect_error: str = ""
_profile = LTA_HS
_limits = (0.0, LTA_HS.travel_mm)
_firmware = ""
_stage_id = ""

# Light status cache so a fast UI poll doesn't hammer the serial line.
_snap: dict = {}
_snap_ts: float = 0.0
_SNAP_TTL = 0.15   # seconds

# Scan integration (used by the merged scanner app's actuator abstraction).
_scan_busy = False
_home_state: dict = {"running": False, "ready": False, "error": "", "message": ""}


def _init_controller() -> None:
    global _smc, _connect_error, _limits, _firmware, _stage_id
    try:
        s = SMC100(profile=_profile)
        _firmware = s.get_firmware()
        _stage_id = s.get_stage_id()
        try:
            _limits = s.get_limits()
        except Exception:
            _limits = (0.0, _profile.travel_mm)
        _smc = s
        _connect_error = ""
        print(f"Connected: SMC100 on {s.port} (addr {s.address})  FW {_firmware}  "
              f"stage '{_stage_id or _profile.name}'  limits {_limits} mm")
    except Exception as exc:
        _connect_error = str(exc)
        print(f"SMC100 connection failed: {exc}")


# ---------------------------------------------------------------------------
# Scan helpers — used by the merged scanner app (actuator abstraction)
# ---------------------------------------------------------------------------

def is_connected() -> bool:
    return _smc is not None


def connect_error() -> str:
    return _connect_error


def profile():
    return _profile


def limits_mm() -> tuple[float, float]:
    return _limits


def scan_axes() -> list[dict]:
    lo, hi = _limits
    return [{"key": "position", "label": "Position (mm)", "unit": "mm",
             "min": round(lo, 4), "max": round(hi, 4), "kind": "position"}]


def scan_begin(_tag: str = "scanning") -> None:
    global _scan_busy
    _scan_busy = True


def scan_end() -> None:
    global _scan_busy
    _scan_busy = False


def scan_check_ready(_axis_key: str = "position") -> tuple[bool, str]:
    if _smc is None:
        return False, "SMC100 not connected"
    with _smc_lock:
        st = _smc.get_status()
    if not st.is_referenced:
        return False, "Stage not referenced — Home it first"
    return True, ""


def scan_prepare(_axis_key: str = "position") -> None:
    """Ensure the axis is enabled (leave DISABLE) before a scan."""
    if _smc is None:
        return
    with _smc_lock:
        if _smc.get_status().state == SMCState.DISABLE:
            _smc.set_enabled(True)
            time.sleep(0.2)


def scan_move_position(mm: float, settle_s: float = 0.0) -> float:
    """
    Absolute move used by the scan loop. Commands the move, waits for the
    controller to return to READY (releasing the lock between polls so the UI
    stays live), applies an extra settle, then reads the position once.
    """
    if _smc is None:
        raise RuntimeError("SMC100 not connected")
    with _smc_lock:
        if _smc.get_status().state == SMCState.DISABLE:
            _smc.set_enabled(True)
            time.sleep(0.2)
        _smc.move_absolute(mm)
    _smc.wait_until_ready(timeout_s=120.0, poll_s=0.15)
    if settle_s > 0:
        time.sleep(settle_s)
    with _smc_lock:
        return _smc.get_position()


def start_home() -> tuple[bool, str]:
    """Begin a home search on a background thread; poll home_state() for done."""
    global _home_state
    if _smc is None:
        return False, "SMC100 not connected"
    _home_state = {"running": True, "ready": False, "error": "",
                   "message": "Home search in progress…"}

    def _run():
        try:
            with _smc_lock:
                if _smc.get_status().state == SMCState.DISABLE:
                    _smc.set_enabled(True)
                    time.sleep(0.2)
                _smc.home()
            ok = _smc.wait_until_ready(timeout_s=120.0, poll_s=0.2)
            _home_state.update({"ready": ok,
                                "message": "Homed — ready." if ok
                                else "Home search did not reach READY."})
        except Exception as exc:
            _home_state.update({"error": str(exc), "message": f"Home failed: {exc}"})
        finally:
            _home_state["running"] = False

    threading.Thread(target=_run, daemon=True, name="smc-home").start()
    return True, "Home search started…"


def home_state() -> dict:
    return dict(_home_state)


def scan_live_status() -> dict:
    """Actuator-agnostic status snapshot for the Scan UI."""
    if _smc is None:
        return {"connected": False, "error": _connect_error}
    try:
        with _smc_lock:
            st = _smc.get_status()
            pos = _smc.get_position()
        return {"connected": True, "busy": st.is_moving or _scan_busy,
                "position": round(pos, 5), "state": st.state.value,
                "referenced": st.is_referenced, "ready": st.is_referenced}
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Routes — API (mounted at /api/smc)
# ---------------------------------------------------------------------------

@smc_bp.route("/info")
def api_info():
    if _smc is None:
        return jsonify({"connected": False, "error": _connect_error,
                        "profile": _profile.__dict__})
    return jsonify({
        "connected": True,
        "port": _smc.port,
        "address": _smc.address,
        "firmware": _firmware,
        "stage_id": _stage_id,
        "limits_mm": {"min": round(_limits[0], 4), "max": round(_limits[1], 4)},
        "profile": _profile.__dict__,
    })


@smc_bp.route("/status")
def api_status():
    global _snap, _snap_ts
    if _smc is None:
        return jsonify({"connected": False, "error": _connect_error})

    if time.monotonic() - _snap_ts < _SNAP_TTL and _snap:
        return jsonify(_snap)

    try:
        with _smc_lock:
            st = _smc.get_status()
            pos = _smc.get_position()
            target = _smc.get_target()
            vel = _smc.get_velocity()
        lo, hi = _limits
        frac = (pos - lo) / (hi - lo) if hi > lo else 0.0
        _snap = {
            "connected": True,
            "state": st.state.value,
            "state_code": st.state_code,
            "moving": st.is_moving,
            "referenced": st.is_referenced,
            "ready": st.is_ready,
            "error_bits": st.error_bits,
            "position": round(pos, 5),
            "target": round(target, 5),
            "velocity": round(vel, 5),
            "limits": {"min": round(lo, 4), "max": round(hi, 4)},
            "fraction": max(0.0, min(1.0, frac)),
        }
        _snap_ts = time.monotonic()
        return jsonify(_snap)
    except TimeoutError:
        return jsonify({"connected": False,
                        "error": "Controller not responding — check cable/power"})
    except Exception as exc:
        return jsonify({"connected": False, "error": str(exc)})


# Optional external gate: the merged Scan app sets this to a callable that
# returns a message while a scan owns the stage (so manual moves are refused).
external_busy = None


def _busy_error():
    """Reject motion commands while the stage is already moving/homing,
    or while an external owner (the scan engine) holds the stage."""
    if external_busy is not None:
        msg = external_busy()
        if msg:
            return jsonify({"error": msg}), 409
    try:
        with _smc_lock:
            st = _smc.get_status()
        if st.is_moving:
            return jsonify({"error": f"Busy ({st.state.value})"}), 409
    except Exception:
        pass
    return None


def _move_result(letter: str, desc: str, after, vel=None):
    """
    Build the JSON response for a move. PA/PR are write-only (the controller
    sends no reply to them), so we judge the move by the TE letter queried right
    after: '@' = accepted, '?' = TE didn't answer (treat as sent), any other
    letter = the controller rejected it (surface why).
    """
    if letter not in ("@", "?"):
        return jsonify({"ok": False, "te": letter, "error": desc,
                        "state": after.state.value}), 400
    warning = None
    if vel is not None and vel <= 1e-9:
        warning = "Velocity is 0 mm/s — set a velocity (e.g. 2) and the stage will move"
    return jsonify({"ok": True, "te": letter, "velocity": vel, "warning": warning,
                    "state": after.state.value, "moving": after.is_moving})


@smc_bp.route("/move", methods=["POST"])
def api_move():
    """Absolute move to a position in mm."""
    if _smc is None:
        return jsonify({"error": "Not connected"}), 503
    data = request.get_json(force=True)
    try:
        pos = float(data["position"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    busy = _busy_error()
    if busy:
        return busy
    try:
        with _smc_lock:
            st = _smc.get_status()
            if not st.is_referenced:
                return jsonify({"error": f"Not referenced (state {st.state.value})"
                                         " — run Home first"}), 409
            if st.state == SMCState.DISABLE:
                _smc.set_enabled(True)   # leave DISABLE → READY before moving
                time.sleep(0.2)
            vel = _smc.get_velocity()
            _smc.move_absolute(pos)
            time.sleep(0.08)             # let the controller register before TE
            letter, desc = _smc.get_last_error()
            after = _smc.get_status()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return _move_result(letter, desc, after, vel)


@smc_bp.route("/move/relative", methods=["POST"])
def api_move_relative():
    """Relative move (jog) by delta mm."""
    if _smc is None:
        return jsonify({"error": "Not connected"}), 503
    data = request.get_json(force=True)
    try:
        delta = float(data["delta"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    busy = _busy_error()
    if busy:
        return busy
    try:
        with _smc_lock:
            st = _smc.get_status()
            if not st.is_referenced:
                return jsonify({"error": f"Not referenced (state {st.state.value})"
                                         " — run Home first"}), 409
            if st.state == SMCState.DISABLE:
                _smc.set_enabled(True)   # leave DISABLE → READY before moving
                time.sleep(0.2)
            vel = _smc.get_velocity()
            _smc.move_relative(delta)
            time.sleep(0.08)             # let the controller register before TE
            letter, desc = _smc.get_last_error()
            after = _smc.get_status()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return _move_result(letter, desc, after, vel)


@smc_bp.route("/home", methods=["POST"])
def api_home():
    """Execute a home search (OR). Non-blocking; poll status for READY."""
    if _smc is None:
        return jsonify({"error": "Not connected"}), 503
    busy = _busy_error()
    if busy:
        return busy
    try:
        with _smc_lock:
            st = _smc.get_status()
            if st.state == SMCState.DISABLE:
                _smc.set_enabled(True)
                time.sleep(0.2)
            _smc.home()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True, "message": "Home search started…"})


@smc_bp.route("/stop", methods=["POST"])
def api_stop():
    if _smc is None:
        return jsonify({"error": "Not connected"}), 503
    with _smc_lock:
        _smc.stop()
    return jsonify({"ok": True})


@smc_bp.route("/velocity", methods=["POST"])
def api_velocity():
    if _smc is None:
        return jsonify({"error": "Not connected"}), 503
    try:
        v = float(request.get_json(force=True)["velocity"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    with _smc_lock:
        _smc.set_velocity(v)
        try:
            actual = _smc.get_velocity()   # _write already settled; readback to confirm
        except TimeoutError:
            actual = v                     # set almost certainly took; don't fail on readback
    return jsonify({"ok": True, "velocity": round(actual, 5)})


@smc_bp.route("/enable", methods=["POST"])
def api_enable():
    if _smc is None:
        return jsonify({"error": "Not connected"}), 503
    enable = bool(request.get_json(force=True).get("enable", True))
    with _smc_lock:
        _smc.set_enabled(enable)
    return jsonify({"ok": True})


@smc_bp.route("/reset", methods=["POST"])
def api_reset():
    if _smc is None:
        return jsonify({"error": "Not connected"}), 503
    with _smc_lock:
        _smc.reset()
    return jsonify({"ok": True, "message": "Controller reset — re-home required."})


@smc_bp.route("/reconnect", methods=["POST"])
def api_reconnect():
    global _smc, _snap, _snap_ts
    if _smc is not None:
        try:
            _smc.close()
        except Exception:
            pass
        _smc = None
    _snap, _snap_ts = {}, 0.0
    _init_controller()
    return jsonify({"connected": _smc is not None, "error": _connect_error})


# ---------------------------------------------------------------------------
# Standalone app
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(smc_bp, url_prefix="/api/smc")

    @app.route("/")
    def index():
        return render_template("smc100.html")

    return app


if __name__ == "__main__":
    _init_controller()
    create_app().run(host="0.0.0.0", port=5003, threaded=True, debug=False)

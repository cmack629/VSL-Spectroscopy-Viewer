"""
Thorlabs PM400 optical power monitor — Flask blueprint.

Exposes the power monitor as a blueprint (`power_bp`, mounted at /api/power/*)
plus scan helpers (collect_average / latest_power_w) used by the merged Scan app
(scanner.py). Can still be run standalone:  python power_monitor.py
→ http://localhost:5002

A self-contained re-implementation of Thorlabs' "Optical Power Monitor"
desktop app: live auto-ranging power readout, wavelength correction, sensor
info, running statistics (min/max/avg/std), a real-time strip chart, dark
(zero) adjustment, averaging / range control, and CSV data logging.

Architecture
------------
A single background sampler thread owns the PM400 and pushes Measurement
snapshots into a ring buffer at the configured rate. Flask request handlers
NEVER touch the instrument directly for reads — they read the latest snapshot
from the buffer. Control commands (wavelength, averaging, zero…) are queued to
the sampler thread so all VISA I/O stays on one thread, which keeps USBTMC happy.
The scan loop reuses that same buffer to average power at each actuator step.
"""

import csv
import io
import statistics
import threading
import time
from collections import deque

from flask import Blueprint, Flask, jsonify, render_template, request, Response

from drivers.pm400 import PM400, Measurement

power_bp = Blueprint("power", __name__)

# ---------------------------------------------------------------------------
# Global monitor state
# ---------------------------------------------------------------------------

_pm: PM400 | None = None
_connect_error: str = ""
_sensor_info = None

# Ring buffer of recent samples for the chart: deque[(t_epoch, power_w)]
_CHART_SECONDS = 120
_samples: deque = deque(maxlen=20000)
_samples_lock = threading.Lock()

# Latest snapshot dict served to the UI poller.
_latest: dict = {"connected": False}

# Running statistics over the current session (reset on demand).
_stats_vals: deque = deque(maxlen=100000)
_stats_lock = threading.Lock()

# Command queue executed on the sampler thread (keeps all VISA I/O single-thread).
_cmd_queue: deque = deque()
_cmd_lock = threading.Lock()

# Sampler control
_sampler_thread: threading.Thread | None = None
_sampler_stop = threading.Event()
_sample_interval = 0.1   # seconds between reads (10 Hz default)

# CSV logging
_log_rows: list[tuple] = []
_logging = False
_log_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Sampler thread — the only thing that talks to the instrument
# ---------------------------------------------------------------------------

def _drain_commands() -> None:
    """Run any queued control commands on this (sampler) thread."""
    while True:
        with _cmd_lock:
            if not _cmd_queue:
                return
            fn = _cmd_queue.popleft()
        try:
            fn(_pm)
        except Exception as exc:
            global _latest
            _latest = {**_latest, "cmd_error": str(exc)}


def _queue_cmd(fn) -> None:
    with _cmd_lock:
        _cmd_queue.append(fn)


def _sampler_loop() -> None:
    global _latest
    while not _sampler_stop.is_set():
        t0 = time.monotonic()
        _drain_commands()
        try:
            m: Measurement = _pm.read()
        except Exception as exc:
            _latest = {"connected": False, "error": str(exc)}
            time.sleep(0.5)
            continue

        val, unit = PM400.format_power(m.power_w)
        with _samples_lock:
            _samples.append((m.timestamp, m.power_w))
            cutoff = m.timestamp - _CHART_SECONDS
            while _samples and _samples[0][0] < cutoff:
                _samples.popleft()
        with _stats_lock:
            _stats_vals.append(m.power_w)
        with _log_lock:
            if _logging:
                _log_rows.append((m.timestamp, m.power_w, m.power_dbm,
                                  m.current_a, m.wavelength_nm))

        _latest = {
            "connected": True,
            "power_w": m.power_w,
            "power_val": round(val, 4),
            "power_unit": unit,
            "power_dbm": None if m.power_dbm == float("-inf") else round(m.power_dbm, 3),
            "current_a": m.current_a,
            "wavelength": round(m.wavelength_nm, 1),
            "range_w": m.power_range_w,
            "auto_range": m.auto_range,
            "overrange": m.overrange,
            "logging": _logging,
            "timestamp": m.timestamp,
        }

        dt = time.monotonic() - t0
        sleep = _sample_interval - dt
        if sleep > 0:
            _sampler_stop.wait(sleep)


def _init_monitor() -> None:
    global _pm, _connect_error, _sensor_info, _sampler_thread
    try:
        pm = PM400()
        _pm = pm
        _sensor_info = pm.get_sensor_info()
        _connect_error = ""
        print(f"Connected: {pm.model}  S/N {pm.serial}  FW {pm.firmware}")
        print(f"Sensor:    {_sensor_info.name}  S/N {_sensor_info.serial}  "
              f"({_sensor_info.type_name})  cal {_sensor_info.cal_date}")
    except Exception as exc:
        _connect_error = str(exc)
        print(f"Power monitor connection failed: {exc}")
        return

    _sampler_stop.clear()
    _sampler_thread = threading.Thread(target=_sampler_loop, daemon=True,
                                       name="pm400-sampler")
    _sampler_thread.start()


# ---------------------------------------------------------------------------
# Scan helpers — used by scanner.py to read averaged power at each step
# ---------------------------------------------------------------------------

def is_connected() -> bool:
    return _pm is not None


def connect_error() -> str:
    return _connect_error


def latest_power_w():
    """Most recent instantaneous power in watts (or None)."""
    return _latest.get("power_w") if _latest.get("connected") else None


def collect_average(dwell_s: float = 0.3) -> dict | None:
    """
    Average the sampler ring buffer over a fresh dwell window. Records a start
    time, waits dwell_s while the sampler keeps filling the buffer, then averages
    every sample taken after the start time. Falls back to the latest single
    reading if the dwell was too short to capture a new sample.

    Returns {mean_w, std_w, min_w, max_w, n} or None if no power is available.
    """
    t0 = time.time()
    if dwell_s > 0:
        time.sleep(dwell_s)
    with _samples_lock:
        vals = [p for (t, p) in _samples if t >= t0]
    if not vals:
        p = latest_power_w()
        if p is None:
            return None
        vals = [p]
    n = len(vals)
    return {
        "mean_w": statistics.fmean(vals),
        "std_w": statistics.pstdev(vals) if n > 1 else 0.0,
        "min_w": min(vals),
        "max_w": max(vals),
        "n": n,
    }


# ---------------------------------------------------------------------------
# Routes — API (mounted at /api/power)
# ---------------------------------------------------------------------------

@power_bp.route("/info")
def api_info():
    if _pm is None:
        return jsonify({"connected": False, "error": _connect_error})
    s = _sensor_info
    try:
        lo, hi = _pm.wavelength_range
        avg = _pm.averaging
    except Exception:
        lo, hi, avg = 0, 0, 1
    return jsonify({
        "connected": True,
        "model": _pm.model,
        "serial": _pm.serial,
        "firmware": _pm.firmware,
        "resource": _pm._resource_name,
        "sensor": {
            "name": s.name, "serial": s.serial, "cal_date": s.cal_date,
            "type": s.type_name, "connected": s.connected,
        },
        "wavelength": _pm.wavelength,
        "wl_min": lo, "wl_max": hi,
        "averaging": avg,
        "auto_range": _pm.auto_range,
        "sample_rate_hz": round(1.0 / _sample_interval, 2),
    })


@power_bp.route("/status")
def api_status():
    if _pm is None:
        return jsonify({"connected": False, "error": _connect_error})
    return jsonify(_latest)


@power_bp.route("/chart")
def api_chart():
    """Return the chart ring buffer as parallel arrays [t_ms], [power_w]."""
    with _samples_lock:
        pts = list(_samples)
    return jsonify({
        "t":   [round(t * 1000) for t, _ in pts],
        "p":   [p for _, p in pts],
        "span_s": _CHART_SECONDS,
    })


@power_bp.route("/plot")
def api_plot():
    """Download the strip chart as a matplotlib figure (?format=png|svg|pdf)."""
    from server import plots
    if not plots.MPL_OK:
        return plots.unavailable()
    with _samples_lock:
        pts = list(_samples)
    if len(pts) < 2:
        return jsonify({"error": "No chart data yet"}), 404
    wl = None
    try:
        if _pm is not None:
            wl = _pm.wavelength
    except Exception:
        pass
    fig = plots.power_figure([t * 1000 for t, _ in pts], [p for _, p in pts],
                             _CHART_SECONDS, wavelength_nm=wl)
    return plots.respond(fig, "power")


@power_bp.route("/stats")
def api_stats():
    with _stats_lock:
        vals = list(_stats_vals)
    if not vals:
        return jsonify({"n": 0})
    n = len(vals)
    mean = statistics.fmean(vals)
    return jsonify({
        "n": n,
        "min": min(vals),
        "max": max(vals),
        "mean": mean,
        "std": statistics.pstdev(vals) if n > 1 else 0.0,
        "last": vals[-1],
    })


@power_bp.route("/stats/reset", methods=["POST"])
def api_stats_reset():
    with _stats_lock:
        _stats_vals.clear()
    return jsonify({"ok": True})


@power_bp.route("/wavelength", methods=["POST"])
def api_wavelength():
    if _pm is None:
        return jsonify({"error": "Not connected"}), 503
    try:
        nm = float(request.get_json(force=True)["wavelength"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    _queue_cmd(lambda pm: setattr(pm, "wavelength", nm))
    return jsonify({"ok": True})


@power_bp.route("/averaging", methods=["POST"])
def api_averaging():
    if _pm is None:
        return jsonify({"error": "Not connected"}), 503
    try:
        n = int(request.get_json(force=True)["count"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    _queue_cmd(lambda pm: setattr(pm, "averaging", n))
    return jsonify({"ok": True})


@power_bp.route("/range", methods=["POST"])
def api_range():
    if _pm is None:
        return jsonify({"error": "Not connected"}), 503
    data = request.get_json(force=True)
    if "auto" in data:
        auto = bool(data["auto"])
        _queue_cmd(lambda pm: setattr(pm, "auto_range", auto))
    elif "upper" in data:
        upper = float(data["upper"])
        _queue_cmd(lambda pm: setattr(pm, "power_range", upper))
    return jsonify({"ok": True})


@power_bp.route("/zero", methods=["POST"])
def api_zero():
    """Dark/zero adjustment. Block the beam first."""
    if _pm is None:
        return jsonify({"error": "Not connected"}), 503
    result = {}
    done = threading.Event()

    def _do(pm):
        try:
            result["magnitude"] = pm.zero()
        except Exception as exc:
            result["error"] = str(exc)
        finally:
            done.set()

    _queue_cmd(_do)
    done.wait(timeout=5.0)
    if "error" in result:
        return jsonify({"error": result["error"]}), 500
    return jsonify({"ok": True, "magnitude": result.get("magnitude")})


@power_bp.route("/zero/clear", methods=["POST"])
def api_zero_clear():
    if _pm is None:
        return jsonify({"error": "Not connected"}), 503
    _queue_cmd(lambda pm: pm.clear_zero())
    return jsonify({"ok": True})


@power_bp.route("/rate", methods=["POST"])
def api_rate():
    """Set the sampler interval from a target rate in Hz."""
    global _sample_interval
    try:
        hz = float(request.get_json(force=True)["hz"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    hz = max(0.5, min(20.0, hz))
    _sample_interval = 1.0 / hz
    return jsonify({"ok": True, "hz": round(hz, 2)})


# ---------------------------------------------------------------------------
# CSV logging
# ---------------------------------------------------------------------------

@power_bp.route("/log/start", methods=["POST"])
def api_log_start():
    global _logging
    with _log_lock:
        _log_rows.clear()
        _logging = True
    return jsonify({"ok": True})


@power_bp.route("/log/stop", methods=["POST"])
def api_log_stop():
    global _logging
    with _log_lock:
        _logging = False
        n = len(_log_rows)
    return jsonify({"ok": True, "rows": n})


@power_bp.route("/log/download")
def api_log_download():
    with _log_lock:
        rows = list(_log_rows)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp_iso", "epoch_s", "power_W", "power_dBm",
                "current_A", "wavelength_nm"])
    for ts, p_w, p_dbm, cur, wl in rows:
        iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        iso += f".{int((ts % 1) * 1000):03d}"
        w.writerow([iso, f"{ts:.3f}", f"{p_w:.9e}",
                    "" if p_dbm == float("-inf") else f"{p_dbm:.4f}",
                    "" if cur is None else f"{cur:.9e}", f"{wl:.1f}"])
    fname = time.strftime("pm400_log_%Y%m%d_%H%M%S.csv")
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@power_bp.route("/export")
def api_export():
    """Download the power log and available strip-chart plot in one ZIP."""
    from server import plots
    with _samples_lock:
        pts = list(_samples)
    files = {"power_log.csv": api_log_download().get_data(as_text=True)}
    if plots.MPL_OK and len(pts) >= 2:
        wavelength = None
        try:
            if _pm is not None:
                wavelength = _pm.wavelength
        except Exception:
            pass
        fig = plots.power_figure([t * 1000 for t, _ in pts], [p for _, p in pts],
                                 _CHART_SECONDS, wavelength_nm=wavelength)
        files["power_plot.png"] = plots.figure_bytes(fig)
    return plots.zip_response(files, "pm400_export")


@power_bp.route("/reconnect", methods=["POST"])
def api_reconnect():
    global _pm
    _sampler_stop.set()
    if _sampler_thread is not None:
        _sampler_thread.join(timeout=2.0)
    if _pm is not None:
        try:
            _pm.close()
        except Exception:
            pass
        _pm = None
    _init_monitor()
    return jsonify({"connected": _pm is not None, "error": _connect_error})


# ---------------------------------------------------------------------------
# Standalone app (python power_monitor.py) — same prefixes as the merged app
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(power_bp, url_prefix="/api/power")

    @app.route("/")
    def index():
        return render_template("power_monitor.html")

    return app


if __name__ == "__main__":
    _init_monitor()
    create_app().run(host="0.0.0.0", port=5002, threaded=True, debug=False)

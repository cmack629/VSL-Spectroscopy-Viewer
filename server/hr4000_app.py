"""
Ocean Optics HR4000CG-UV-NIR spectrometer — Flask blueprint + basic viewer.

(Works with any HR4000; the "CG-UV-NIR" composite-grating variant just spans
~200–1100 nm at coarser resolution — all read from the unit's calibration.)

A standalone web app to observe live spectra, mirroring the PM400 power monitor
(server/power_monitor.py): a single background sampler thread owns the
spectrometer and pushes the latest spectrum into a snapshot; Flask handlers read
that snapshot and queue control commands (integration time, averaging, boxcar,
dark) onto the sampler thread so all USB I/O stays single-threaded.

This is a STARTER app and is intentionally NOT wired into the merged scanner
(scanner.py) — run it on its own:

    python -m server.hr4000_app    →  http://localhost:5004
"""

import csv
import io
import threading
import time

from flask import Blueprint, Flask, jsonify, render_template, request, Response

from drivers.hr4000 import HR4000

hr_bp = Blueprint("hr4000", __name__)

# ---------------------------------------------------------------------------
# Global spectrometer state
# ---------------------------------------------------------------------------

_spec: HR4000 | None = None
_connect_error: str = ""
_wavelengths: list = []

# Latest spectrum snapshot served to the UI (intensities + stats, no wavelengths
# — those are static and fetched once from /info).
_latest: dict = {"connected": False}
_latest_lock = threading.Lock()

# Control commands run on the sampler thread (keeps all USB I/O single-thread).
_cmd_queue: list = []
_cmd_lock = threading.Lock()

_sampler_thread: threading.Thread | None = None
_sampler_stop = threading.Event()


def _queue_cmd(fn) -> None:
    with _cmd_lock:
        _cmd_queue.append(fn)


def _drain_commands() -> None:
    while True:
        with _cmd_lock:
            if not _cmd_queue:
                return
            fn = _cmd_queue.pop(0)
        try:
            fn(_spec)
        except Exception as exc:
            with _latest_lock:
                _latest["cmd_error"] = str(exc)


def _sampler_loop() -> None:
    global _latest
    while not _sampler_stop.is_set():
        _drain_commands()
        try:
            s = _spec.read()
        except Exception as exc:
            with _latest_lock:
                _latest = {"connected": False, "error": str(exc)}
            _sampler_stop.wait(0.5)
            continue
        with _latest_lock:
            _latest = {
                "connected": True,
                "intensities": s.intensities,
                "integration_time_us": s.integration_time_us,
                "n_averaged": s.n_averaged,
                "boxcar": s.boxcar,
                "peak_wavelength": round(s.peak_wavelength, 3),
                "peak_intensity": round(s.peak_intensity, 1),
                "total_counts": s.total_counts,
                "max_intensity": s.max_intensity,
                "saturated": s.saturated,
                "pixels": s.pixels,
                "timestamp": s.timestamp,
            }
        # Yield briefly; acquisition itself paces the loop via integration time.
        _sampler_stop.wait(0.01)


def _init_spectrometer() -> None:
    global _spec, _connect_error, _wavelengths, _sampler_thread
    try:
        sp = HR4000()
        _spec = sp
        _wavelengths = sp.wavelengths
        _connect_error = ""
        print(f"Connected: {sp.model}  S/N {sp.serial}  {sp.pixels} px  "
              f"{_wavelengths[0]:.1f}-{_wavelengths[-1]:.1f} nm")
    except Exception as exc:
        _connect_error = str(exc)
        print(f"HR4000 connection failed: {exc}")
        return

    _sampler_stop.clear()
    _sampler_thread = threading.Thread(target=_sampler_loop, daemon=True,
                                       name="hr4000-sampler")
    _sampler_thread.start()


# ---------------------------------------------------------------------------
# Scan-detector helpers — used by the merged scanner (detector abstraction).
# A spectrometer contributes a scalar metric (peak / integrated / intensity@λ)
# per scan step, plus the full spectrum for optional capture.
# ---------------------------------------------------------------------------

def is_connected() -> bool:
    return _spec is not None


def connect_error() -> str:
    return _connect_error


def wavelengths() -> list:
    return list(_wavelengths)


def _trapz_area(y, wl, lo=None, hi=None) -> float:
    """Trapezoidal integral ∫ y dλ over pixels whose λ is within [lo, hi]."""
    total = 0.0
    n = min(len(y), len(wl))
    for i in range(n - 1):
        x0, x1 = wl[i], wl[i + 1]
        if lo is not None and (x0 < lo or x1 < lo):
            continue
        if hi is not None and (x0 > hi or x1 > hi):
            continue
        total += (x1 - x0) * (y[i] + y[i + 1]) * 0.5
    return total


def _metric_value(d: dict, metric: str, wl_target):
    inten = d.get("intensities")
    if not inten:
        return None
    if metric in ("area", "total"):                 # integrated area over all λ
        return _trapz_area(inten, _wavelengths)
    if metric == "at_wavelength" and wl_target is not None and _wavelengths:
        idx = min(range(len(_wavelengths)),
                  key=lambda i: abs(_wavelengths[i] - wl_target))
        return inten[idx] if idx < len(inten) else None
    return d.get("peak_intensity", max(inten))      # peak


def latest_metric(metric: str = "peak", wl_target=None):
    with _latest_lock:
        d = dict(_latest)
    if not d.get("connected"):
        return None
    return _metric_value(d, metric, wl_target)


def collect_metric(metric: str = "peak", wl_target=None, dwell_s: float = 0.3) -> dict | None:
    """
    Wait dwell_s, then take the first spectrum acquired AT/AFTER the dwell start
    (so it reflects the settled actuator position), and reduce it to the chosen
    scalar metric. Returns {value, unit, std, n, spectrum, peak_wavelength}.
    """
    t0 = time.time()
    if dwell_s > 0:
        time.sleep(dwell_s)
    d = None
    deadline = time.time() + 5.0
    while time.time() < deadline:
        with _latest_lock:
            snap = dict(_latest)
        if snap.get("connected") and snap.get("timestamp", 0) >= t0:
            d = snap
            break
        time.sleep(0.01)
    if d is None:
        with _latest_lock:
            d = dict(_latest)
    if not d.get("connected"):
        return None
    val = _metric_value(d, metric, wl_target)
    if val is None:
        return None
    return {"value": float(val), "unit": "counts", "std": 0.0, "n": 1,
            "spectrum": list(d.get("intensities", [])),
            "peak_wavelength": d.get("peak_wavelength")}


def scan_set_acquisition(integration_ms=None, averages=None) -> None:
    """Set acquisition from the scanner. Integration is given in ms (→ µs here)."""
    if integration_ms is not None:
        us = max(1.0, float(integration_ms) * 1000.0)
        _queue_cmd(lambda sp: sp.set_integration_time(us))
    if averages is not None:
        n = max(1, int(averages))
        _queue_cmd(lambda sp: sp.set_averaging(n))


def scan_acquisition_state() -> dict:
    with _latest_lock:
        d = dict(_latest)
    it_us = d.get("integration_time_us")
    return {"integration_ms": round(it_us / 1000.0, 4) if it_us else None,
            "averages": d.get("n_averaged")}


# ---------------------------------------------------------------------------
# Routes — API (mounted at /api/spec)
# ---------------------------------------------------------------------------

@hr_bp.route("/info")
def api_info():
    if _spec is None:
        return jsonify({"connected": False, "error": _connect_error})
    return jsonify({
        "connected": True,
        "model": _spec.model,
        "serial": _spec.serial,
        "pixels": _spec.pixels,
        "wl_min": round(_wavelengths[0], 2),
        "wl_max": round(_wavelengths[-1], 2),
        "wavelengths": [round(w, 4) for w in _wavelengths],
        "max_intensity": _spec.max_intensity,
        "integration_limits_us": _spec.integration_limits_us,
        "integration_time_us": _spec.integration_time_us,
        "scans_to_average": _spec.scans_to_average,
        "boxcar": _spec.boxcar_width,
        "dark": _spec.correct_dark,
    })


@hr_bp.route("/status")
def api_status():
    """Lightweight: stats only (no full intensities array)."""
    if _spec is None:
        return jsonify({"connected": False, "error": _connect_error})
    with _latest_lock:
        out = {k: v for k, v in _latest.items() if k != "intensities"}
    return jsonify(out)


@hr_bp.route("/spectrum")
def api_spectrum():
    """The full intensities array + stats for the live plot."""
    if _spec is None:
        return jsonify({"connected": False, "error": _connect_error})
    with _latest_lock:
        return jsonify(dict(_latest))


@hr_bp.route("/integration", methods=["POST"])
def api_integration():
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    try:
        us = float(request.get_json(force=True)["micros"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    _queue_cmd(lambda sp: sp.set_integration_time(us))
    return jsonify({"ok": True})


@hr_bp.route("/averaging", methods=["POST"])
def api_averaging():
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    try:
        n = int(request.get_json(force=True)["scans"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    _queue_cmd(lambda sp: sp.set_averaging(n))
    return jsonify({"ok": True})


@hr_bp.route("/boxcar", methods=["POST"])
def api_boxcar():
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    try:
        w = int(request.get_json(force=True)["width"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    _queue_cmd(lambda sp: sp.set_boxcar(w))
    return jsonify({"ok": True})


@hr_bp.route("/dark", methods=["POST"])
def api_dark():
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    enable = bool(request.get_json(force=True).get("enable", True))
    _queue_cmd(lambda sp: sp.set_dark_correction(enable))
    return jsonify({"ok": True})


@hr_bp.route("/save")
def api_save():
    """Download the latest spectrum as CSV (wavelength_nm, intensity_counts)."""
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    with _latest_lock:
        inten = list(_latest.get("intensities", []))
        peak_wl = _latest.get("peak_wavelength")
        it = _latest.get("integration_time_us")
        navg = _latest.get("n_averaged")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# HR4000 spectrum  model={_spec.model}  serial={_spec.serial}"])
    w.writerow([f"# integration_us={it}  scans_averaged={navg}  peak_nm={peak_wl}"])
    w.writerow(["wavelength_nm", "intensity_counts"])
    for wl, y in zip(_wavelengths, inten):
        w.writerow([f"{wl:.4f}", f"{y:.3f}"])
    fname = time.strftime("hr4000_%Y%m%d_%H%M%S.csv")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@hr_bp.route("/plot")
def api_plot():
    """Download the latest spectrum as a matplotlib figure (?format=png|svg|pdf)."""
    from server import plots
    if not plots.MPL_OK:
        return plots.unavailable()
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    with _latest_lock:
        inten = list(_latest.get("intensities", []))
        peak_wl = _latest.get("peak_wavelength")
        it = _latest.get("integration_time_us")
        navg = _latest.get("n_averaged")
        sat = _latest.get("max_intensity")
    if not inten or len(_wavelengths) != len(inten):
        return jsonify({"error": "No spectrum acquired yet"}), 404
    sub = f"integration {(it or 0) / 1000:.2f} ms · {navg or 1}× averaged"
    log_scale = request.args.get("log", "").lower() in ("1", "true", "yes")
    fig = plots.spectrum_figure(_wavelengths, inten,
                                f"HR4000 spectrum — S/N {_spec.serial}",
                                subtitle=sub, peak_wl=peak_wl, sat_level=sat,
                                log_scale=log_scale)
    return plots.respond(fig, "hr4000")


@hr_bp.route("/reconnect", methods=["POST"])
def api_reconnect():
    global _spec
    _sampler_stop.set()
    if _sampler_thread is not None:
        _sampler_thread.join(timeout=2.0)
    if _spec is not None:
        try:
            _spec.close()
        except Exception:
            pass
        _spec = None
    _init_spectrometer()
    return jsonify({"connected": _spec is not None, "error": _connect_error})


# ---------------------------------------------------------------------------
# Standalone app
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(hr_bp, url_prefix="/api/spec")

    @app.route("/")
    def index():
        return render_template("hr4000.html")

    return app


if __name__ == "__main__":
    _init_spectrometer()
    create_app().run(host="0.0.0.0", port=5004, threaded=True, debug=False)

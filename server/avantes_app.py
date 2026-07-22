"""
Avantes AvaSpec spectrometer (SensLine / NIRLine) — Flask blueprint + viewer.

Standalone web app to observe live spectra, mirroring the HR4000 viewer
(server/hr4000_app.py): one background sampler thread owns the spectrometer and
publishes the latest spectrum; Flask handlers read that snapshot and queue
control commands (integration time, averaging, smoothing, dark) onto the sampler
thread so all device I/O stays single-threaded.

Runs natively (the Avantes lib is loaded by drivers/avantes.py). NOT wired into
the merged scanner — run it on its own:

    python -m server.avantes_app    →  http://localhost:5005

macOS note: if the vendored library is quarantined, clear it once:
    xattr -dr com.apple.quarantine drivers/libavs/
"""

import csv
import io
import threading
import time

from flask import Blueprint, Flask, jsonify, render_template, request, Response

from drivers.avantes import Avantes, PORT_USB

av_bp = Blueprint("avantes", __name__)

# ---------------------------------------------------------------------------
# Global spectrometer state
# ---------------------------------------------------------------------------

_spec: Avantes | None = None
_connect_error: str = ""
_wavelengths: list = []

_latest: dict = {"connected": False}
_latest_lock = threading.Lock()

_cmd_queue: list = []
_cmd_lock = threading.Lock()

_sampler_thread: threading.Thread | None = None
_sampler_stop = threading.Event()

# AVS_Init port mode: PORT_USB (0) or PORT_ALL (-1, includes AS7010 Ethernet).
PORT_MODE = PORT_USB


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
                "integration_time_ms": s.integration_time_ms,
                "n_averaged": s.n_averaged,
                "smooth_pix": s.smooth_pix,
                "peak_wavelength": round(s.peak_wavelength, 3),
                "peak_intensity": round(s.peak_intensity, 1),
                "total_counts": s.total_counts,
                "max_counts": s.max_counts,
                "saturated": s.saturated,
                "pixels": s.pixels,
                "timestamp": s.timestamp,
            }
        _sampler_stop.wait(0.01)


def _init_spectrometer() -> None:
    global _spec, _connect_error, _wavelengths, _sampler_thread
    try:
        sp = Avantes(port_mode=PORT_MODE)
        _spec = sp
        _wavelengths = sp.wavelengths
        _connect_error = ""
        print(f"Connected: {sp.name or sp.line.name}  S/N {sp.serial}  "
              f"{sp.pixels} px  {_wavelengths[0]:.1f}-{_wavelengths[-1]:.1f} nm  "
              f"line={sp.line.name}")
    except Exception as exc:
        _connect_error = str(exc)
        print(f"Avantes connection failed: {exc}")
        return

    _sampler_stop.clear()
    _sampler_thread = threading.Thread(target=_sampler_loop, daemon=True,
                                       name="avantes-sampler")
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
    """Set acquisition from the scanner. Avantes integration is natively in ms."""
    if integration_ms is not None:
        ms = max(0.002, float(integration_ms))
        _queue_cmd(lambda sp: sp.set_integration_time(ms))
    if averages is not None:
        n = max(1, int(averages))
        _queue_cmd(lambda sp: sp.set_averaging(n))


def scan_acquisition_state() -> dict:
    with _latest_lock:
        d = dict(_latest)
    return {"integration_ms": d.get("integration_time_ms"),
            "averages": d.get("n_averaged")}


# ---------------------------------------------------------------------------
# Routes — API (mounted at /api/avantes)
# ---------------------------------------------------------------------------

@av_bp.route("/info")
def api_info():
    if _spec is None:
        return jsonify({"connected": False, "error": _connect_error})
    return jsonify({
        "connected": True,
        "name": _spec.name,
        "serial": _spec.serial,
        "line": _spec.line.name,
        "detector": _spec.line.detector,
        "pixels": _spec.pixels,
        "wl_min": round(_wavelengths[0], 2),
        "wl_max": round(_wavelengths[-1], 2),
        "wavelengths": [round(w, 4) for w in _wavelengths],
        "max_counts": _spec.max_counts,
        "high_res_adc": _spec.high_res_adc,
        "firmware": _spec.fw,
        "fpga": _spec.fpga,
        "library": _spec.libver,
        "integration_time_ms": _spec.integration_time_ms,
        "n_averaged": _spec.nr_averages,
        "smooth_pix": _spec.smooth_pix,
        "dark": _spec.dark_correction,
    })


@av_bp.route("/status")
def api_status():
    if _spec is None:
        return jsonify({"connected": False, "error": _connect_error})
    with _latest_lock:
        out = {k: v for k, v in _latest.items() if k != "intensities"}
    return jsonify(out)


@av_bp.route("/spectrum")
def api_spectrum():
    if _spec is None:
        return jsonify({"connected": False, "error": _connect_error})
    with _latest_lock:
        return jsonify(dict(_latest))


@av_bp.route("/integration", methods=["POST"])
def api_integration():
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    try:
        ms = float(request.get_json(force=True)["ms"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    _queue_cmd(lambda sp: sp.set_integration_time(ms))
    return jsonify({"ok": True})


@av_bp.route("/averaging", methods=["POST"])
def api_averaging():
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    try:
        n = int(request.get_json(force=True)["scans"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    _queue_cmd(lambda sp: sp.set_averaging(n))
    return jsonify({"ok": True})


@av_bp.route("/smoothing", methods=["POST"])
def api_smoothing():
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    try:
        px = int(request.get_json(force=True)["pixels"])
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    _queue_cmd(lambda sp: sp.set_smoothing(px))
    return jsonify({"ok": True})


@av_bp.route("/dark", methods=["POST"])
def api_dark():
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    enable = bool(request.get_json(force=True).get("enable", True))
    _queue_cmd(lambda sp: sp.set_dark_correction(enable))
    return jsonify({"ok": True})


@av_bp.route("/save")
def api_save():
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    with _latest_lock:
        inten = list(_latest.get("intensities", []))
        peak = _latest.get("peak_wavelength")
        it = _latest.get("integration_time_ms")
        navg = _latest.get("n_averaged")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"# Avantes {_spec.line.name}  name={_spec.name}  serial={_spec.serial}"])
    w.writerow([f"# integration_ms={it}  averages={navg}  peak_nm={peak}"])
    w.writerow(["wavelength_nm", "intensity_counts"])
    for wl, y in zip(_wavelengths, inten):
        w.writerow([f"{wl:.4f}", f"{y:.3f}"])
    fname = time.strftime("avantes_%Y%m%d_%H%M%S.csv")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@av_bp.route("/plot")
def api_plot():
    """Download the latest spectrum as a matplotlib figure (?format=png|svg|pdf)."""
    from server import plots
    if not plots.MPL_OK:
        return plots.unavailable()
    if _spec is None:
        return jsonify({"error": "Not connected"}), 503
    with _latest_lock:
        inten = list(_latest.get("intensities", []))
        peak = _latest.get("peak_wavelength")
        it = _latest.get("integration_time_ms")
        navg = _latest.get("n_averaged")
        sat = _latest.get("max_counts")
    if not inten or len(_wavelengths) != len(inten):
        return jsonify({"error": "No spectrum acquired yet"}), 404
    sub = f"integration {it or 0:.2f} ms · {navg or 1}× averaged"
    fig = plots.spectrum_figure(_wavelengths, inten,
                                f"Avantes {_spec.line.name} — S/N {_spec.serial}",
                                subtitle=sub, peak_wl=peak, sat_level=sat)
    return plots.respond(fig, "avantes")


@av_bp.route("/reconnect", methods=["POST"])
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
    app.register_blueprint(av_bp, url_prefix="/api/avantes")

    @app.route("/")
    def index():
        return render_template("avantes.html")

    return app


if __name__ == "__main__":
    _init_spectrometer()
    create_app().run(host="0.0.0.0", port=5005, threaded=True, debug=False)

"""
Matplotlib figure export for the VSL scan app.

Builds publication-style figures (white background, regardless of UI theme)
and serves them as image downloads. Uses the matplotlib OO API (Figure, no
pyplot) so it is safe under Flask's threaded server.

Every endpoint accepts ?format=png|svg|pdf (default png).
"""

import io
import time

from flask import Response, jsonify, request

try:
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    MPL_OK = True
except Exception:                                            # pragma: no cover
    MPL_OK = False

FORMATS = {"png": "image/png", "svg": "image/svg+xml", "pdf": "application/pdf"}

# Overlay colors matched to the web UI's band palette
BAND_COLORS = ["#c9a800", "#e07f26", "#d94f4f", "#9066d9", "#2f9e63", "#d45faf"]
LINE = "#0088ce"       # main curve — UAH blue
ACCENT = "#4fadde"     # fits
GREEN = "#1f8f55"
ORANGE = "#c77c2a"


def unavailable() -> Response:
    return jsonify({"error": "matplotlib is not installed on the server — "
                             "pip install matplotlib"}), 501


def new_figure(w=8.0, h=5.0):
    fig = Figure(figsize=(w, h), dpi=110)
    ax = fig.subplots()
    style_axes(ax)
    return fig, ax


def style_axes(ax) -> None:
    ax.grid(True, which="major", color="0.85", linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(labelsize=9)


def respond(fig, stem: str) -> Response:
    """Serve a Figure as a download in the requested ?format=."""
    fmt = request.args.get("format", "png").lower()
    if fmt not in FORMATS:
        fmt = "png"
    buf = io.BytesIO()
    fig.savefig(buf, format=fmt, dpi=200, bbox_inches="tight",
                facecolor="white")
    fname = time.strftime(f"{stem}_%Y%m%d_%H%M%S.{fmt}")
    return Response(buf.getvalue(), mimetype=FORMATS[fmt],
                    headers={"Content-Disposition":
                             f"attachment; filename={fname}"})


def _axis_label(label: str, unit: str) -> str:
    label = label or ""
    return f"{label} ({unit})" if unit and unit not in label else label


# ---------------------------------------------------------------------------
# Figure builders
# ---------------------------------------------------------------------------

def scan_figure(pts, meta, bands=None, profile=None, vsl=None):
    """
    Signal-vs-position scan with the same optional overlays as the web plot:
    band-integrated curves, knife-edge derivative + Gaussian fit, VSL SSG fit.
    """
    fig, ax = new_figure()
    xs = [p["x"] for p in pts]
    ys = [p["value"] for p in pts]
    errs = [p.get("std") or 0.0 for p in pts]

    if any(errs):
        ax.errorbar(xs, ys, yerr=errs, fmt="o-", ms=3, lw=1.3, color=LINE,
                    ecolor="0.6", elinewidth=0.8, capsize=2,
                    label=meta.get("y_label") or "Signal")
    else:
        ax.plot(xs, ys, "o-", ms=3, lw=1.3, color=LINE,
                label=meta.get("y_label") or "Signal")

    # Peak marker
    if ys:
        k = max(range(len(ys)), key=lambda i: ys[i])
        ax.axvline(xs[k], color=GREEN, lw=0.9, ls="--", alpha=0.7)

    for i, b in enumerate(bands or []):
        c = BAND_COLORS[i % len(BAND_COLORS)]
        bx = [x for x, v in zip(b["x"], b["values"]) if v is not None]
        bv = [v for v in b["values"] if v is not None]
        ax.plot(bx, bv, "-", lw=1.2, color=c, label=b["label"])

    if profile:
        dmax = max((abs(v) for v in profile["deriv_y"]), default=0.0)
        g = profile.get("gaussian")
        if g:
            dmax = max(dmax, g["amplitude"])
        ymin, ymax = min(ys + [0]), max(ys)
        span = (ymax - ymin) or 1.0

        def norm(v):
            return ymin + (abs(v) / dmax) * span if dmax > 0 else ymin

        ax.plot(profile["deriv_x"], [norm(v) for v in profile["deriv_y"]],
                ls="--", lw=1.1, color=ORANGE, label="d(signal)/dx (norm.)")
        if g:
            ax.plot(g["curve_x"], [norm(v) for v in g["curve_y"]],
                    lw=1.5, color=ORANGE, alpha=0.85,
                    label=f"Gaussian fit  FWHM={g['fwhm']:.3g} {meta.get('x_unit', '')}")
            ax.axvline(g["center"], color=ORANGE, lw=0.8, ls=":")

    if vsl:
        xu = meta.get("x_unit", "")
        ax.plot(vsl["curve_z"], vsl["curve_I"], ls="--", lw=1.4, color=ACCENT,
                label=f"SSG fit  g={vsl['best_g']:.4g} 1/{xu}")
        ax.axvline(vsl["z_sat"], color=GREEN, lw=0.9, ls=":",
                   label=f"z$_{{sat}}$={vsl['z_sat']:.3g} {xu}")
        sat = vsl.get("sat")
        if sat:
            ax.plot(sat["curve_z"], sat["curve_I"], ls="-.", lw=1.4,
                    color="#7a52c7",
                    label=f"saturation model  g$_0$={sat['g0']:.4g} 1/{xu}, "
                          f"I$_s$={sat['I_s']:.3g}")

    ax.set_xlabel(_axis_label(meta.get("x_label", "Position"),
                              meta.get("x_unit", "")), fontsize=10)
    ax.set_ylabel(_axis_label(meta.get("y_label", "Signal"),
                              meta.get("y_unit", "")), fontsize=10)
    ax.set_title(meta.get("title", "VSL scan"), fontsize=11)
    ax.legend(fontsize=8, frameon=False)
    return fig


def vsl_figure(res, meta):
    """Cutoff-scan of the fitted small-signal gain: g vs z_fit with 95 % CI."""
    fig, ax = new_figure(8.0, 4.2)
    xu = meta.get("x_unit", "")
    scan = res["scan"]
    zs = [s["z_fit"] for s in scan]
    gs = [s["g"] for s in scan]
    lo = [s["g"] - (s["ci95"] or 0) for s in scan]
    hi = [s["g"] + (s["ci95"] or 0) for s in scan]

    ax.fill_between(zs, lo, hi, color=ACCENT, alpha=0.15, label="95 % CI")
    ax.plot(zs, gs, "-", lw=1.5, color=ACCENT, label="g(z$_{fit}$) cutoff scan")
    inst = res.get("inst")
    if inst:
        ax.plot(inst["z"], inst["g_inst"], "o-", ms=3, lw=1.0, color=ORANGE,
                alpha=0.8,
                label=f"g$_{{inst}}$(z)=(dI/dz−A$_{{sp}}$)/I  "
                      f"plateau {inst['g0']:.4g} 1/{xu}")
    sat = res.get("sat")
    if sat:
        ax.plot(sat["curve_z"], sat["curve_g"], "-", lw=1.6, color="#7a52c7",
                label=f"saturation model g$_0$/(1+I/I$_s$)  "
                      f"g$_0$={sat['g0']:.4g} 1/{xu}")
        if sat.get("z_sat") is not None:
            ax.axvline(sat["z_sat"], color="#7a52c7", lw=0.9, ls="-.",
                       label=f"z(I=I$_s$)={sat['z_sat']:.3g} {xu}")
    ax.axhline(res["best_g"], color="#b39a1f", lw=1.0, ls="--",
               label=f"g={res['best_g']:.4g} ±{res['best_ci95']:.2g} 1/{xu}")
    ax.axvline(res["z_sat"], color=GREEN, lw=1.0, ls=":",
               label=f"z$_{{sat}}$={res['z_sat']:.3g} {xu}")

    ax.set_xlabel(f"stripe length z ({xu})", fontsize=10)
    ax.set_ylabel(f"gain g (1/{xu})", fontsize=10)
    ax.set_title("VSL gain — cutoff scan & instantaneous gain", fontsize=11)
    ax.legend(fontsize=8, frameon=False)
    return fig


def spectra_map_figure(wl, specs, xs, meta):
    """Wavelength × position intensity map from the per-step captured spectra."""
    fig, ax = new_figure(8.0, 5.0)
    ax.grid(False)
    # Columns = scan points, rows = pixels; mask missing spectra as zero rows.
    cols = [(s if s else [0.0] * len(wl)) for s in specs]
    z = [[cols[j][i] for j in range(len(cols))] for i in range(len(wl))]
    im = ax.pcolormesh(xs, wl, z, shading="nearest", cmap="viridis",
                       rasterized=True)
    fig.colorbar(im, ax=ax, label="Intensity (counts)")
    ax.set_xlabel(_axis_label(meta.get("x_label", "Position"),
                              meta.get("x_unit", "")), fontsize=10)
    ax.set_ylabel("Wavelength (nm)", fontsize=10)
    ax.set_title("Captured spectra vs position", fontsize=11)
    return fig


def spectrum_figure(wl, inten, title, subtitle="", peak_wl=None, sat_level=None):
    """Single spectrum (live spectrometer view)."""
    fig, ax = new_figure(8.0, 4.5)
    ax.plot(wl, inten, "-", lw=0.9, color=LINE)
    if peak_wl is not None:
        ax.axvline(peak_wl, color=GREEN, lw=0.9, ls="--",
                   label=f"peak {peak_wl:.1f} nm")
        ax.legend(fontsize=8, frameon=False)
    if sat_level is not None and inten and max(inten) > 0.8 * sat_level:
        ax.axhline(sat_level, color="#d94f4f", lw=0.8, ls=":")
    ax.set_xlabel("Wavelength (nm)", fontsize=10)
    ax.set_ylabel("Intensity (counts)", fontsize=10)
    ax.set_title(title + (f"\n{subtitle}" if subtitle else ""), fontsize=11)
    ax.margins(x=0)
    return fig


def power_figure(ts, ps, span_s, wavelength_nm=None):
    """Optical power vs time from the strip-chart ring buffer."""
    fig, ax = new_figure(8.0, 4.2)
    t0 = ts[-1] if ts else 0
    rel = [(t - t0) / 1000.0 for t in ts]
    ax.plot(rel, ps, "-", lw=1.1, color=LINE)
    ax.set_xlim(-span_s, 0)
    ax.set_xlabel("Time (s)", fontsize=10)
    ax.set_ylabel("Optical power (W)", fontsize=10)
    sub = f"λ-correction {wavelength_nm:.0f} nm" if wavelength_nm else ""
    ax.set_title("PM400 optical power" + (f" — {sub}" if sub else ""),
                 fontsize=11)
    return fig

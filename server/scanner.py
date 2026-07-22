"""
VSL Scan — Actuator + Detector Scan (merged app)
=================================================

A single Flask app that drives an ACTUATOR and a DETECTOR in one process and
runs coordinated "signal vs. position" scans. Both sides are auto-detected and
abstracted, so the scan engine is identical regardless of the attached hardware.

Actuators (server/actuators.py):
  * Thorlabs BPC301 / DRV517 piezo   — position (µm) / voltage (V); Zero
  * Newport SMC100CC / LTA-HS        — position (mm); Home

Detectors (server/detectors.py):
  * Thorlabs PM400 power meter       — signal = optical power (W)
  * Ocean Optics HR4000              — signal = peak / integrated / intensity@λ
  * Avantes SensLine / NIRLine         (full spectra are captured per step too)

The engine steps the active actuator to each setpoint, waits for it to settle,
asks the active detector for one measurement over a dwell window, and records a
(position, signal) point for the live plot. With a spectrometer detector it also
stores the full spectrum at each step (downloadable as a wavelength×position
matrix).

Run (from the repo root):  python run.py   →  http://localhost:5050
      or:  python -m server.scanner
"""

import csv
import io
import math
import threading
import time

from flask import Flask, jsonify, render_template, request, Response

from server import app as piezo            # piezo blueprint
from server import power_monitor as power  # power blueprint
from server import smc100_app as smc       # SMC100 blueprint
from server import hr4000_app as hr        # HR4000 blueprint
from server import avantes_app as av       # Avantes blueprint
from server import actuators
from server import detectors
from server import plots

# ---------------------------------------------------------------------------
# Scan state
# ---------------------------------------------------------------------------

_scan_lock = threading.Lock()
_scan_thread: threading.Thread | None = None

_scan = {
    "running": False,
    "actuator": None, "axis": None, "x_label": "", "x_unit": "",
    "detector": None, "metric": None, "wavelength": None,
    "y_label": "", "y_unit": "", "y_kind": "",
    "total": 0, "done": 0, "current_setpoint": None,
    "message": "", "error": "",
    "started_at": None, "finished_at": None, "config": {},
    "spectra_wavelengths": None,    # set when the detector is a spectrometer
}
_points: list[dict] = []
_spectra: list[list] = []           # full spectrum per point (spectrometer only)
_points_lock = threading.Lock()
_abort = threading.Event()

# Current detector-signal selection (also used between scans for the live readout)
_sel_metric: str = "power"
_sel_wavelength = None


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


def _solve3(M, r):
    """Solve a 3x3 linear system by Gaussian elimination. None if singular."""
    A = [row[:] + [r[i]] for i, row in enumerate(M)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda i: abs(A[i][col]))
        if abs(A[piv][col]) < 1e-12:
            return None
        A[col], A[piv] = A[piv], A[col]
        f = A[col][col]
        A[col] = [v / f for v in A[col]]
        for i in range(3):
            if i != col:
                g = A[i][col]
                A[i] = [A[i][j] - g * A[col][j] for j in range(4)]
    return [A[0][3], A[1][3], A[2][3]]


def _gaussian_profile(xs, ys):
    """
    Knife-edge → beam-profile post-processor.

    The scan (signal vs. position) is an edge (error-function). Its first
    derivative is the Gaussian beam profile, so we:
      1. differentiate by central differences → rate-of-change curve,
      2. locate the steepest pair (greatest |slope|) = edge centre,
      3. fit a Gaussian to |derivative| in closed form (weighted log-parabola,
         Caruana's method — no scipy), giving centre, σ and FWHM.
    Returns derivative curve + smooth Gaussian fit, or None if too few points.
    """
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    dx, dy = [], []
    for i in range(1, n - 1):
        span = xs[i + 1] - xs[i - 1]
        slope = (ys[i + 1] - ys[i - 1]) / span if span else 0.0
        dx.append(xs[i])
        dy.append(slope)
    prof = [abs(v) for v in dy]
    k = max(range(len(prof)), key=lambda i: prof[i])
    max_slope = {"x": dx[k], "slope": dy[k]}

    # Weighted (w = p²) log-parabola fit:  ln p = a + b·x + c·x²
    Sw = Swx = Swx2 = Swx3 = Swx4 = Swy = Swxy = Swx2y = 0.0
    for x, p in zip(dx, prof):
        if p <= 0:
            continue
        w = p * p
        ln = math.log(p)
        x2 = x * x
        Sw += w;        Swx += w * x;      Swx2 += w * x2
        Swx3 += w * x2 * x;                Swx4 += w * x2 * x2
        Swy += w * ln;  Swxy += w * x * ln; Swx2y += w * x2 * ln
    sol = _solve3([[Sw, Swx, Swx2], [Swx, Swx2, Swx3], [Swx2, Swx3, Swx4]],
                  [Swy, Swxy, Swx2y])
    fit = None
    if sol:
        a, b, c = sol
        if c < 0:                                   # concave-down ⇒ real Gaussian
            sigma = math.sqrt(-1.0 / (2.0 * c))
            x0 = -b / (2.0 * c)
            amp = math.exp(a - b * b / (4.0 * c))
            fwhm = 2.0 * math.sqrt(2.0 * math.log(2.0)) * sigma
            lo, hi = dx[0], dx[-1]
            xg = [lo + (hi - lo) * i / 200.0 for i in range(201)]
            yg = [amp * math.exp(-(x - x0) ** 2 / (2.0 * sigma * sigma)) for x in xg]
            fit = {"center": round(x0, 6), "sigma": round(sigma, 6),
                   "fwhm": round(fwhm, 6), "amplitude": amp,
                   "curve_x": xg, "curve_y": yg}
    return {"deriv_x": dx, "deriv_y": dy, "max_slope": max_slope, "gaussian": fit}


# ---------------------------------------------------------------------------
# VSL gain analysis — small-signal gain (SSG) model
# ---------------------------------------------------------------------------

def _t95(df):
    """Two-tailed 95% CI t-quantile for given degrees of freedom."""
    table = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
             6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
             15: 2.131, 20: 2.086, 30: 2.042, 60: 2.000}
    if df <= 0:
        return float("inf")
    if df >= 120:
        return 1.96
    keys = sorted(table)
    for i, k in enumerate(keys[:-1]):
        if k <= df <= keys[i + 1]:
            t = (df - k) / (keys[i + 1] - k)
            return table[k] * (1 - t) + table[keys[i + 1]] * t
    return table[keys[-1]]


def _ssg_h(z, g):
    """h(z,g) = (e^{gz}-1)/g, with L'Hôpital limit z at g→0."""
    if abs(g) < 1e-9:
        return z
    return (math.exp(min(g * z, 700.0)) - 1.0) / g


def _ssg_dhg(z, g):
    """∂h/∂g = (z·e^{gz}·g − (e^{gz}−1)) / g², limit z²/2 at g→0."""
    if abs(g) < 1e-9:
        return z * z * 0.5
    gz = min(g * z, 700.0)
    eg = math.exp(gz)
    return (z * eg * g - (eg - 1.0)) / (g * g)


def _fit_ssg(zs, Is):
    """
    Fit I_ASE(z) = A_sp · h(z,g) to (zs, Is).

    A_sp is eliminated analytically (linear in h for fixed g); g is found by
    golden-section search over [g_lo, g_hi].  Returns {g, A_sp, ci95, rss}
    or None if the fit is degenerate.
    """
    n = len(zs)
    if n < 4:
        return None

    def a_from_g(g):
        hs = [_ssg_h(z, g) for z in zs]
        sh2 = sum(h * h for h in hs)
        if not math.isfinite(sh2) or sh2 < 1e-100:
            return None, hs
        num = sum(I * h for I, h in zip(Is, hs))
        if not math.isfinite(num):
            return None, hs
        return num / sh2, hs

    def rss_g(g):
        if g <= 1e-9:
            return float("inf")
        A, hs = a_from_g(g)
        if A is None or not math.isfinite(A) or A <= 0:
            return float("inf")
        r = sum((I - A * h) ** 2 for I, h in zip(Is, hs))
        return r if math.isfinite(r) else float("inf")

    z_span = zs[-1] - zs[0]
    if z_span <= 0:
        return None
    pos_I = [I for I in Is if I > 0]
    I_max = max(Is) if Is else 1.0
    I_min = min(pos_I) if pos_I else 1e-30
    g_rough = math.log(max(I_max / I_min, math.e)) / z_span
    # Bound g_hi so that h² = exp(2·g·z_max) doesn't overflow (max ~exp(709))
    z_max = zs[-1]
    g_hi_safe = 354.0 / max(z_max, 1e-9)
    g_lo, g_hi = 1e-4, min(max(g_rough * 10.0, 100.0), g_hi_safe)

    phi = (3.0 - math.sqrt(5.0)) / 2.0
    a, b = g_lo, g_hi
    x1 = a + phi * (b - a)
    x2 = b - phi * (b - a)
    f1, f2 = rss_g(x1), rss_g(x2)
    for _ in range(150):
        if f1 < f2:
            b = x2; x2 = x1; f2 = f1
            x1 = a + phi * (b - a); f1 = rss_g(x1)
        else:
            a = x1; x1 = x2; f1 = f2
            x2 = b - phi * (b - a); f2 = rss_g(x2)
        if (b - a) < g_hi * 1e-9:
            break
    g_opt = (a + b) * 0.5

    A_opt, hs = a_from_g(g_opt)
    if A_opt is None or A_opt <= 0:
        return None

    rss_val = sum((I - A_opt * h) ** 2 for I, h in zip(Is, hs))
    df = n - 2
    sigma2 = rss_val / max(df, 1)

    # Jacobian: J[:,0]=∂I/∂g, J[:,1]=∂I/∂A_sp
    J_g = [A_opt * _ssg_dhg(z, g_opt) for z in zs]
    J00 = sum(jg * jg for jg in J_g)
    J01 = sum(jg * h for jg, h in zip(J_g, hs))
    J11 = sum(h * h for h in hs)
    det = J00 * J11 - J01 * J01
    if det < 1e-300:
        return None
    var_g = sigma2 * J11 / det
    if var_g <= 0:
        return {"g": g_opt, "A_sp": A_opt, "ci95": float("inf"), "rss": rss_val}
    ci95 = _t95(df) * math.sqrt(var_g)
    return {"g": g_opt, "A_sp": A_opt, "ci95": ci95, "rss": rss_val}


def _vsl_gain(xs, ys, z0=None):
    """
    VSL scanning-cutoff SSG gain analysis (Fig 3b style from the paper).

    For each z_fit cutoff, fits I_ASE = (A_sp/g)(e^{gz}−1) to data [z0, z_fit].
    Returns the g-vs-z_fit stability curve and the best estimate at minimum CI.
    """
    n = min(len(xs), len(ys))
    if n < 5:
        return None

    pairs = sorted(zip(xs[:n], ys[:n]))
    all_z = [p[0] for p in pairs]
    all_I = [p[1] for p in pairs]

    if z0 is None:
        z0 = all_z[0]

    filt = [(z, I) for z, I in zip(all_z, all_I) if z >= z0 - 1e-12]
    if len(filt) < 5:
        return None
    zs = [p[0] for p in filt]
    Is = [p[1] for p in filt]

    scan = []
    for end_idx in range(3, len(zs)):
        fit = _fit_ssg(zs[: end_idx + 1], Is[: end_idx + 1])
        if fit is None:
            continue
        if not math.isfinite(fit["g"]):
            continue
        scan.append({
            "z_fit": round(zs[end_idx], 6),
            "g": round(fit["g"], 6),
            "ci95": round(fit["ci95"], 6) if math.isfinite(fit["ci95"]) else None,
            "A_sp": round(fit["A_sp"], 9),
            "n_pts": end_idx + 1,
        })

    if not scan:
        return None

    finite = [s for s in scan if s["ci95"] is not None]
    if not finite:
        return None
    best = min(finite, key=lambda s: s["ci95"])

    g_b, A_b = best["g"], best["A_sp"]
    z_range = zs[-1] - zs[0]
    curve_z = [zs[0] + z_range * i / 200 for i in range(201)]
    curve_I = [A_b * _ssg_h(z, g_b) for z in curve_z]

    return {
        "scan": scan,
        "best_g": best["g"],
        "best_ci95": best["ci95"],
        "z_sat": best["z_fit"],
        "A_sp": best["A_sp"],
        "curve_z": curve_z,
        "curve_I": curve_I,
        "z0": z0,
    }


def _sat_integrate(z_grid, a_sp, g0, i_s):
    """
    Integrate the saturated ASE amplifier equation
        dI/dz = A_sp + g0·I / (1 + I/I_s)
    (homogeneous gain saturation, De Giorgi & Anni) from I(0)=0 over the
    sorted z_grid using RK4 with sub-stepping. Returns I at each grid point,
    or None on blow-up.
    """
    def f(I):
        return a_sp + g0 * I / (1.0 + I / i_s)

    out = []
    I, z = 0.0, 0.0
    for zt in z_grid:
        span = zt - z
        if span < 0:
            return None
        nsub = max(1, int(math.ceil(span / max(z_grid[-1], 1e-9) * 40)))
        h = span / nsub if nsub else 0.0
        for _ in range(nsub):
            k1 = f(I)
            k2 = f(I + 0.5 * h * k1)
            k3 = f(I + 0.5 * h * k2)
            k4 = f(I + h * k3)
            I += (h / 6.0) * (k1 + 2 * k2 + 2 * k3 + k4)
            if not math.isfinite(I) or I > 1e300:
                return None
        z = zt
        out.append(I)
    return out


def _fit_saturated(zs, Is, a0, g0_init, i_max):
    """
    Full-curve fit of the gain-saturation model I(z) from
    dI/dz = A_sp + g0·I/(1+I/I_s), following the saturated-regime analysis in
    De Giorgi & Anni. Unlike the cutoff-scan SSG fit (which discards the
    saturated tail), this uses ALL points and yields the unsaturated gain g0,
    the saturation intensity I_s, and A_sp together.

    Nelder–Mead over (ln A_sp, ln g0, ln I_s), seeded from the SSG fit
    (a0, g0_init) and I_s ≈ half the maximum measured signal. Returns
    {A_sp, g0, I_s, r2, rss} or None.
    """
    if len(zs) < 6 or i_max <= 0 or a0 <= 0 or g0_init <= 0:
        return None

    def rss(p):
        a, g, s = (math.exp(v) for v in p)
        model = _sat_integrate(zs, a, g, s)
        if model is None:
            return float("inf")
        r = sum((I - m) ** 2 for I, m in zip(Is, model))
        return r if math.isfinite(r) else float("inf")

    # Initial simplex around the seed (log-space)
    p0 = [math.log(a0), math.log(g0_init), math.log(i_max * 0.5)]
    simplex = [list(p0)]
    for i in range(3):
        q = list(p0)
        q[i] += 0.4
        simplex.append(q)
    fv = [rss(p) for p in simplex]
    if all(not math.isfinite(v) for v in fv):
        return None

    for _ in range(250):
        order = sorted(range(4), key=lambda k: fv[k])
        simplex = [simplex[k] for k in order]
        fv = [fv[k] for k in order]
        if fv[0] > 0 and (fv[3] - fv[0]) / max(fv[0], 1e-300) < 1e-9:
            break
        cen = [sum(s[i] for s in simplex[:3]) / 3.0 for i in range(3)]
        refl = [cen[i] + (cen[i] - simplex[3][i]) for i in range(3)]
        fr = rss(refl)
        if fr < fv[0]:
            exp_ = [cen[i] + 2.0 * (cen[i] - simplex[3][i]) for i in range(3)]
            fe = rss(exp_)
            simplex[3], fv[3] = (exp_, fe) if fe < fr else (refl, fr)
        elif fr < fv[2]:
            simplex[3], fv[3] = refl, fr
        else:
            con = [cen[i] + 0.5 * (simplex[3][i] - cen[i]) for i in range(3)]
            fc = rss(con)
            if fc < fv[3]:
                simplex[3], fv[3] = con, fc
            else:                                     # shrink
                for k in range(1, 4):
                    simplex[k] = [(simplex[k][i] + simplex[0][i]) / 2.0
                                  for i in range(3)]
                    fv[k] = rss(simplex[k])

    best = min(range(4), key=lambda k: fv[k])
    if not math.isfinite(fv[best]):
        return None
    a, g0, i_s = (math.exp(v) for v in simplex[best])

    mean_I = sum(Is) / len(Is)
    ss_tot = sum((I - mean_I) ** 2 for I in Is)
    r2 = 1.0 - fv[best] / ss_tot if ss_tot > 0 else 0.0
    return {"A_sp": a, "g0": g0, "I_s": i_s, "rss": fv[best], "r2": r2}


def _vsl_saturated(xs, ys, z0=None, seed=None):
    """
    Fit the saturation model to the full dataset and return fit parameters,
    a smooth model curve I(z), the model's local gain g(z)=g0/(1+I/I_s), and
    z_sat defined as the stripe length where I(z) reaches I_s.
    """
    n = min(len(xs), len(ys))
    if n < 6:
        return None
    pairs = sorted(zip(xs[:n], ys[:n]))
    if z0 is not None:
        pairs = [p for p in pairs if p[0] >= z0 - 1e-12]
    if len(pairs) < 6:
        return None
    zs = [p[0] for p in pairs]
    Is = [p[1] for p in pairs]
    i_max = max(Is)

    a0 = (seed or {}).get("A_sp") or i_max / max(zs[-1], 1e-9) * 0.01
    g0i = (seed or {}).get("best_g") or 1.0 / max(zs[-1], 1e-9)
    fit = _fit_saturated(zs, Is, a0, g0i, i_max)
    if fit is None:
        return None

    # Smooth model curve + local gain over the measured range
    z_hi = zs[-1]
    curve_z = [z_hi * i / 200 for i in range(201)]
    curve_I = _sat_integrate(curve_z, fit["A_sp"], fit["g0"], fit["I_s"])
    if curve_I is None:
        return None
    curve_g = [fit["g0"] / (1.0 + I / fit["I_s"]) for I in curve_I]

    # z_sat: where the model intensity crosses I_s (gain compressed to g0/2)
    z_sat = None
    for i in range(1, len(curve_z)):
        if curve_I[i] >= fit["I_s"]:
            f0, f1 = curve_I[i - 1], curve_I[i]
            t = (fit["I_s"] - f0) / (f1 - f0) if f1 > f0 else 0.0
            z_sat = curve_z[i - 1] + t * (curve_z[i] - curve_z[i - 1])
            break

    return {"g0": round(fit["g0"], 6),
            "I_s": fit["I_s"],
            "A_sp": fit["A_sp"],
            "z_sat": round(z_sat, 6) if z_sat is not None else None,
            "r2": round(fit["r2"], 5),
            "curve_z": [round(z, 6) for z in curve_z],
            "curve_I": curve_I,
            "curve_g": [round(g, 6) for g in curve_g]}


def _vsl_instantaneous(xs, ys, z0=None, a_sp=None):
    """
    Instantaneous (local) VSL gain, following De Giorgi & Anni,
    "Optical Gain of Lead Halide Perovskites Measured via the Variable Stripe
    Length Method: What We Can Learn and How to Avoid Pitfalls".

    The 1-D amplifier equation  dI/dz = A_sp + g(z)·I(z)  is inverted point by
    point:                      g_inst(z) = (dI/dz − A_sp) / I(z)

    Numerically this is evaluated in the equivalent logarithmic form
        g_inst(z) = d ln I / dz − A_sp / I(z)
    because the central-difference log-derivative is exact for exponential
    growth at any step size, while differencing I directly biases g low by
    ~g·Δz/2 at typical knife-edge step sizes.

    In the small-signal regime g_inst(z) is flat (= g₀); a roll-off at longer
    stripe reveals gain saturation / pump depletion / edge artifacts — exactly
    the pitfalls the paper warns fitting the whole curve would average over.

    A_sp (the spontaneous-emission source term) is dI/dz in the z→0 limit; if
    not supplied it is estimated as the median slope over the shortest lengths,
    where I≈0 so the amplified term vanishes. Cheap enough to run live while
    the scan is still acquiring.
    """
    n = min(len(xs), len(ys))
    if n < 5:
        return None
    pairs = sorted(zip(xs[:n], ys[:n]))
    if z0 is not None:
        pairs = [p for p in pairs if p[0] >= z0 - 1e-12]
    if len(pairs) < 5:
        return None
    zs = [p[0] for p in pairs]
    Is = [p[1] for p in pairs]

    # Light 3-point smoothing of I before differencing (derivatives amplify noise)
    Ism = [Is[0]] + [(Is[i - 1] + Is[i] + Is[i + 1]) / 3.0
                     for i in range(1, len(Is) - 1)] + [Is[-1]]

    # A_sp = dI/dz at z→0 (I≈0 there, so the g·I term is gone). The slope
    # grows as A·e^{gz}, i.e. ln(slope) is linear in z — fit the first few
    # slopes and extrapolate the intercept back to z=0.
    if a_sp is None:
        slopes = []                                # (z, dI/dz)
        for i in range(1, len(zs) - 1):
            span = zs[i + 1] - zs[i - 1]
            if span > 0:
                slopes.append((zs[i], (Ism[i + 1] - Ism[i - 1]) / span))
        if len(slopes) < 3:
            return None
        head = [(z, s) for z, s in slopes[:max(4, len(slopes) // 6)] if s > 0]
        if len(head) >= 3:
            hz = [z for z, _ in head]
            hl = [math.log(s) for _, s in head]
            mz = sum(hz) / len(hz); ml = sum(hl) / len(hl)
            sxx = sum((z - mz) ** 2 for z in hz)
            b = (sum((z - mz) * (l - ml) for z, l in zip(hz, hl)) / sxx
                 if sxx > 0 else 0.0)
            a_sp = math.exp(ml - b * mz) if b >= 0 else None
        if a_sp is None:                           # fall back: earliest slopes
            first = sorted(s for _, s in slopes[:3])
            a_sp = first[1]

    i_max = max(Is)
    floor = i_max * 0.02          # skip points where I is noise-level
    z_out, g_out, i_here_out = [], [], []
    for i in range(1, len(zs) - 1):
        span = zs[i + 1] - zs[i - 1]
        i_here = Ism[i]
        if span <= 0 or i_here <= floor or i_here <= 0:
            continue
        if Ism[i - 1] <= 0 or Ism[i + 1] <= 0:
            continue
        # g_inst = d(lnI)/dz − A_sp/I  (exact inversion of I=(A/g)(e^gz−1))
        dlog = (math.log(Ism[i + 1]) - math.log(Ism[i - 1])) / span
        g = dlog - a_sp / i_here
        if math.isfinite(g):
            z_out.append(round(zs[i], 6))
            g_out.append(round(g, 6))
            i_here_out.append(i_here)
    if len(g_out) < 3:
        return None

    # Plateau estimate: g_inst rises out of the noise at short z and only
    # decreases once saturation sets in, so the small-signal plateau is the
    # highest stable stretch — take the maximum of a 5-point moving median.
    win = 5
    if len(g_out) >= win:
        g0 = max(sorted(g_out[i:i + win])[win // 2]
                 for i in range(len(g_out) - win + 1))
    else:
        g0 = sorted(g_out)[len(g_out) // 2]

    return {"z": z_out, "g_inst": g_out,
            "A_sp": a_sp, "g0": round(g0, 6), "n": len(g_out)}


def _setpoints(start, stop, steps, step_size, bidirectional):
    start, stop = float(start), float(stop)
    if step_size and float(step_size) > 0 and not steps:
        n = int(abs(stop - start) / float(step_size)) + 1
    else:
        n = max(2, int(steps or 2))
    n = max(2, n)
    pts = [start + (stop - start) * i / (n - 1) for i in range(n)]
    if bidirectional:
        pts = pts + pts[-2::-1]
    return pts


def _run_scan(cfg, act, axis_key, x_unit, det, metric, wavelength):
    """
    Background worker: step the actuator and record the detector signal.

    The recorded "x" is the stripe length traveled from the FIRST point of
    the sweep — |position − origin| — not the raw actuator coordinate. This
    is what the knife edge physically exposes: it always starts at 0 and
    grows monotonically, even when the configured scan sweeps from a high
    position down toward zero (or down to zero) rather than 0 upward. Using
    the raw coordinate in that case would plot/analyze the data backwards
    (fitting the VSL gain curve as if it decayed instead of grew). The raw
    actuator position is kept alongside as "pos" for reference/export.
    """
    settle_s = cfg["settle_s"]; dwell_s = cfg["dwell_s"]
    inter_s = cfg["inter_s"]; repeat = cfg["repeat"]

    act.begin_scan(); det.begin_scan()
    origin = None
    try:
        act.prepare(axis_key)
        setpoints = _setpoints(cfg["start"], cfg["stop"], cfg["steps"],
                               cfg["step_size"], cfg["bidirectional"])
        passes = 0
        while not _abort.is_set():
            passes += 1
            for sp in setpoints:
                if _abort.is_set():
                    break
                _scan["current_setpoint"] = round(sp, 4)
                _scan["message"] = f"Pass {passes}: setting {sp:.4g} {x_unit}"

                pos = act.move(axis_key, sp, settle_s)
                if _abort.is_set():
                    break
                if origin is None:
                    origin = pos
                z = abs(pos - origin)

                m = det.measure(metric, wavelength, dwell_s)
                if m is None:
                    _scan["error"] = f"No reading from {det.name}"
                    break

                with _points_lock:
                    i = len(_points)
                    _points.append({
                        "i": i, "setpoint": round(sp, 4), "x": round(z, 5),
                        "pos": round(pos, 5),
                        "value": m["value"], "std": m.get("std", 0.0),
                        "vmin": m.get("vmin", m["value"]),
                        "vmax": m.get("vmax", m["value"]),
                        "unit": m.get("unit", ""), "n": m.get("n", 1),
                        "t": time.time(),
                    })
                    _spectra.append(m.get("spectrum"))
                _scan["done"] = i + 1

                if inter_s > 0 and not _abort.is_set():
                    time.sleep(inter_s)

            if _abort.is_set() or not repeat:
                break
    except Exception as exc:
        _scan["error"] = str(exc)
    finally:
        act.end_scan(); det.end_scan()
        _scan["running"] = False
        _scan["finished_at"] = time.time()
        _scan["message"] = ("Aborted." if _abort.is_set()
                            else _scan["error"] or "Scan complete.")


# ---------------------------------------------------------------------------
# Scan API
# ---------------------------------------------------------------------------

def _power_wavelength():
    try:
        if power.is_connected():
            return round(power._pm.wavelength, 1)
    except Exception:
        pass
    return None


def _active_axes(act):
    return [{**ax, "ready_action": act.ready_action_label(ax["key"])}
            for ax in act.axes()]


def _ensure_metric(det):
    """Coerce the global metric selection to one valid for the active detector
    (so a spectrometer defaults to integrated area even before the UI selects it)."""
    global _sel_metric, _sel_wavelength
    if det is None:
        return _sel_metric
    valid = [m["key"] for m in det.metrics()]
    if valid and _sel_metric not in valid:
        _sel_metric = valid[0]
        rng = det.wavelength_range()
        if rng and _sel_wavelength is None:
            _sel_wavelength = round((rng[0] + rng[1]) / 2, 1)
    return _sel_metric


def _active_detector_block(det):
    if det is None:
        return None
    return {
        "id": det.id, "name": det.name, "kind": det.kind,
        "metrics": det.metrics(),
        "wavelength_range": det.wavelength_range(),
        "acquisition": det.acquisition_state(),
    }


def _register_scan_routes(app: Flask) -> None:

    @app.route("/api/scan/config")
    def scan_config():
        act = actuators.manager.active()
        det = detectors.manager.active()
        metric = _ensure_metric(det)
        return jsonify({
            "actuators": [
                {"id": a.id, "name": a.name, "controller": a.controller,
                 "connected": a.connected(), "error": a.error()}
                for a in actuators.manager.all()
            ],
            "active": ({
                "id": act.id, "name": act.name, "controller": act.controller,
                "axes": _active_axes(act),
            } if act else None),
            "detectors": [
                {"id": d.id, "name": d.name, "kind": d.kind,
                 "connected": d.connected(), "error": d.error()}
                for d in detectors.manager.all()
            ],
            "active_detector": _active_detector_block(det),
            "metric": metric, "wavelength": _sel_wavelength,
            "power": {"connected": power.is_connected(),
                      "wavelength": _power_wavelength()},
        })

    @app.route("/api/scan/actuator", methods=["POST"])
    def scan_set_actuator():
        if _scan["running"]:
            return jsonify({"error": "Stop the scan first"}), 409
        ok, msg = actuators.manager.set_active(request.get_json(force=True).get("id", ""))
        return (jsonify({"ok": True, "active": msg}) if ok
                else (jsonify({"error": msg}), 409))

    @app.route("/api/scan/detector", methods=["POST"])
    def scan_set_detector():
        global _sel_metric, _sel_wavelength
        if _scan["running"]:
            return jsonify({"error": "Stop the scan first"}), 409
        ok, msg = detectors.manager.set_active(request.get_json(force=True).get("id", ""))
        if not ok:
            return jsonify({"error": msg}), 409
        det = detectors.manager.active()
        mets = det.metrics()
        _sel_metric = mets[0]["key"] if mets else "power"
        rng = det.wavelength_range()
        _sel_wavelength = round((rng[0] + rng[1]) / 2, 1) if rng else None
        return jsonify({"ok": True, "active": det.id})

    @app.route("/api/scan/detector/metric", methods=["POST"])
    def scan_set_metric():
        global _sel_metric, _sel_wavelength
        data = request.get_json(force=True)
        if "metric" in data:
            _sel_metric = str(data["metric"])
        if "wavelength" in data and data["wavelength"] is not None:
            _sel_wavelength = float(data["wavelength"])
        return jsonify({"ok": True, "metric": _sel_metric, "wavelength": _sel_wavelength})

    @app.route("/api/scan/detector/acquire", methods=["POST"])
    def scan_set_acquire():
        det = detectors.manager.active()
        if det is None:
            return jsonify({"error": "No detector connected"}), 409
        data = request.get_json(force=True)
        ok = det.set_acquisition(integration_ms=data.get("integration_ms"),
                                 averages=data.get("averages"))
        if not ok:
            return jsonify({"error": f"{det.name} has no settable acquisition"}), 400
        return jsonify({"ok": True})

    @app.route("/api/scan/prepare", methods=["POST"])
    def scan_prepare():
        act = actuators.manager.active()
        if act is None:
            return jsonify({"error": "No actuator connected"}), 409
        ok, msg = act.start_ready_action()
        return (jsonify({"ok": True, "message": msg}) if ok
                else (jsonify({"error": msg}), 409))

    @app.route("/api/scan/start", methods=["POST"])
    def scan_start():
        global _scan_thread, _points, _spectra
        with _scan_lock:
            if _scan["running"]:
                return jsonify({"error": "A scan is already running"}), 409

            act = actuators.manager.active()
            det = detectors.manager.active()
            if act is None or not act.connected():
                return jsonify({"error": "No actuator connected"}), 409
            if det is None or not det.connected():
                return jsonify({"error": "No detector connected"}), 409

            data = request.get_json(force=True)
            axis_key = data.get("axis", "")
            axis = next((a for a in act.axes() if a["key"] == axis_key), None)
            if axis is None:
                return jsonify({"error": f"Unknown axis '{axis_key}' for {act.name}"}), 400

            try:
                start = float(data["start"]); stop = float(data["stop"])
            except (KeyError, ValueError) as exc:
                return jsonify({"error": f"Bad start/stop: {exc}"}), 400

            steps = data.get("steps"); step_size = data.get("step_size")
            settle_s = max(0.0, float(data.get("settle_s", 1.0)))
            dwell_s = max(0.0, float(data.get("dwell_s", 0.3)))
            inter_s = max(0.0, float(data.get("inter_s", 0.0)))
            bidirectional = bool(data.get("bidirectional", False))
            repeat = bool(data.get("repeat", False))

            lo, hi, unit = axis["min"], axis["max"], axis["unit"]
            for nm, v in (("start", start), ("stop", stop)):
                if not lo - 1e-9 <= v <= hi + 1e-9:
                    return jsonify({"error": f"{nm} {v} out of range "
                                            f"{lo:g}–{hi:g} {unit}"}), 400

            act.prepare(axis_key)
            ready, why = act.check_ready(axis_key)
            if not ready:
                return jsonify({"error": why}), 409

            metric, wavelength = _ensure_metric(det), _sel_wavelength
            ymeta = det.y_meta(metric)
            cfg = {"axis": axis_key, "start": start, "stop": stop,
                   "steps": steps, "step_size": step_size,
                   "settle_s": settle_s, "dwell_s": dwell_s, "inter_s": inter_s,
                   "bidirectional": bidirectional, "repeat": repeat,
                   "detector": det.id, "metric": metric, "wavelength": wavelength}
            total = len(_setpoints(start, stop, steps, step_size, bidirectional))

            with _points_lock:
                _points = []; _spectra = []
            _scan.update({
                "running": True, "actuator": act.id, "axis": axis_key,
                "x_label": axis["label"], "x_unit": unit,
                "detector": det.id, "metric": metric, "wavelength": wavelength,
                "y_label": ymeta["label"], "y_unit": ymeta["unit"], "y_kind": ymeta["kind"],
                "total": total, "done": 0, "current_setpoint": None,
                "message": "Starting…", "error": "",
                "started_at": time.time(), "finished_at": None, "config": cfg,
                "spectra_wavelengths": (det.wavelengths()
                                        if det.kind == "spectrometer" else None),
            })
            _abort.clear()
            _scan_thread = threading.Thread(
                target=_run_scan,
                args=(cfg, act, axis_key, unit, det, metric, wavelength),
                daemon=True, name="scan")
            _scan_thread.start()
            return jsonify({"ok": True, "total": total})

    @app.route("/api/scan/stop", methods=["POST"])
    def scan_stop():
        _abort.set()
        return jsonify({"ok": True})

    @app.route("/api/scan/status")
    def scan_status():
        with _points_lock:
            n = len(_points)
        out = dict(_scan)
        out.pop("spectra_wavelengths", None)
        out["n_points"] = n
        act = actuators.manager.active()
        det = detectors.manager.active()
        out["actuator_status"] = act.status() if act else {"connected": False}
        out["ready_action"] = act.ready_action_state() if act else {}
        if det is not None:
            metric = _ensure_metric(det)
            ym = det.y_meta(metric)
            out["detector_status"] = {
                "id": det.id, "name": det.name, "kind": det.kind,
                "connected": det.connected(),
                "value": det.live(metric, _sel_wavelength),
                "unit": ym["unit"], "y_kind": ym["kind"], "label": ym["label"],
            }
        else:
            out["detector_status"] = {"connected": False}
        return jsonify(out)

    @app.route("/api/scan/data")
    def scan_data():
        with _points_lock:
            pts = list(_points)
        return jsonify({
            "x": [p["x"] for p in pts],
            "value": [p["value"] for p in pts],
            "setpoint": [p["setpoint"] for p in pts],
            "std": [p["std"] for p in pts],
            "x_label": _scan["x_label"], "x_unit": _scan["x_unit"],
            "y_label": _scan["y_label"], "y_unit": _scan["y_unit"],
            "y_kind": _scan["y_kind"],
            "points": pts, "running": _scan["running"],
        })

    @app.route("/api/scan/download")
    def scan_download():
        with _points_lock:
            pts = list(_points)
        cfg = _scan.get("config", {})
        is_power = _scan.get("y_kind") == "power"
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow([f"# VSL scan  actuator={_scan.get('actuator')}",
                    f"axis={_scan.get('axis')}",
                    f"detector={_scan.get('detector')}",
                    f"metric={_scan.get('metric')}"])
        w.writerow([f"# config: {cfg}"])
        x_unit = _scan.get("x_unit", "")
        cols = ["index", "setpoint", _scan.get("x_label", "x"),
                f"actual_position_{x_unit}",
                f"signal_{_scan.get('y_unit', '')}", "std", "min", "max",
                "n_samples", "epoch_s"]
        if is_power:
            cols.insert(5, "power_dBm")
        w.writerow(cols)
        for p in pts:
            v = p["value"]
            pos = p.get("pos", p["x"])
            row = [p["i"], f"{p['setpoint']:.5f}", f"{p['x']:.5f}", f"{pos:.5f}",
                   f"{v:.9e}", f"{p['std']:.9e}", f"{p['vmin']:.9e}",
                   f"{p['vmax']:.9e}", p["n"], f"{p['t']:.3f}"]
            if is_power:
                row.insert(5, "" if v <= 0 else f"{10.0 * math.log10(v / 1e-3):.4f}")
            w.writerow(row)
        fname = time.strftime("scan_%Y%m%d_%H%M%S.csv")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

    @app.route("/api/scan/bands", methods=["POST"])
    def scan_bands():
        """
        Post-process the captured spectra: for each user-chosen wavelength band
        [lo, hi], integrate the area (∫ I dλ) at every scan point. Returns one
        curve per band (band area vs. actuator position) for overlay on the plot.
        """
        wl = _scan.get("spectra_wavelengths")
        with _points_lock:
            specs = list(_spectra)
            pts = list(_points)
        if not wl or not any(specs):
            return jsonify({"error": "No spectra captured — run a scan with a "
                                     "spectrometer detector first"}), 404
        bands = request.get_json(force=True).get("bands", [])
        x = [p["x"] for p in pts]
        out = []
        for b in bands:
            try:
                lo, hi = float(b[0]), float(b[1])
            except (TypeError, ValueError, IndexError):
                continue
            if hi < lo:
                lo, hi = hi, lo
            vals = [(_trapz_area(s, wl, lo, hi) if s else None) for s in specs]
            out.append({"lo": round(lo, 3), "hi": round(hi, 3),
                        "label": f"{lo:g}–{hi:g} nm", "values": vals})
        return jsonify({"x": x, "x_label": _scan.get("x_label"),
                        "x_unit": _scan.get("x_unit"),
                        "unit": "counts·nm", "bands": out})

    @app.route("/api/scan/bands/download", methods=["POST"])
    def scan_bands_download():
        wl = _scan.get("spectra_wavelengths")
        with _points_lock:
            specs = list(_spectra)
            pts = list(_points)
        if not wl or not any(specs):
            return jsonify({"error": "No spectra captured"}), 404
        bands = request.get_json(force=True).get("bands", [])
        cleaned = []
        for b in bands:
            try:
                lo, hi = float(b[0]), float(b[1])
            except (TypeError, ValueError, IndexError):
                continue
            cleaned.append((min(lo, hi), max(lo, hi)))
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow([f"# VSL band-integrated areas (counts·nm)  "
                    f"detector={_scan.get('detector')}  axis={_scan.get('axis')}"])
        w.writerow(["index", "setpoint", _scan.get("x_label", "x")]
                   + [f"area_{lo:g}-{hi:g}nm" for lo, hi in cleaned])
        for p, s in zip(pts, specs):
            row = [p["i"], f"{p['setpoint']:.5f}", f"{p['x']:.5f}"]
            for lo, hi in cleaned:
                row.append(f"{_trapz_area(s, wl, lo, hi):.6e}" if s else "")
            w.writerow(row)
        fname = time.strftime("bands_%Y%m%d_%H%M%S.csv")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

    @app.route("/api/scan/spectra")
    def scan_spectra():
        """Full spectra as a wavelength×position matrix (spectrometer scans only)."""
        wl = _scan.get("spectra_wavelengths")
        with _points_lock:
            specs = [s for s in _spectra]
            pts = list(_points)
        if not wl or not any(specs):
            return jsonify({"error": "No spectra captured (detector was not a "
                                     "spectrometer, or no scan run yet)"}), 404
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow([f"# VSL spectra  detector={_scan.get('detector')}  "
                    f"axis={_scan.get('axis')}"])
        # Header: wavelength_nm, then one column per scan point (its x position).
        w.writerow(["wavelength_nm"] + [f"{p['x']:.5f}" for p in pts])
        for pix in range(len(wl)):
            row = [f"{wl[pix]:.4f}"]
            for s in specs:
                row.append(f"{s[pix]:.3f}" if s and pix < len(s) else "")
            w.writerow(row)
        fname = time.strftime("spectra_%Y%m%d_%H%M%S.csv")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

    @app.route("/api/scan/profile")
    def scan_profile():
        """
        Knife-edge beam-profile post-processor: differentiate the scan (the
        greatest rate of change = edge centre) and fit a Gaussian to that
        derivative. Returns the derivative curve, the max-slope point, and the
        fitted Gaussian (centre / σ / FWHM in the actuator's units).
        """
        with _points_lock:
            pts = list(_points)
        xs = [p["x"] for p in pts]
        ys = [p["value"] for p in pts]
        res = _gaussian_profile(xs, ys)
        if res is None:
            return jsonify({"error": "Need ≥3 scan points for a profile"}), 400
        res.update({"x_label": _scan.get("x_label"), "x_unit": _scan.get("x_unit"),
                    "y_label": _scan.get("y_label"), "y_unit": _scan.get("y_unit")})
        return jsonify(res)

    @app.route("/api/scan/profile/download")
    def scan_profile_download():
        with _points_lock:
            pts = list(_points)
        res = _gaussian_profile([p["x"] for p in pts], [p["value"] for p in pts])
        if res is None:
            return jsonify({"error": "Need ≥3 scan points"}), 400
        g = res["gaussian"] or {}
        xu = _scan.get("x_unit", "")
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow([f"# VSL knife-edge beam profile  detector={_scan.get('detector')}"
                    f"  axis={_scan.get('axis')}"])
        w.writerow([f"# max_slope_at={res['max_slope']['x']} {xu}",
                    f"slope={res['max_slope']['slope']:.6e}"])
        if g:
            w.writerow([f"# gaussian center={g['center']} {xu}",
                        f"sigma={g['sigma']} {xu}", f"FWHM={g['fwhm']} {xu}",
                        f"amplitude={g['amplitude']:.6e}"])
        else:
            w.writerow(["# gaussian fit: failed (derivative not concave-down)"])
        w.writerow([f"position_{xu}", "d_signal_d_x"])
        for x, y in zip(res["deriv_x"], res["deriv_y"]):
            w.writerow([f"{x:.6f}", f"{y:.9e}"])
        fname = time.strftime("profile_%Y%m%d_%H%M%S.csv")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

    @app.route("/api/scan/vsl")
    def scan_vsl():
        with _points_lock:
            pts = list(_points)
        if len(pts) < 5:
            return jsonify({"error": "Need ≥5 scan points for VSL gain analysis"}), 400
        xs = [p["x"] for p in pts]
        ys = [p["value"] for p in pts]
        z0_raw = request.args.get("z0")
        z0 = float(z0_raw) if z0_raw else None
        res = _vsl_gain(xs, ys, z0=z0)
        if res is None:
            return jsonify({"error": "VSL fit failed — verify data shows a rising "
                                     "ASE-like curve before running gain analysis"}), 400
        # Instantaneous gain uses the fitted A_sp for consistency (De Giorgi & Anni)
        res["inst"] = _vsl_instantaneous(xs, ys, z0=z0, a_sp=res.get("A_sp"))
        # Full-curve gain-saturation model dI/dz = A_sp + g0·I/(1+I/I_s)
        res["sat"] = _vsl_saturated(xs, ys, z0=z0, seed=res)
        res.update({
            "x_label": _scan.get("x_label"), "x_unit": _scan.get("x_unit"),
            "y_label": _scan.get("y_label"), "y_unit": _scan.get("y_unit"),
        })
        return jsonify(res)

    @app.route("/api/scan/vsl/instant")
    def scan_vsl_instant():
        """
        Instantaneous gain g_inst(z) = (dI/dz − A_sp)/I(z) — cheap enough to
        poll live while the scan is still acquiring (De Giorgi & Anni).
        """
        with _points_lock:
            pts = list(_points)
        if len(pts) < 5:
            return jsonify({"error": "Need ≥5 scan points"}), 400
        z0_raw = request.args.get("z0")
        res = _vsl_instantaneous([p["x"] for p in pts], [p["value"] for p in pts],
                                 z0=float(z0_raw) if z0_raw else None)
        if res is None:
            return jsonify({"error": "Not enough usable points yet"}), 400
        res.update({"x_unit": _scan.get("x_unit"), "running": _scan["running"]})
        return jsonify(res)

    @app.route("/api/scan/vsl/download")
    def scan_vsl_download():
        with _points_lock:
            pts = list(_points)
        if len(pts) < 5:
            return jsonify({"error": "Need ≥5 scan points"}), 400
        xs = [p["x"] for p in pts]
        ys = [p["value"] for p in pts]
        z0_raw = request.args.get("z0")
        z0 = float(z0_raw) if z0_raw else None
        res = _vsl_gain(xs, ys, z0=z0)
        if res is None:
            return jsonify({"error": "VSL fit failed"}), 400
        xu = _scan.get("x_unit", "")
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow([f"# VSL gain analysis  detector={_scan.get('detector')}"
                    f"  axis={_scan.get('axis')}"])
        w.writerow([f"# best_g={res['best_g']:.6f} 1/{xu}"
                    f"  CI95=±{res['best_ci95']:.6f}"
                    f"  z_sat={res['z_sat']:.4f} {xu}"
                    f"  A_sp={res['A_sp']:.6e}"])
        sat = _vsl_saturated(xs, ys, z0=z0, seed=res)
        if sat:
            zsat = f"{sat['z_sat']:.4f} {xu}" if sat["z_sat"] is not None \
                   else "not reached"
            w.writerow([f"# saturation model dI/dz=A_sp+g0*I/(1+I/I_s):"
                        f"  g0={sat['g0']:.6f} 1/{xu}"
                        f"  I_s={sat['I_s']:.6e}"
                        f"  A_sp={sat['A_sp']:.6e}"
                        f"  z_sat(I=I_s)={zsat}"
                        f"  R2={sat['r2']}"])
        w.writerow([f"z_fit_{xu}", f"g_1per{xu}", "ci95_half", "A_sp", "n_pts"])
        for s in res["scan"]:
            ci = f"{s['ci95']:.6f}" if s["ci95"] is not None else ""
            w.writerow([f"{s['z_fit']:.6f}", f"{s['g']:.6f}", ci,
                        f"{s['A_sp']:.6e}", s["n_pts"]])
        inst = _vsl_instantaneous(xs, ys, z0=z0, a_sp=res.get("A_sp"))
        if inst:
            w.writerow([])
            w.writerow([f"# instantaneous gain g_inst(z)=(dI/dz - A_sp)/I(z)"
                        f"  (De Giorgi & Anni, VSL pitfalls)"
                        f"  A_sp={inst['A_sp']:.6e}"
                        f"  g0_plateau={inst['g0']:.6f} 1/{xu}"])
            w.writerow([f"z_{xu}", f"g_inst_1per{xu}"])
            for z, g in zip(inst["z"], inst["g_inst"]):
                w.writerow([f"{z:.6f}", f"{g:.6f}"])
        fname = time.strftime("vsl_gain_%Y%m%d_%H%M%S.csv")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

    # -----------------------------------------------------------------------
    # Matplotlib figure exports (?format=png|svg|pdf)
    # -----------------------------------------------------------------------

    def _scan_meta():
        return {"x_label": _scan.get("x_label") or "Position",
                "x_unit": _scan.get("x_unit") or "",
                "y_label": _scan.get("y_label") or "Signal",
                "y_unit": _scan.get("y_unit") or "",
                "title": (f"VSL scan — {_scan.get('actuator') or 'actuator'}"
                          f" / {_scan.get('detector') or 'detector'}")}

    def _parse_bands_arg():
        """?bands=400:500,600:700 → [(lo, hi), …]"""
        raw = request.args.get("bands", "")
        out = []
        for part in raw.split(","):
            if ":" not in part:
                continue
            try:
                lo, hi = (float(v) for v in part.split(":", 1))
            except ValueError:
                continue
            out.append((min(lo, hi), max(lo, hi)))
        return out

    @app.route("/api/scan/plot")
    def scan_plot():
        """
        Scan figure rendered with matplotlib. Optional overlays mirroring the
        web plot: &bands=lo:hi,lo:hi  &profile=1  &vsl=1[&z0=…]
        """
        if not plots.MPL_OK:
            return plots.unavailable()
        with _points_lock:
            pts = list(_points)
            specs = list(_spectra)
        if not pts:
            return jsonify({"error": "No scan data yet"}), 404

        xs = [p["x"] for p in pts]
        ys = [p["value"] for p in pts]

        band_curves = []
        wl = _scan.get("spectra_wavelengths")
        if wl and any(specs):
            for lo, hi in _parse_bands_arg():
                vals = [(_trapz_area(s, wl, lo, hi) if s else None) for s in specs]
                band_curves.append({"x": xs, "values": vals,
                                    "label": f"{lo:g}–{hi:g} nm"})

        profile = (_gaussian_profile(xs, ys)
                   if request.args.get("profile") else None)

        vsl = None
        if request.args.get("vsl"):
            z0_raw = request.args.get("z0")
            z0v = float(z0_raw) if z0_raw else None
            vsl = _vsl_gain(xs, ys, z0=z0v)
            if vsl:
                vsl["sat"] = _vsl_saturated(xs, ys, z0=z0v, seed=vsl)

        fig = plots.scan_figure(pts, _scan_meta(),
                                bands=band_curves, profile=profile, vsl=vsl)
        return plots.respond(fig, "scan")

    @app.route("/api/scan/vsl/plot")
    def scan_vsl_plot():
        """VSL gain cutoff-scan figure (g vs z_fit with 95 % CI band)."""
        if not plots.MPL_OK:
            return plots.unavailable()
        with _points_lock:
            pts = list(_points)
        if len(pts) < 5:
            return jsonify({"error": "Need ≥5 scan points"}), 400
        z0_raw = request.args.get("z0")
        xs = [p["x"] for p in pts]
        ys = [p["value"] for p in pts]
        z0 = float(z0_raw) if z0_raw else None
        res = _vsl_gain(xs, ys, z0=z0)
        if res is None:
            return jsonify({"error": "VSL fit failed"}), 400
        res["inst"] = _vsl_instantaneous(xs, ys, z0=z0, a_sp=res.get("A_sp"))
        res["sat"] = _vsl_saturated(xs, ys, z0=z0, seed=res)
        fig = plots.vsl_figure(res, _scan_meta())
        return plots.respond(fig, "vsl_gain")

    @app.route("/api/scan/spectra/plot")
    def scan_spectra_plot():
        """Wavelength × position intensity map of the captured spectra."""
        if not plots.MPL_OK:
            return plots.unavailable()
        wl = _scan.get("spectra_wavelengths")
        with _points_lock:
            specs = list(_spectra)
            pts = list(_points)
        if not wl or not any(specs):
            return jsonify({"error": "No spectra captured"}), 404
        fig = plots.spectra_map_figure(wl, specs, [p["x"] for p in pts],
                                       _scan_meta())
        return plots.respond(fig, "spectra_map")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    # Refuse manual stage commands while a scan owns the actuator
    smc.external_busy = (lambda: "Scan running — stop it before moving the "
                                 "stage manually" if _scan["running"] else "")
    app.register_blueprint(piezo.piezo_bp, url_prefix="/api/piezo")
    app.register_blueprint(power.power_bp, url_prefix="/api/power")
    app.register_blueprint(smc.smc_bp, url_prefix="/api/smc")
    app.register_blueprint(hr.hr_bp, url_prefix="/api/spec")
    app.register_blueprint(av.av_bp, url_prefix="/api/avantes")
    _register_scan_routes(app)

    # The Scan page is the whole application — the device blueprints above are
    # kept only for their APIs (the scan engine and UI drive them directly).
    @app.route("/")
    def scan_page():
        return render_template("scan.html")

    return app


if __name__ == "__main__":
    actuators.manager.detect()   # auto-detect attached actuator(s)
    detectors.manager.detect()   # auto-detect attached detector(s)
    create_app().run(host="0.0.0.0", port=5050, threaded=True, debug=False)

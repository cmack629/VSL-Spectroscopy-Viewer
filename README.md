# VSL Scan

VSL Scan is a local Flask application for coordinated actuator and optical-signal scans, built around a variable-stripe-length (VSL) optical-gain measurement. One merged **Scan app** (port 5050) can drive any connected actuator and any connected detector, including both spectrometers, through a uniform interface. Five **standalone single-instrument apps** (ports 5001 through 5005) expose each device's full instrument-specific controls (integration time, averaging, zeroing, homing, wavelength correction, and more) independently, sharing the same underlying drivers.

The app can start without instruments connected. Unavailable devices are shown as not connected rather than preventing the server from running. See [How Device Awareness Works](#how-device-awareness-works) for details.

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Setup](#setup)
- [Run](#run)
- [How Device Awareness Works](#how-device-awareness-works)
- [Basic Workflow](#basic-workflow)
- [Detector Metrics](#detector-metrics)
- [VSL Gain Analysis](#vsl-gain-analysis)
- [Beam Profile Analysis](#beam-profile-analysis)
- [Live Plots and UI Behavior](#live-plots-and-ui-behavior)
- [Standalone Instrument Apps](#standalone-instrument-apps)
- [Instrument Reference](#instrument-reference)
- [Supported Hardware](#supported-hardware)
- [Hardware Notes](#hardware-notes)
- [Data Export Reference](#data-export-reference)
- [API Quick Reference](#api-quick-reference)
- [Project Layout](#project-layout)
- [Development](#development)

## Features

- Coordinate step-and-measure scans with configurable start, stop, step count, settle time, dwell time, bidirectional operation, and repeat mode.
- Use a Thorlabs BPC301 / DRV517 piezo in position (µm) or voltage (V) mode, or a Newport SMC100CC / LTA-HS stage in position (mm) mode.
- Measure with a Thorlabs PM400 power meter, Ocean Optics HR4000, or Avantes AvaSpec spectrometer: all three are selectable directly from the merged Scan app, not just the power meter.
- Choose optical power, summed spectral counts, spectral peak intensity, or intensity at a selected wavelength or band.
- Take multiple independent samples per scan point with automatic median/MAD outlier rejection (see [Detector Metrics](#detector-metrics)).
- Capture a complete spectrum at every scan step when using a spectrometer, with per-band count sums computed in post-processing.
- Run VSL optical-gain analysis on a completed scan: small-signal-gain and gain-saturation cutoff scans, live instantaneous gain, and spectrum-aware band analysis (see [VSL Gain Analysis](#vsl-gain-analysis)).
- Fit a Gaussian beam profile from a knife-edge scan (see [Beam Profile Analysis](#beam-profile-analysis)).
- Download every available result (data CSVs, analysis CSVs, and rendered plots) as one timestamped ZIP from any app page.
- Run any instrument standalone, with its own full control set and live plot, on its own port (see [Standalone Instrument Apps](#standalone-instrument-apps)).

## Requirements

- Python 3.10 or later.
- macOS, Linux, or Windows.
- Optional: one supported actuator and detector connected to the host.

On macOS, the hardware drivers require `libusb`; the setup script installs it with Homebrew when available. On Linux, the setup script installs the needed `libusb` development package and `pkg-config` on Debian/Ubuntu systems. See [Hardware Notes](#hardware-notes) for Windows raw-USB devices and the Avantes SDK library.

## Setup

From the repository root, use the platform setup script to create `.venv` and install dependencies.

### macOS and Linux

```bash
./setup.sh
source .venv/bin/activate
```

To select a particular Python interpreter, set `PYTHON` before running the script:

```bash
PYTHON=python3.11 ./setup.sh
```

### Windows PowerShell

```powershell
.\setup.ps1
.venv\Scripts\Activate.ps1
python run.py
```

If PowerShell blocks the setup or activation scripts, allow locally created
scripts for the current user, then reopen PowerShell and run the commands above:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

The application is available at [http://localhost:5050](http://localhost:5050).

## Run

Start the application from the repository root:

```bash
python run.py
```

Open [http://localhost:5050](http://localhost:5050). The server listens on all interfaces at port `5050`.

For automatic reload while editing Python or templates, set `FLASK_DEBUG=1` before launching:

```bash
FLASK_DEBUG=1 python run.py
```

## How Device Awareness Works

"Awareness" is the app's hardware auto-detection: it probes every supported instrument at startup and picks a sensible default, without needing hardware physically present to boot.

**Uniform interface.** `server/actuators.py` and `server/detectors.py` each define a small abstract base class (`Actuator`, `Detector`) with the same shape regardless of vendor: `connected()`, `axes()`/`metrics()`, `move()`/`measure()`, `status()`. A thin adapter class wraps each vendor backend (`PiezoActuator`, `SMC100Actuator`, `PowerDetector`, `SpectrometerDetector`, the last one instantiated twice, once for HR4000 and once for Avantes, since they share one adapter). The scan engine (`server/scanner.py`) only ever talks to these interfaces, never to a specific driver.

**Detection.** A manager (`ActuatorManager`, `DetectorManager`) owns a fixed, priority-ordered list of adapters and calls `detect()` once at process startup (`run.py`, guarded so Flask's debug-mode reloader does not probe hardware twice). `detect()`:

1. Calls each backend's own `_init_controller()` / `_init_monitor()` / `_init_spectrometer()` probe, each wrapped in its own `try`/`except`, so one instrument failing to open (missing library, unplugged, wrong permissions) never prevents the others from being probed.
2. Builds the list of devices that report `connected() == True`.
3. Picks the active device: if there is currently no active device, or the previously active one just disconnected, the new active device is the first device, in fixed registration order, that is actually connected. Registration order is a constructor-level constant, not configurable at runtime:
   - Actuators: **Piezo before SMC100** (`server/actuators.py`).
   - Detectors: **PM400 before HR4000 before Avantes** (`server/detectors.py`).
4. If nothing is connected, the active device is `None` and the app keeps running; actions that need a device (starting a scan, moving an axis) return an error instead of crashing.

**All three detectors are wired into the merged Scan app.** `server/detectors.py` registers `PowerDetector`, and a `SpectrometerDetector` for *both* HR4000 and Avantes, into the same `DetectorManager` used by the merged Scan page. HR4000 and Avantes are fully selectable from the Scan page's detector switcher exactly like the PM400; you do not need to run their standalone apps to use them in a scan. (Two module docstrings, in `server/hr4000_app.py` and `server/avantes_app.py`, previously described these as not wired into the merged scanner; that was stale. The registration in `detectors.py` says otherwise.)

**Manual selection.** When more than one actuator or detector is connected, the Scan page shows a device selector. Picking one calls `set_active(id)`, which refuses (returns an error) if the requested device is not currently `connected()`. You can switch between live devices, but never force-select a disconnected one.

**Reconnecting.** Every standalone app exposes a `POST .../reconnect` route that drops the current handle and re-runs just that device's init, without restarting the Flask process or affecting other instruments.

**What "not connected" looks like.** The status LED goes red and the device's `error()` string (the exception message from its failed probe) is surfaced in the UI, for example "No FTDI device found" or "libavs not found." The rest of the app keeps working.

## Basic Workflow

1. Connect whichever actuator and detector(s) you are using, then start the application (`python run.py`). It probes all supported devices (see [How Device Awareness Works](#how-device-awareness-works)) and activates the first connected actuator and the first connected detector.
2. Open the Scan page. If more than one actuator or detector is connected, pick the one you want from its selector.
3. Prepare the actuator: **Zero** and close the loop for a piezo position scan, or **Home** the SMC100 stage. The **Prepare** button runs whichever of these the active axis needs.
4. Select the actuator axis, detector metric (and wavelength/band if applicable, see [Detector Metrics](#detector-metrics)), scan limits, number of points, and settle time. Dwell time is set manually for a power meter (it is the averaging window); for a spectrometer it auto-fills from Integration × Averages (uncheck **Auto** to override; see the dwell formula in [Detector Metrics](#detector-metrics)). Optionally raise **Samples/pt** above 1 to average multiple independent reads per point with outlier rejection.
5. Start the scan. The live plot and readouts update as each measurement is recorded; the actuator position is recorded as stripe length `z = |pos - origin|`, zeroed to the scan's own starting position.
6. Run any post-processing:
   - **Wavelength bands**, a different feature from VSL's spectral band: this overlays one or more summed-counts-vs-position curves for arbitrary wavelength ranges, independent of gain fitting.
   - **Beam profile**, which fits a Gaussian to the knife-edge derivative.
   - **VSL gain** (see [VSL Gain Analysis](#vsl-gain-analysis)); for spectrometer scans this defaults to the spectrum-aware band fit, with a manual band override.

   Then use **Download ZIP** to save the signal CSV, spectra CSV, analysis CSVs, and rendered PNG plots in one archive.

## Detector Metrics

How the scalar signal value plotted at each scan point is actually computed, and how per-point averaging works. These settings matter both for signal-to-noise and for the VSL fits ([VSL Gain Analysis](#vsl-gain-analysis)) that consume them.

### Power meter (PM400)

A background thread samples the PM400 continuously at 10 Hz. For a scan point, the app averages every sample taken during that point's `dwell_s` window:

- `mean_w = fmean(samples)`
- `std_w = pstdev(samples)` (population standard deviation; `0` if only one sample)
- Falls back to a single most-recent reading if the dwell was too short to catch a fresh sample.

The `power_dBm` value shown in the UI and written as a CSV column is derived, not measured: `power_dBm = 10 * log10(power_w / 1e-3)` (blank/negative infinity if `power_w <= 0`).

### Spectrometer (HR4000 / Avantes, identical logic)

Three selectable metrics:

| Metric | Meaning | Formula |
| --- | --- | --- |
| `area` ("Summed counts") | Total signal across the whole spectrum | Plain **sum** of every pixel's count value (not a wavelength-weighted, trapezoidal, integral), so its unit is counts, not counts times nm |
| `peak` | Brightest pixel | Device-reported peak intensity, or `max(intensities)` if not reported |
| `at_wavelength` | Intensity at or near a chosen wavelength | If band width = 0: nearest-pixel lookup. If band width Δλ > 0: sum of counts for every pixel with `λ - Δλ/2 <= wavelength <= λ + Δλ/2` |

Acquisition (integration time, averages) is set once via **Integration (ms)** / **Averages**, applied to the device directly. This is a *device-level* setting, distinct from the scan's own per-point `samples_per_point` averaging below.

### Per-point averaging and outlier rejection

When **Samples/pt** (`samples_per_point`, default 5) is greater than 1, each scan point takes that many independent, *full-dwell* reads (each one waits out the entire `dwell_s`, not a fraction of it; this is deliberate, so a spectrometer always sees a fully settled frame and the PM400's own internal averaging window is never shortened). If **Reject outliers (3σ)** is checked (default on, `reject_outliers=True`) and there are at least 3 samples, robust statistics discard spikes before the mean is taken:

- `med = median(samples)`
- `mad = median(|v - med| for v in samples)`
- `robust_std = 1.4826 * mad` (the standard MAD-to-sigma conversion constant for Gaussian noise)
- Keep only samples with `|v - med| <= outlier_sigma * robust_std` (`outlier_sigma` default `3.0`), but never fewer than 2 kept samples.

The point's final mean, standard deviation, min, and max are computed from the kept samples. For a spectrometer, the kept reads' spectra are averaged pixel by pixel to produce the point's stored spectrum.

### Stripe length (`z`) vs. raw position

The actuator's raw position is recorded alongside, but the x-axis used for VSL analysis is `z = |position - origin|`, where `origin` is the actuator's actual position at the scan's very first setpoint. This guarantees `z` starts at 0 and grows monotonically no matter which direction the sweep runs. That is a real requirement for a knife-edge/VSL measurement, where a raw coordinate could run backward depending on how the stage is mounted.

### Dwell-time auto-fill (spectrometer)

When **Auto** is checked, dwell time is derived from the spectrometer's own acquisition settings, with a 15% margin for per-frame USB/dark-read overhead:

```
dwell_s = max(0.1, ceil(integration_ms * averages / 1000 * 1.15 * 10) / 10)
```

rounded up to the nearest 0.1 s. Editing the dwell field directly auto-unchecks **Auto**.

### Config defaults

| Parameter | Default | Notes |
| --- | --- | --- |
| `samples_per_point` | 5 | minimum 1 |
| `outlier_sigma` | 3.0 | minimum 0.5 |
| `reject_outliers` | on | |
| `settle_s` | 1.0 s | pause after the actuator move, before measuring |
| `dwell_s` | 0.3 s | per-sample measurement window |
| `inter_s` | 0.0 s | extra pause between points after `dwell_s` |

## VSL Gain Analysis

The Scan page analyzes a variable-stripe-length (VSL) measurement, ASE intensity `I` versus pump-stripe length `z`, using the methodology of Alvarado-Leaños et al., *Adv. Optical Mater.* **9**, 2001773 (2021) ([doi:10.1002/adom.202001773](https://doi.org/10.1002/adom.202001773)), which builds on the classic Shaklee-Leheny method and the applicability analysis of Dal Negro et al., *Opt. Commun.* **229**, 337 (2004). Every fit below runs **server-side** in Python; the browser only ever polls the results and draws them (see [Live Plots and UI Behavior](#live-plots-and-ui-behavior)).

### Onset detection (z0)

Motorized knife-edge scans often carry dead travel before the stripe reaches the sample edge. Auto-detection takes the shortest-stripe quarter of the data as a baseline window: `baseline = median`, `noise = 1.4826 * median(|v - baseline|)` (MAD-based). The onset threshold is `baseline + max(5 * noise, 0.5% * (max(I) - baseline))`, a 5-sigma requirement with a small floor for near-noiseless data, kept tiny deliberately since ASE rises over decades and even a few-percent threshold would already bias the fit. The first run of 3 consecutive points at or above threshold marks onset, then the detector walks backward while points stay above a looser `baseline + 2 * noise` line, to catch the true early rise. Override with the **Fit start z₀** field; leaving it blank uses this auto-detection.

### Background subtraction

A constant background (stray light, dark counts, unpumped PL) is subtracted before any fit: `baseline = median` of the shortest-stripe quarter of the (already z0-filtered) data. Confirm this estimate against a pump-blocked measurement if precision matters; the app has no way to know whether a low-z reading is real, small ASE, or background only.

### Small-signal-gain (SSG) cutoff scan

Model: `I(z) = A_sp * (e^(gz) - 1) / g`. For each candidate cutoff `z_fit`, the app refits the entire window `[z0, z_fit]` from scratch:

- `A_sp` is eliminated analytically for a given `g` (it is a linear least-squares problem once `g` is fixed).
- `g` itself is found by **golden-section search** (derivative-free) over `[1e-4, min(10*g_rough, 100, 354/z_max)]`, where `g_rough` is a crude `ln(I_max/I_min)/z_span` estimate and the upper bound keeps `e^(gz)` from overflowing double precision. A fit is rejected if the optimum lands within 0.1% of either search bound (a boundary hit, not a real optimum).
- The 95% confidence interval on `g` comes from the fit's Jacobian and Student's t (`t95(df)`, a hardcoded lookup table with linear interpolation, no scipy dependency), using the per-point measurement standard deviations as inverse-variance weights when available, or plain ordinary-least-squares residuals otherwise.
- **The reported "best" gain is the cutoff with the minimum CI95 across all scanned cutoffs, not the full-range fit.** Where that minimum falls (`z_sat` in the output) is a statistical convenience marker, not a physical saturation onset.

### Gain-saturation (Gsat) model

Homogeneous-saturation model (De Giorgi and Anni): `dI/dz = A_sp + g0 * I / (1 + I/I_s)`, integrated numerically with RK4 (at least 40 substeps across the full z range) since it has no closed form. The three parameters (`A_sp`, `g0`, `I_s`) are fit in log space with a from-scratch Nelder-Mead simplex implementation (positivity is automatic since the optimizer works in log space), seeded from the SSG fit's `(A_sp, g)` and `I_s ≈ 0.5 * max(I)`. The 95% confidence interval on `g0` uses the delta method: the Jacobian of residuals with respect to the log-parameters is built by forward finite differences (step `1e-4`), and `g0`'s variance is scaled by `g0` squared to convert from `ln(g0)` back to `g0`. A confidence interval is rejected outright if it comes out larger than `50 * |g0|`, treated as an ill-conditioned fit rather than reported as a wildly uncertain number. The cutoff scan (analogous to SSG's) is *warm-started* from the previous cutoff's fit (Nelder-Mead needs a decent starting simplex), and a candidate is rejected if `g0 * z_span > 50` ("saturates instantly," which this model cannot meaningfully distinguish from "saturates even faster").

### Instantaneous gain

Local gain `g_inst(z)`, De Giorgi/Anni style, computed as the **log-derivative** form:

```
g_inst(z) = d(ln I)/dz - A_sp / I(z)
```

rather than differencing `I` directly: the log-derivative is exact for pure exponential growth at any step size, whereas a direct `dI/dz` difference biases `g` low by roughly `g * Δz / 2`. `I` is lightly smoothed (3-point moving average) before differencing; `A_sp` is estimated (if not already fixed from an SSG fit) by fitting `ln(slope)` versus `z` over the earliest few points and extrapolating to `z=0`. Points below 2% of the max signal are skipped (differentiation noise dominates there). The reported plateau value `g0` is the **maximum of a 5-point moving median** of `g_inst(z)`, chosen because `g_inst` climbs out of the noise at small z and only falls once saturation sets in, so the highest stable stretch is the small-signal plateau.

This is the value that updates live during acquisition: the browser polls `GET /api/scan/vsl/instant` every 1.2 s (only once at least 5 points exist) and renders whatever the server just computed; no gain math happens in JavaScript.

### Spectrum-aware band analysis

For a spectrometer scan, gain fitting can use the actual emission feature that grows with stripe length, rather than total counts (which can be dominated by a fixed background across the whole spectrum). Needs at least 8 scan points with captured spectra.

- **Baseline/high spectra**: median spectrum over the first quarter (roughly) of points (shortest stripes) versus the last quarter (longest stripes); `change[pixel] = high - baseline`.
- **Automatic band**: plus or minus 5 nm around the pixel with the largest positive change.
- **Manual band**: enter explicit wavelength limits in the **Manual VSL band (nm)** field (both or neither; one alone is a validation error).
- **Per-point net signal**: sum counts in the band, subtract the median of the first quarter (roughly) of points as background.
- **Fit window**: the longest *contiguous* run of points whose net signal falls between 5% and 75% of the peak net signal, with a minimum run length of 6 points, deliberately the longest run (not simply first-to-last match) so a single noise spike near the flat baseline cannot stretch the window across the whole scan.
- **Fit**: ordinary least squares of `ln(net signal)` versus `z` over that window, giving gain, 95% confidence interval, and R-squared.
- **Warnings** are attached to the result whenever R-squared is below 0.9 ("not strongly exponential, provisional"), the fit gain is zero or negative, or as a standing reminder that the baseline is a median-of-shortest-stripes estimate that should be checked against a pump-blocked measurement.

This is the default mode (**Spectrum-aware analysis**, checked by default) whenever the active detector is a spectrometer; unchecking it falls back to the scalar SSG/Gsat/instantaneous fits above, run on whatever scalar metric is currently selected.

### Publication plot and symlog

The **Show publication plot** button renders a Fig. 3b-style matplotlib figure server-side: both cutoff scans (`g`/`g0` versus `z_fit`) with shaded 95% confidence-interval bands and a callout at the best (minimum-CI) point. The optional **log scale** checkbox switches the y-axis to `symlog`, with the linear-region threshold (`linthresh`) set to roughly the 5th-percentile magnitude of `|g|` across both scans, so the axis stays linear near zero (where gain can cross sign) and log-scaled further out.

### Tunable constants

Every threshold above is a plain constant in `server/scanner.py` (or `server/plots.py`), not a config file. Edit it in source if your sample's physics needs a different assumption:

| Constant | Value | Where |
| --- | --- | --- |
| Auto-band half-width | plus or minus 5.0 nm | `_spectral_vsl_analysis` |
| Spectral baseline window | `clamp(n // 4, 4, 20)` points | `_spectral_vsl_analysis` |
| Spectral fit-window bounds | 5% to 75% of peak net signal | `_spectral_vsl_analysis` |
| Spectral minimum run length | 6 points | `_spectral_vsl_analysis` |
| Spectral minimum points required | 8 points, 3 shared pixels | `_spectral_vsl_analysis` |
| VSL baseline window | `max(3, n // 4)` points | `_vsl_baseline` |
| Auto-z0 onset threshold | `baseline + max(5 sigma, 0.5% of (max - baseline))` | `_vsl_auto_z0` |
| Auto-z0 walk-back threshold | `baseline + 2 sigma` | `_vsl_auto_z0` |
| Auto-z0 confirmation window | 3 consecutive points | `_vsl_auto_z0` |
| SSG golden-section bounds | `[1e-4, min(10*g_rough, 100, 354/z_max)]` | `_fit_ssg` |
| SSG boundary-rejection margin | within 0.1% of either bound | `_fit_ssg` |
| Gsat Nelder-Mead | 250 iterations, tolerance `1e-9` | `_fit_saturated` |
| Gsat CI finite-difference step | `1e-4` (log space) | `_fit_saturated_ci` |
| Gsat CI sanity cap | reject if `ci > 50 * |g0|` | `_fit_saturated_ci` |
| Gsat cutoff-scan rejection | reject if `g0 * z_span > 50` | `_vsl_saturated_scan` |
| Instantaneous-gain noise floor | 2% of max(I) | `_vsl_instantaneous` |
| Instantaneous-gain plateau | maximum of 5-point moving median | `_vsl_instantaneous` |
| Symlog linthresh | roughly 5th-percentile `|g|`, floor `1e-6`/`1e-4` | `plots.py` |

Agreement between the SSG plateau and the Gsat `g0` indicates a trustworthy gain value; divergence indicates saturation, onset error, or a non-ASE background: the same self-consistency checks recommended in the paper.

## Beam Profile Analysis

**Beam profile** turns a knife-edge scan (signal versus position, an erf-like edge) into a Gaussian beam profile, without needing a pre-existing peak function:

1. Central-difference derivative of the signal: `slope_i = (y[i+1] - y[i-1]) / (x[i+1] - x[i-1])`.
2. A weighted log-parabola fit to `|slope|`, of the form `ln(p) = a + b*x + c*x^2` with weights `w = p^2`, solved in closed form via a 3x3 normal-equations solve (Caruana's method; no iterative optimizer, no scipy).
3. Accepted only if the fit curves concave-down (`c < 0`, meaning it is actually a peak, not a saddle): `sigma = sqrt(-1 / (2c))`, center `x0 = -b / (2c)`, `amplitude = exp(a - b^2 / (4c))`, `FWHM = 2 * sqrt(2 * ln2) * sigma`.
4. Needs at least 3 usable points; returns nothing if the quadratic term is not negative.

## Live Plots and UI Behavior

**Client versus server split.** Every fit in [VSL Gain Analysis](#vsl-gain-analysis) and [Beam Profile Analysis](#beam-profile-analysis) runs in Python. The browser's job is polling and drawing: no gain math, curve fitting, or statistics run in JavaScript. The one piece of real client-side math is axis/tick scaling and unit formatting.

**Polling intervals** (each page/feature polls its own backend independently):

| What | Interval | Endpoint |
| --- | --- | --- |
| Scan config (device list, active selection) | 5000 ms | `GET /api/scan/config` |
| Scan status (progress, LEDs, readouts) | 400 ms | `GET /api/scan/status` |
| Scan data (points for the plot/table) | 800 ms | `GET /api/scan/data` |
| Live instantaneous gain | 1200 ms (only once at least 5 points exist) | `GET /api/scan/vsl/instant` |
| SMC100 manual-control status | 500 ms | `GET /api/smc/status` |
| Standalone spectrometer live spectrum | 250 ms | `GET /api/spec/spectrum` or `/api/avantes/spectrum` |
| PM400 chart / stats | 500 ms | `GET /api/power/chart`, `/api/power/stats` |
| PM400 status | 200 ms | `GET /api/power/status` |
| Piezo status | 300 ms | `GET /api/piezo/status` |

**Canvas rendering.** All live plots are hand-drawn on `<canvas>` (no charting library): a "nice numbers" tick-step algorithm picks round gridline spacing, and `devicePixelRatio` scaling keeps lines crisp on high-DPI displays. The main Scan-page plot is **linear only**: there is no log/symlog toggle on it. The standalone spectrometer pages (`avantes.html`/`hr4000.html`) do have a true log10 y-axis toggle, computed client-side. Symlog only appears on the server-rendered VSL publication figure (see above), never in a live canvas plot.

**Two distinct band UIs (do not confuse them):**
- **Wavelength bands** card (Scan page): arbitrary summed-counts-vs-position overlays for any wavelength range you define, unrelated to gain fitting.
- **Manual VSL band** field (inside the VSL Gain card): overrides the automatic emission-band detection used by the spectrum-aware gain fit.

**Theming.** Dark is the default theme; toggle via the UI button, or force it with `?theme=light`/`?theme=dark` in the URL (persisted to `localStorage`). All canvas-plot colors are read live from CSS custom properties, so a live plot repaints correctly the instant you switch themes.

## Standalone Instrument Apps

Each instrument UI can also run independently from the repository root, on its own port, with its own live plot and full instrument-specific control set (integration time, averaging, zeroing, homing, wavelength correction; see [Instrument Reference](#instrument-reference)). Each app initializes only its own hardware backend and remains usable when that instrument is disconnected, showing its connection error in the browser.

| Instrument | Command | URL | What it uniquely controls |
| --- | --- | --- | --- |
| Piezo controller | `python -m server.piezo` | [http://localhost:5001](http://localhost:5001) | Voltage sweeps, zeroing/closed-loop workflow |
| PM400 power meter | `python -m server.power_monitor` | [http://localhost:5002](http://localhost:5002) | Wavelength correction, auto-range, zero adjustment, CSV logging |
| SMC100 stage | `python -m server.smc100_app` | [http://localhost:5003](http://localhost:5003) | Velocity, homing, jog moves |
| HR4000 spectrometer | `python -m server.hr4000_app` | [http://localhost:5004](http://localhost:5004) | Boxcar smoothing, dark correction |
| Avantes spectrometer | `python -m server.avantes_app` | [http://localhost:5005](http://localhost:5005) | Pixel smoothing, dark correction |

The header navigation on each standalone page links across to the other apps by port (the *Scan* link points to the unified app on port 5050), so cross-links only resolve for apps that are currently running. Note that HR4000 and Avantes do not need their standalone app running to be usable *in a scan*: they are already available from the Scan page (see [How Device Awareness Works](#how-device-awareness-works)). Their standalone apps exist for quick single-instrument viewing and tuning outside of a scan.

## Instrument Reference

Physical quantity, every user-tunable parameter, and hardware quirks that actually affect how you use the tool, per device. Deep protocol/threading internals are intentionally omitted here; they are in the driver source if you need them.

### Piezo, Thorlabs BPC301 / DRV517

Closed-loop position (µm, 0 to 30 µm nominal DRV517 travel) or open-loop drive voltage (V), over USB via Thorlabs APT protocol.

| Parameter | Set via | Range / default |
| --- | --- | --- |
| Open-loop voltage | `POST /api/piezo/voltage {voltage}` | 0 to 75 V (or 150 V, controller-dependent) |
| Closed-loop position | `POST /api/piezo/move {position}` | 0 to 30 µm |
| Output enable | `POST /api/piezo/output {enable}` | on/off |
| Zeroing | `POST /api/piezo/zero/start` then `/zero/confirm` (or `/zero/auto` for unattended) | roughly 22 s typical settle |
| Voltage sweep | `POST /api/piezo/sweep/start {start, stop, steps, dwell, repeat}` | |

Quirks: the controller **locks up if any USB traffic is sent while it is zeroing or moving in closed loop**, so the app deliberately goes silent (no status polling) during those windows; do not expect live status during a zero or a closed-loop move. The `piezo_connected` status flag reads **false even when the DRV517 is fully operational**: it is a known false-negative (the DRV517 does not wire that ID-resistor pin), not a fault.

### Newport SMC100CC / LTA-HS stage

Linear position (mm, 50 mm travel) over RS-232 ASCII, 57600 baud.

| Parameter | Set via | Range / default |
| --- | --- | --- |
| Absolute move | `POST /api/smc/move {position}` | 0 to 50 mm |
| Relative jog | `POST /api/smc/move/relative {delta}` | |
| Velocity | `POST /api/smc/velocity {velocity}` | 0 to 5.0 mm/s (default 2.0) |
| Home search | `POST /api/smc/home` | required once after power-up |
| Enable/disable axis | `POST /api/smc/enable {enable}` | |

Quirks: unlike the piezo, this protocol tolerates continuous status polling during motion; the UI reads live status throughout a move. A home search is required before any absolute move will be accepted; the app tracks this as a "referenced" flag and disables absolute-move controls until it is done.

### Thorlabs PM400 power meter

Optical power (watts internally, dBm derived) via USBTMC/SCPI, over PyVISA.

| Parameter | Set via | Range / default |
| --- | --- | --- |
| Wavelength correction | `POST /api/power/wavelength {wavelength}` | clamped to the sensor's supported range |
| Averaging count | `POST /api/power/averaging {count}` | at least 1 |
| Auto/manual power range | `POST /api/power/range {auto}` or `{upper}` | |
| Dark/zero adjustment | `POST /api/power/zero` (block the beam first) | |
| Sampler poll rate | `POST /api/power/rate {hz}` | 0.5 to 20 Hz (default 10 Hz) |

Quirk: one background thread owns the instrument continuously; all reads and writes (including from the merged Scan app) are funneled through it so USBTMC never sees concurrent access from two code paths at once.

### Ocean Optics HR4000 spectrometer

Intensity-vs-wavelength spectrum (roughly 200 to 1100 nm, 3648-pixel CCD) via `python-seabreeze`.

| Parameter | Set via | Range / default |
| --- | --- | --- |
| Integration time | `POST /api/spec/integration {micros}` | device-reported limits (typically about 3.8 ms to 10 s); default 10 ms |
| Averaging (host-side) | `POST /api/spec/averaging {scans}` | at least 1 |
| Boxcar smoothing width | `POST /api/spec/boxcar {width}` | 0 or more pixels (kernel = 2*width+1) |
| Dark correction | `POST /api/spec/dark {enable}` | auto-disabled if the unit does not support it |

Quirks: forces the pure-Python `pyseabreeze` backend rather than the compiled `cseabreeze` extension (no C++ toolchain needed to install); dark/nonlinearity correction silently falls back to an uncorrected read if the specific unit does not support it, rather than erroring.

### Avantes AvaSpec spectrometer (SensLine / NIRLine)

Intensity-vs-wavelength spectrum (SensLine roughly 200 to 1100 nm, NIRLine roughly 900 to 2500 nm) via the vendor `libavs` C library, wrapped in ctypes.

| Parameter | Set via | Range / default |
| --- | --- | --- |
| Integration time | `POST /api/avantes/integration {ms}` | 0.002 to 600000 ms; default 10 ms |
| Averaging | `POST /api/avantes/averaging {scans}` | at least 1 |
| Pixel smoothing | `POST /api/avantes/smoothing {pixels}` | 0 or more |
| Dark correction | `POST /api/avantes/dark {enable}` | |

Quirks: SensLine versus NIRLine is auto-detected from the device's own wavelength coverage, not a separate setting. On macOS, if the vendored `.dylib` is quarantined, clear it once: `xattr -dr com.apple.quarantine drivers/libavs/`.

## Supported Hardware

| Role | Supported device | Notes |
| --- | --- | --- |
| Actuator | Thorlabs BPC301 / DRV517 piezo controller | Position and voltage scans; position scans require zeroed, closed-loop operation. |
| Actuator | Newport SMC100CC with LTA-HS actuator | Position scans; reference (home) before use. |
| Detector | Thorlabs PM400 power meter | Measures optical power in W. |
| Detector | Ocean Optics HR4000 | Captures spectra and derives selectable scalar metrics. |
| Detector | Avantes AvaSpec, including SensLine/NIRLine | Captures spectra and derives selectable scalar metrics. |

See [Instrument Reference](#instrument-reference) for every tunable parameter and hardware quirk.

## Hardware Notes

- Hardware backends are optional and fail independently, so a missing Python package, native library, or disconnected device only affects that instrument.
- On Windows, raw-USB instruments such as the HR4000, PM400, Avantes, or a BPC301 using the pyftdi/libusb fallback need a WinUSB driver. Bind it with [Zadig](https://zadig.akeo.ie) once per instrument and host. Serial-port devices (SMC100, BPC301 in plain VCP mode) do not need this step.
- The Avantes driver uses the vendor `libavs` library. A macOS arm64 library is included. For Linux or Windows, place the matching vendor SDK library in `drivers/libavs/` (`libavs.so` on Linux or `avaspec_production.dll` on Windows).

## Data Export Reference

**Merged Scan app**: **Download ZIP** bundles whatever is currently available: `scan_data.csv` (always), `spectra.csv` (spectrometer scans), `band_sums.csv` (if wavelength bands were computed), `beam_profile.csv` (if the Gaussian fit succeeded), `vsl_gain.csv` (if a VSL fit succeeded, includes the SSG cutoff scan, saturation-model summary, and instantaneous gain), plus rendered PNGs (`scan_plot.png`, `spectra_map.png`, `vsl_gain_plot.png`) gated by the export dialog's checkboxes. `scan_data.csv` includes a derived `power_dBm` column for power-meter scans (`10 * log10(v / 1e-3)`).

**Standalone apps**: each has its own **Download ZIP**/**Export** action bundling that instrument's latest CSV plus rendered plot (for example, HR4000/Avantes: `spectrum.csv` and `spectrum_plot.png`; PM400: power log CSV and `power_plot.png`).

## API Quick Reference

Every scan action is a plain HTTP endpoint. The browser UI is just one client of it, so scans can be scripted directly (curl, Python `requests`, and so on) without the browser.

| Route | Method | Purpose |
| --- | --- | --- |
| `/api/scan/config` | GET | Device list, active selections, current metric |
| `/api/scan/actuator` | POST | Switch active actuator |
| `/api/scan/detector` | POST | Switch active detector |
| `/api/scan/detector/metric` | POST | Set metric / wavelength / band width |
| `/api/scan/detector/acquire` | POST | Set spectrometer integration/averages |
| `/api/scan/prepare` | POST | Run the active axis's ready action (zero/home) |
| `/api/scan/start` | POST | Start a scan |
| `/api/scan/stop` | POST | Stop the running scan |
| `/api/scan/clear` | POST | Clear recorded points |
| `/api/scan/status` | GET | Live progress |
| `/api/scan/data` | GET | Raw point arrays |
| `/api/scan/download` | GET | CSV of raw scan points |
| `/api/scan/bands` | POST | Per-band summed counts vs. position |
| `/api/scan/spectra` | GET | Full wavelength times position CSV |
| `/api/scan/profile` | GET | Knife-edge Gaussian-profile fit |
| `/api/scan/vsl` | GET/POST | Full VSL gain analysis |
| `/api/scan/vsl/instant` | GET | Instantaneous-gain-only (cheap, pollable) |
| `/api/scan/plot` | GET | Matplotlib scan figure (`?format=png|svg|pdf`) |
| `/api/scan/vsl/plot` | GET | Gain-vs-cutoff figure (`?log=1` for symlog) |
| `/api/scan/export` | POST | Bundle everything into one ZIP |

Each standalone app exposes the equivalent per-device routes under its own prefix (`/api/piezo`, `/api/smc`, `/api/power`, `/api/spec`, `/api/avantes`). See [Instrument Reference](#instrument-reference) for the tunable ones.

## Project Layout

```text
run.py                    Application launcher (merged Scan app, port 5050)
server/scanner.py         Unified scan engine and Flask application
server/actuators.py       Actuator abstraction and auto-detection
server/detectors.py       Detector abstraction and auto-detection
server/plots.py           Plot and spectrum-map export
server/piezo.py           BPC301/DRV517 piezo blueprint + standalone app
server/smc100_app.py      SMC100/LTA-HS stage blueprint + standalone app
server/power_monitor.py   PM400 power meter blueprint + standalone app
server/hr4000_app.py      HR4000 spectrometer blueprint + standalone app
server/avantes_app.py     Avantes spectrometer blueprint + standalone app
drivers/                  Hardware protocol implementations
server/templates/         Browser UI templates
server/static/            Shared CSS/JS and image assets
```

## Development

The application uses Flask and serves templates from `server/templates/` with shared assets in `server/static/`. The production-style launcher is `python run.py`; `python -m server.scanner` is also supported. To change a VSL-analysis assumption (onset sensitivity, band width, fit-window bounds, and more), edit the constant directly in `server/scanner.py`. See the Tunable Constants table under [VSL Gain Analysis](#vsl-gain-analysis) for exact locations.

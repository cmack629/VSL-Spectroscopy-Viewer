# VSL Scan

VSL Scan is a local Flask application for coordinated actuator and optical-signal scans. It auto-detects connected hardware, moves an actuator through configured setpoints, measures the selected detector after each move, and displays the resulting signal-versus-position trace in the browser.

The app can start without instruments connected. Unavailable devices are shown as not connected rather than preventing the server from running.

## Features

- Coordinate step-and-measure scans with configurable start, stop, step count, settle time, dwell time, bidirectional operation, and repeat mode.
- Use a Thorlabs BPC301 / DRV517 piezo in position (um) or voltage (V) mode, or a Newport SMC100CC / LTA-HS stage in position (mm) mode.
- Measure with a Thorlabs PM400 power meter, Ocean Optics HR4000, or Avantes AvaSpec spectrometer.
- Choose optical power, integrated spectral area, spectral peak intensity, or intensity at a selected wavelength.
- Export scan signals and spectra as CSV, and download plot images as PNG.
- Capture a complete spectrum at every scan step when using a spectrometer.

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

## Basic Workflow

1. Connect the actuator and detector, then start the application. It probes all supported devices and selects the first connected actuator and detector.
2. Open the Scan page and select devices when more than one is connected.
3. Prepare the actuator: zero and close the loop for a piezo position scan, or home the SMC100 stage.
4. Select the actuator axis, detector metric, scan limits, number of points, settle time, and measurement dwell time.
5. Start the scan. The live plot and readouts update as each measurement is recorded.
6. Download the signal CSV, spectra CSV when applicable, or PNG plot from the Scan page.

Dedicated pages are also available for the piezo controller, stage, PM400, HR4000, and Avantes instruments.

## Supported Hardware

| Role | Supported device | Notes |
| --- | --- | --- |
| Actuator | Thorlabs BPC301 / DRV517 piezo controller | Position and voltage scans; position scans require zeroed, closed-loop operation. |
| Actuator | Newport SMC100CC with LTA-HS actuator | Position scans; reference (home) before use. |
| Detector | Thorlabs PM400 power meter | Measures optical power in W. |
| Detector | Ocean Optics HR4000 | Captures spectra and derives selectable scalar metrics. |
| Detector | Avantes AvaSpec, including SensLine/NIRLine | Captures spectra and derives selectable scalar metrics. |

## Hardware Notes

- Hardware backends are optional and fail independently, so a missing Python package, native library, or disconnected device only affects that instrument.
- For a piezo closed-loop move, the controller must be zeroed first. The app intentionally avoids status polling while the controller is moving or scanning to prevent USB communication from interfering with motion.
- On Windows, raw-USB instruments such as the HR4000, PM400, Avantes, or a BPC301 using the pyftdi/libusb fallback need a WinUSB driver. Bind it with [Zadig](https://zadig.akeo.ie) once per instrument and host. Serial-port devices do not need this step.
- The Avantes driver uses the vendor `libavs` library. A macOS arm64 library is included. For Linux or Windows, place the matching vendor SDK library in `drivers/libavs/` (`libavs.so` on Linux or `avaspec_production.dll` on Windows).

## Project Layout

```text
run.py              Application launcher
server/scanner.py   Unified scan engine and Flask application
server/actuators.py Actuator abstraction and auto-detection
server/detectors.py Detector abstraction and auto-detection
server/plots.py     Plot and spectrum-map export
drivers/             Hardware protocol implementations
server/templates/   Browser UI templates
```

## Development

The application uses Flask and serves templates from `server/templates/` with shared assets in `server/static/`. The production-style launcher is `python run.py`; `python -m server.scanner` is also supported.
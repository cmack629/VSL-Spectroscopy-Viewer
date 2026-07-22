# One-time environment setup for Windows (PowerShell).
#
#   .\setup.ps1
#
# Creates a virtual environment (.venv) and installs every Python dependency
# this app needs. Hardware backends are imported defensively by the app
# itself (server/detectors.py, server/actuators.py, drivers/*.py), so it's
# fine to run this on a machine that only has some — or none — of the
# instruments attached; missing hardware just shows as "not connected".
#
# After this finishes:
#   .venv\Scripts\Activate.ps1
#   python run.py                 ->  http://localhost:5050

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Venv = ".venv"

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    $ver = python --version
    Write-Host "!! $ver found, but this app needs Python 3.10+"
    Write-Host "   (server/app.py uses newer type-annotation syntax that fails to"
    Write-Host "   import on older versions). Install a newer Python from"
    Write-Host "   https://python.org (check 'Add python.exe to PATH') and re-run."
    exit 1
}

Write-Host "==> Creating virtual environment in $Venv"
python -m venv $Venv

$Pip = "$Venv\Scripts\pip.exe"
& $Pip install --upgrade pip --quiet

# Unlike Linux, seabreeze (the HR4000 spectrometer library) ships a prebuilt
# Windows wheel on PyPI — no compiler or pkg-config dance needed here.
Write-Host "==> Installing Python dependencies"
& $Pip install --quiet -r requirements.txt

Write-Host ""
Write-Host "==> Done. Next:"
Write-Host "      .venv\Scripts\Activate.ps1"
Write-Host "      python run.py                 ->  http://localhost:5050"
Write-Host ""
Write-Host "NOTE: raw-USB instruments (HR4000, PM400, Avantes, or the BPC301 if"
Write-Host "it falls back to pyftdi/libusb mode) need a WinUSB driver bound to"
Write-Host "the device before Python can see it -- Windows' default driver won't"
Write-Host "work with pyusb/pyvisa-py. Use Zadig (https://zadig.akeo.ie) to bind"
Write-Host "WinUSB to each instrument once, per machine. Serial-port instruments"
Write-Host "(SMC100, BPC301 in plain VCP mode) don't need this -- Windows'"
Write-Host "built-in COM port driver already works."

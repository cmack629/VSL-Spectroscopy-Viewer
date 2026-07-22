#!/usr/bin/env bash
# One-time environment setup for macOS and Linux.
#
#   ./setup.sh
#
# Creates a virtual environment (.venv) and installs every Python dependency
# this app needs. Hardware backends are imported defensively by the app
# itself (server/detectors.py, server/actuators.py, drivers/*.py), so it's
# fine to run this on a machine that only has some — or none — of the
# instruments attached; missing hardware just shows as "not connected".
#
# After this finishes:
#   source .venv/bin/activate
#   python run.py                 →  http://localhost:5050
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON=${PYTHON:-python3}
VENV=.venv

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "!! $("$PYTHON" --version 2>&1) found, but this app needs Python 3.10+"
    echo "   (server/app.py uses newer type-annotation syntax that fails to"
    echo "   import on older versions). Install a newer Python and re-run,"
    echo "   e.g.: PYTHON=python3.11 ./setup.sh"
    exit 1
fi

echo "==> Creating virtual environment in $VENV"
"$PYTHON" -m venv "$VENV"

PIP="$VENV/bin/pip"
"$PIP" install --upgrade pip --quiet

OS=$(uname -s)

if [ "$OS" = "Darwin" ]; then
    echo "==> macOS detected"
    if command -v brew >/dev/null 2>&1; then
        if ! brew list libusb >/dev/null 2>&1; then
            echo "==> Installing libusb (Homebrew) — needed at runtime by the"
            echo "    BPC301/HR4000/PM400 drivers (pyftdi/pyusb/pyvisa-py bind to it via ctypes)"
            brew install libusb
        fi
    else
        echo "!! Homebrew not found. If an instrument driver can't find libusb at"
        echo "   runtime, install Homebrew (https://brew.sh) then: brew install libusb"
    fi
    echo "==> Installing Python dependencies"
    "$PIP" install --quiet -r requirements.txt

elif [ "$OS" = "Linux" ]; then
    echo "==> Linux detected"
    # seabreeze's compiled cseabreeze backend has no prebuilt Linux wheel on
    # PyPI, so a plain `pip install` tries to build it from source — even
    # though drivers/hr4000.py forces the pure-Python pyseabreeze (pyusb)
    # backend at runtime regardless, making that compile unnecessary. Still,
    # just RESOLVING the build (before we ever get a say) needs pkg-config +
    # libusb's headers, so we install those, then tell pip to skip the actual
    # compile with --without-cseabreeze.
    if command -v apt-get >/dev/null 2>&1; then
        if ! dpkg -s libusb-1.0-0-dev >/dev/null 2>&1 || ! command -v pkg-config >/dev/null 2>&1; then
            echo "==> Installing libusb + pkg-config (apt, needs sudo)"
            sudo apt-get update
            sudo apt-get install -y libusb-1.0-0 libusb-1.0-0-dev pkg-config
        fi
    else
        echo "!! Not a Debian/Ubuntu system — install the equivalent of"
        echo "   libusb-1.0-0-dev + pkg-config with your package manager first"
        echo "   (Fedora/RHEL: sudo dnf install libusb1-devel pkgconfig)"
    fi

    # Debian's .pc file is named "libusb-1.0", but seabreeze's setup.py asks
    # pkg-config for plain "libusb". Alias it just for this install via
    # PKG_CONFIG_PATH instead of touching system pkg-config directories.
    SHIM_DIR="$VENV/pkgconfig-shim"
    mkdir -p "$SHIM_DIR"
    PC_DIR=$(pkg-config --variable pcfiledir libusb-1.0 2>/dev/null || true)
    if [ -n "$PC_DIR" ] && [ -f "$PC_DIR/libusb-1.0.pc" ]; then
        ln -sf "$PC_DIR/libusb-1.0.pc" "$SHIM_DIR/libusb.pc"
    fi

    echo "==> Installing Python dependencies"
    grep -v '^seabreeze' requirements.txt > "$VENV/requirements-base.txt"
    "$PIP" install --quiet -r "$VENV/requirements-base.txt"
    PKG_CONFIG_PATH="$SHIM_DIR${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
        "$PIP" install --quiet \
        --config-settings="--build-option=--without-cseabreeze" \
        "$(grep '^seabreeze' requirements.txt)"
    rm -rf "$SHIM_DIR" "$VENV/requirements-base.txt"

else
    echo "!! Unrecognized OS ($OS) — falling back to a plain pip install."
    echo "   If seabreeze fails to build, see the Linux section of this script"
    echo "   for the pkg-config workaround it needs."
    "$PIP" install --quiet -r requirements.txt
fi

echo
echo "==> Done. Next:"
echo "      source $VENV/bin/activate"
echo "      python run.py                 →  http://localhost:5050"

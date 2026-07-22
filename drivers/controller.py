"""
DRV517 Piezoelectric Screw Actuator Controller
Communicates with a Thorlabs KPC101 / BPC301 / BPC303 controller
via the Thorlabs APT USB protocol (FTDI serial).

Hardware chain:  PC <--USB--> KPC101/BPC30x <--SMA/Hirose--> DRV517

Requirements:  pip install pyserial
               pip install pyftdi   (macOS without FTDI VCP driver)
               brew install libusb  (required by pyftdi on macOS)

On macOS the OS no longer ships an FTDI VCP driver, so the controller will
not appear as /dev/tty.* unless you install the FTDI VCP driver pkg from
ftdichip.com.  Alternatively, pyftdi + libusb work without any kernel driver
and are preferred automatically when no VCP port is found.
"""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

import serial
import serial.tools.list_ports

try:
    import pyftdi.serialext  # registers the ftdi:// URL scheme with pyserial
    from pyftdi.ftdi import Ftdi as _Ftdi
    _PYFTDI_AVAILABLE = True
except ImportError:
    _PYFTDI_AVAILABLE = False


# ---------------------------------------------------------------------------
# APT protocol constants
# ---------------------------------------------------------------------------

class MsgID(IntEnum):
    HW_DISCONNECT          = 0x0002
    HW_REQ_INFO           = 0x0005
    HW_GET_INFO           = 0x0006
    HW_START_UPDATEMSGS   = 0x0011
    HW_STOP_UPDATEMSGS    = 0x0012
    MOD_SET_CHANENABLESTATE = 0x0210
    MOD_REQ_CHANENABLESTATE = 0x0211
    MOD_GET_CHANENABLESTATE = 0x0212
    MOD_IDENTIFY           = 0x0223

    # Control loop mode (open / closed)
    PZ_SET_POSCONTROLMODE = 0x0640
    PZ_REQ_POSCONTROLMODE = 0x0641
    PZ_GET_POSCONTROLMODE = 0x0642

    # Open-loop voltage
    PZ_SET_OUTPUTVOLTS    = 0x0643
    PZ_REQ_OUTPUTVOLTS    = 0x0644
    PZ_GET_OUTPUTVOLTS    = 0x0645

    # Closed-loop position (strain-gauge feedback)
    PZ_SET_OUTPUTPOS      = 0x0646
    PZ_REQ_OUTPUTPOS      = 0x0647
    PZ_GET_OUTPUTPOS      = 0x0648

    # Zero the strain gauge (establish position datum)
    PZ_SET_ZERO           = 0x0658

    # Input voltage / loop source
    PZ_SET_INPUTVOLTSSRC  = 0x0652
    PZ_REQ_INPUTVOLTSSRC  = 0x0653
    PZ_GET_INPUTVOLTSSRC  = 0x0654

    # PI closed-loop constants
    PZ_SET_PICONSTS       = 0x0655
    PZ_REQ_PICONSTS       = 0x0656
    PZ_GET_PICONSTS       = 0x0657

    # Actuator max travel (read from the actuator's calibration resistor)
    PZ_REQ_MAXTRAVEL      = 0x0650
    PZ_GET_MAXTRAVEL      = 0x0651

    # I/O settings (BPC/modular series; 0x07D4 is the old T-Cube TPZ id)
    PZ_SET_IOSETTINGS     = 0x0670
    PZ_REQ_IOSETTINGS     = 0x0671
    PZ_GET_IOSETTINGS     = 0x0672

    # Status
    PZ_REQ_PZSTATUSBITS   = 0x065B
    PZ_GET_PZSTATUSBITS   = 0x065C
    PZ_REQ_PZSTATUSUPDATE = 0x0660   # polled status (volts+pos+bits in one reply)
    PZ_GET_PZSTATUSUPDATE = 0x0661
    PZ_ACK_PZSTATUSUPDATE  = 0x0662  # "server alive" keep-alive (USB; >=1/sec)


class ControlMode(IntEnum):
    OPEN_LOOP      = 0x01   # voltage control
    CLOSED_LOOP    = 0x02   # position control via strain gauge
    OPEN_LOOP_SM   = 0x03   # open-loop with smoothed transition
    CLOSED_LOOP_SM = 0x04   # closed-loop with smoothed transition


class InputVoltsSource(IntEnum):
    SOFTWARE_ONLY = 0x00
    EXT_SIGNAL    = 0x01
    JOYSTICK      = 0x02


# APT address bytes
ADDR_HOST       = 0x01
ADDR_USB_MODULE = 0x50   # generic USB controller module (BPC/KPC)

# Voltage scaling: APT represents voltage as int16, 32767 = max voltage
VOLT_SCALE = 32767.0

# Position scaling: APT represents position as int16, 32767 (0x7FFF) = 100%
# of max extension (APT protocol p.199, MGMSG_PZ_SET_OUTPUTPOS). NOT 65535.
POS_SCALE  = 32767.0
POS_RANGE_UM = 30.0  # µm — DRV517 piezo travel (strain-gauge feedback, 10 nm res.)


# ---------------------------------------------------------------------------
# Low-level APT frame builders / parsers
# ---------------------------------------------------------------------------

def _build_short_msg(msg_id: int, param1: int = 0, param2: int = 0,
                     dest: int = ADDR_USB_MODULE, src: int = ADDR_HOST) -> bytes:
    return struct.pack("<HBBBB", msg_id, param1, param2, dest, src)


def _build_long_msg(msg_id: int, data: bytes,
                    dest: int = ADDR_USB_MODULE, src: int = ADDR_HOST) -> bytes:
    header = struct.pack("<HH BB", msg_id, len(data), dest | 0x80, src)
    return header + data


def _parse_header(raw: bytes) -> tuple[int, int, int, int]:
    """Return (msg_id, param1_or_datalen, dest, src)."""
    msg_id, p1_or_len, dest, src = struct.unpack("<HH BB", raw[:6])
    return msg_id, p1_or_len, dest, src


# ---------------------------------------------------------------------------
# Connection layer
# ---------------------------------------------------------------------------

class APTConnection:
    """
    Thread-safe serial connection to an APT USB device.

    port='auto' tries VCP ports first (requires FTDI VCP driver), then falls
    back to pyftdi direct USB access (requires: brew install libusb &&
    pip install pyftdi) — no kernel driver needed for the fallback path.

    You can also pass an explicit ftdi:// URL to force pyftdi:
        port='ftdi://0x0403:0xFAF0/1'
    """

    THORLABS_VID = 0x0403   # FTDI
    THORLABS_PIDS = {0xFAF0, 0xFAF1, 0xFAF2, 0xFAF3,
                     0xC89C, 0xC89D, 0xC89E, 0xC89F}
    BAUD = 115200

    def __init__(self, port: str = "auto", timeout: float = 2.0):
        self._lock = threading.Lock()
        self._timeout = timeout

        if port == "auto":
            port = self._find_port()

        # pyftdi uses serial.serial_for_url(); plain pyserial uses serial.Serial()
        # Both expose the same API once constructed.
        if port.startswith("ftdi://"):
            if not _PYFTDI_AVAILABLE:
                raise RuntimeError(
                    "pyftdi is required for ftdi:// URLs.\n"
                    "  brew install libusb && pip install pyftdi"
                )
            # Thorlabs programs a custom PID into the FTDI chip; register it
            # so pyftdi's device-matching logic accepts the URL.
            for pid in self.__class__.THORLABS_PIDS:
                try:
                    _Ftdi.add_custom_product(self.__class__.THORLABS_VID, pid)
                except ValueError:
                    pass  # already registered
            self._ser = serial.serial_for_url(
                port,
                baudrate=self.BAUD,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                rtscts=True,
                timeout=timeout,
            )
        else:
            self._ser = serial.Serial(
                port=port,
                baudrate=self.BAUD,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                rtscts=True,
                timeout=timeout,
            )

        # APT requires RTS asserted, DTR deasserted
        self._ser.rts = True
        self._ser.dtr = False
        time.sleep(0.1)
        self._ser.reset_input_buffer()

    # ------------------------------------------------------------------

    @classmethod
    def _find_port(cls) -> str:
        # 1. Try VCP ports (works when FTDI VCP driver is installed)
        for p in serial.tools.list_ports.comports():
            if p.vid == cls.THORLABS_VID:
                return p.device

        # 2. Fall back to pyftdi direct USB (no kernel driver required)
        if _PYFTDI_AVAILABLE:
            return cls._find_ftdi_url()

        raise RuntimeError(
            "No Thorlabs APT device found as a serial port.\n\n"
            "Options:\n"
            "  A) Install the FTDI VCP driver from https://ftdichip.com/drivers/vcp-drivers/\n"
            "     then reconnect the controller.\n\n"
            "  B) Use pyftdi (no kernel driver needed):\n"
            "       brew install libusb\n"
            "       pip install pyftdi\n"
            "     Then reconnect and retry."
        )

    @classmethod
    def _ftdi_scan(cls) -> list[tuple[int, int, str, str]]:
        """
        Enumerate Thorlabs FTDI devices via pyusb without opening them.
        Returns list of (vid, pid, serial, description) tuples.
        """
        import usb.core
        results = []
        for pid in cls.THORLABS_PIDS:
            for dev in usb.core.find(find_all=True,
                                     idVendor=cls.THORLABS_VID,
                                     idProduct=pid):
                try:
                    sn   = usb.util.get_string(dev, dev.iSerialNumber) or ""
                    desc = usb.util.get_string(dev, dev.iProduct) or ""
                except Exception:
                    sn, desc = "", ""
                results.append((cls.THORLABS_VID, pid, sn, desc))
        return results

    @classmethod
    def _find_ftdi_url(cls) -> str:
        """Return the first ftdi:// URL matching a known Thorlabs PID."""
        for vid, pid, _, _ in cls._ftdi_scan():
            return f"ftdi://0x{vid:04x}:0x{pid:04x}/1"
        # Fall back to the known BPC301 PID — let pyftdi raise if not found
        return f"ftdi://0x{cls.THORLABS_VID:04x}:0x{0xFAF0:04x}/1"

    @classmethod
    def list_devices(cls) -> list[str]:
        found = [
            f"{p.device}  [{p.description}]"
            for p in serial.tools.list_ports.comports()
            if p.vid == cls.THORLABS_VID
        ]
        if not found and _PYFTDI_AVAILABLE:
            try:
                for vid, pid, sn, desc in cls._ftdi_scan():
                    found.append(
                        f"ftdi://0x{vid:04x}:0x{pid:04x}/1"
                        f"  [s/n={sn}  {desc}]"
                    )
            except Exception:
                pass
        return found

    # ------------------------------------------------------------------

    def send(self, frame: bytes) -> None:
        with self._lock:
            self._ser.write(frame)

    def recv(self, n_bytes: int) -> bytes:
        data = self._ser.read(n_bytes)
        if len(data) < n_bytes:
            raise TimeoutError(
                f"Expected {n_bytes} bytes but got {len(data)}"
            )
        return data

    def flush_input(self) -> None:
        with self._lock:
            self._ser.reset_input_buffer()

    def close(self) -> None:
        if self._ser.is_open:
            self._ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# Hardware info
# ---------------------------------------------------------------------------

@dataclass
class HardwareInfo:
    serial_number: int
    model: str
    hw_type: int
    fw_minor: int
    fw_interim: int
    fw_major: int
    hw_version: int
    mod_state: int
    num_channels: int


# ---------------------------------------------------------------------------
# Status word bits (MGMSG_PZ_GET_PZSTATUSBITS)
# ---------------------------------------------------------------------------

@dataclass
class PZStatus:
    """
    Status word bit definitions for BPC series (APT protocol rev 24, p.162-163).

    0x00000001  piezo_connected       — actuator detected on output
    0x00000010  zeroed                — channel has been zero'd
    0x00000020  zeroing               — zero operation in progress
    0x00000100  strain_gauge_connected— SG feedback cable present (PAA622 connected)
    0x00000400  position_control_mode — 1 = closed loop, 0 = open loop
    0x00001000  hw_75v                — hardware max = 75 V
    0x00002000  hw_100v               — hardware max = 100 V
    0x00004000  hw_150v               — hardware max = 150 V
    """
    raw: int

    @property
    def piezo_connected(self) -> bool:
        # Reflects the Actuator ID resistor/EEPROM on Pin 7 of PIEZO IN.
        # The DRV517 does not wire Pin 7 on its LEMO connector, so this is
        # always False for DRV517 even when fully operational in closed loop.
        return bool(self.raw & 0x00000001)

    @property
    def zeroed(self) -> bool:
        return bool(self.raw & 0x00000010)

    @property
    def zeroing(self) -> bool:
        return bool(self.raw & 0x00000020)

    @property
    def strain_gauge_connected(self) -> bool:
        return bool(self.raw & 0x00000100)

    @property
    def position_control_mode(self) -> bool:
        return bool(self.raw & 0x00000400)

    @property
    def hw_max_voltage(self) -> int:
        if self.raw & 0x00001000:
            return 75
        if self.raw & 0x00002000:
            return 100
        if self.raw & 0x00004000:
            return 150
        return 0

    # kept for backward compat — the old bit 0x0100 was actually strain_gauge_connected
    @property
    def output_enabled(self) -> bool:
        return self.piezo_connected

    @property
    def at_upper_limit(self) -> bool:
        return False   # BPC series does not use limit bits in this word

    @property
    def at_lower_limit(self) -> bool:
        return False

    def __str__(self) -> str:
        return (
            f"PZStatus(closed_loop={self.position_control_mode}, "
            f"piezo_connected={self.piezo_connected}, "
            f"strain_gauge_connected={self.strain_gauge_connected}, "
            f"hw_max={self.hw_max_voltage}V, "
            f"raw=0x{self.raw:08X})"
        )


# ---------------------------------------------------------------------------
# Main DRV517 controller class
# ---------------------------------------------------------------------------

class DRV517:
    """
    High-level interface for the Thorlabs DRV517 piezoelectric screw actuator
    driven by a KPC101 or BPC301/BPC303 controller.

    Usage – open loop (voltage):
        with DRV517() as drv:
            drv.set_voltage(50.0)          # 50 % of max (~75 V for 150 V unit)

    Usage – closed loop (position via built-in strain gauge):
        with DRV517() as drv:
            drv.prepare_closed_loop()      # open-loop → zero (~15-20 s) → closed-loop
            drv.move_to(15.0)              # µm (0–30 µm range)
            drv.wait_for_position(15.0)
    """

    MAX_VOLTAGE_PERCENT = 100.0

    def __init__(
        self,
        port: str = "auto",
        channel: int = 1,
        max_voltage_v: float = 75.0,   # set to match your controller (75 or 150 V)
        timeout: float = 2.0,
    ):
        self._conn = APTConnection(port=port, timeout=timeout)
        self._chan = channel            # 1-based channel number
        self._max_v = max_voltage_v
        self._mode: Optional[ControlMode] = None

        # Status cache — avoid streaming the controller more than once every few seconds
        self._status_cache: PZStatus = PZStatus(0)
        self._status_cache_ts: float = 0.0
        # Latest position/voltage parsed from the streamed status updates
        # (0x0661) — the same source Kinesis reads position from.
        self._last_pos_raw: int = 0
        self._last_volts_raw: int = 0

        # Stop any update messages left over from a previous session that was
        # killed without calling close() — otherwise they fill the RX buffer
        # and corrupt the HW_GET_INFO response.
        self._conn.send(_build_short_msg(MsgID.HW_STOP_UPDATEMSGS))
        time.sleep(0.5)   # 0.5 s >> 16 ms USB latency — all queued bytes arrive
        n = self._conn._ser.in_waiting
        if n:
            self._conn._ser.read(n)
        self._conn.flush_input()

    def _ack(self) -> None:
        """
        Send the "server alive" keep-alive (0x0662). APT-over-USB needs this at
        least once a second WHILE status updates are streaming, or the
        controller stops responding after ~50 messages (protocol p.207).

        Sent INLINE from whichever thread is driving the link — never from a
        background thread. pyftdi/libusb is not safe for concurrent read+write
        on one device handle, and a separate keep-alive thread writing while we
        block-read during streaming corrupts/locks the link. Kinesis is likewise
        single-threaded and sends this inside its status loop.
        """
        self._conn.send(
            _build_short_msg(MsgID.PZ_ACK_PZSTATUSUPDATE, dest=ADDR_USB_MODULE)
        )

    # ------------------------------------------------------------------
    # Context manager

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        try:
            self._conn.send(
                _build_short_msg(MsgID.HW_STOP_UPDATEMSGS, dest=ADDR_USB_MODULE)
            )
        finally:
            self._conn.close()

    # ------------------------------------------------------------------
    # Discovery / info

    @staticmethod
    def list_devices() -> list[str]:
        return APTConnection.list_devices()

    def get_hardware_info(self) -> HardwareInfo:
        self._conn.flush_input()
        self._conn.send(_build_short_msg(MsgID.HW_REQ_INFO))
        raw = self._conn.recv(90)
        _parse_header(raw[:6])
        payload = raw[6:]
        serial_num, = struct.unpack_from("<I", payload, 0)
        model = payload[4:12].rstrip(b"\x00").decode("ascii", errors="replace")
        hw_type, = struct.unpack_from("<H", payload, 12)
        fw_minor, fw_interim, fw_major = struct.unpack_from("<BBB", payload, 14)
        hw_ver, mod_state, num_ch = struct.unpack_from("<HHH", payload, 78)
        return HardwareInfo(
            serial_number=serial_num,
            model=model,
            hw_type=hw_type,
            fw_minor=fw_minor,
            fw_interim=fw_interim,
            fw_major=fw_major,
            hw_version=hw_ver,
            mod_state=mod_state,
            num_channels=num_ch,
        )

    def identify(self) -> None:
        """Flash the front-panel LED to identify the unit."""
        self._conn.send(_build_short_msg(MsgID.MOD_IDENTIFY, dest=ADDR_USB_MODULE))

    # ------------------------------------------------------------------
    # Mode control

    def enable_open_loop(self) -> None:
        self._conn.send(
            _build_short_msg(MsgID.PZ_SET_POSCONTROLMODE,
                             param1=self._chan, param2=ControlMode.OPEN_LOOP)
        )
        time.sleep(0.05)
        self._mode = ControlMode.OPEN_LOOP
        self._status_cache_ts = 0.0   # force fresh status read next call

    def enable_closed_loop(self) -> None:
        """Enable position control using the strain-gauge feedback (PAA622 required)."""
        self._conn.send(
            _build_short_msg(MsgID.PZ_SET_POSCONTROLMODE,
                             param1=self._chan, param2=ControlMode.CLOSED_LOOP)
        )
        time.sleep(0.05)
        self._mode = ControlMode.CLOSED_LOOP
        self._status_cache_ts = 0.0   # force fresh status read next call

    @staticmethod
    def _latest_update(buf: bytes):
        """
        Return (volts_raw, pos_raw, status_bits) from the last 0x0661 status
        update in buf, or None. The streamed 0x0661 carries position and voltage
        alongside the status word — this is where Kinesis reads position from.
        """
        last = None
        i = 0
        while i + 6 <= len(buf):
            msg_id = struct.unpack_from("<H", buf, i)[0]
            if msg_id == MsgID.PZ_GET_PZSTATUSUPDATE and (buf[i + 4] & 0x80):
                data_len = struct.unpack_from("<H", buf, i + 2)[0]
                end = i + 6 + data_len
                if end <= len(buf) and data_len >= 10:
                    _, v, p = struct.unpack_from("<Hhh", buf, i + 6)
                    s, = struct.unpack_from("<I", buf, i + 12)
                    last = (v, p, s)
                i = end if end <= len(buf) else len(buf)
            else:
                i += 1
        return last

    def zero(self, settle_s: float = 22.0, confirm=None) -> bool:
        """
        Trigger strain-gauge zeroing and wait QUIETLY for it to finish
        (APT protocol p.226, MGMSG_PZ_SET_ZERO 0x0658).

        Zeroing is entirely controller-side — the front-panel Zero button does it
        with the host doing nothing. We deliberately send NO USB traffic while it
        runs: every lock we hit came from streaming status continuously/rapidly
        through an active operation (pyftdi + the controller's update handling is
        fragile). So we send SET_ZERO, ensure the update stream is off, wait, then
        confirm with a single status read.

        Zeroing duration varies, so prefer `confirm`: a no-arg callable invoked
        right after SET_ZERO that must BLOCK until zeroing is done — e.g. a prompt
        asking the user to press Enter when the front-panel 'Zeroed' LED stops
        flashing. Without it, we fall back to a fixed settle_s wait. Returns True
        if 'zeroed' is set afterwards.
        """
        self.start_zero()
        if confirm is not None:
            confirm()
        else:
            time.sleep(settle_s)
        return self.finish_zero()

    def start_zero(self) -> None:
        """
        Begin zeroing (SET_ZERO) and silence the update stream. Pair with
        finish_zero() once the front-panel 'Zeroed' LED is solid — and send NO
        other USB traffic in between (that is what locks the controller). This
        split lets a non-blocking UI start zeroing, watch the LED, then confirm.
        """
        self._conn.flush_input()
        self._conn.send(_build_short_msg(MsgID.PZ_SET_ZERO, param1=self._chan))
        self._conn.send(_build_short_msg(MsgID.HW_STOP_UPDATEMSGS))

    def finish_zero(self) -> bool:
        """Single status read after zeroing completes. Returns True if 'zeroed'."""
        self._status_cache_ts = 0.0
        return self.get_status().zeroed

    def wait_until_zeroed(
        self,
        initial_quiet_s: float = 12.0,
        poll_interval_s: float = 2.5,
        timeout_s: float = 45.0,
        progress=None,
    ) -> bool:
        """
        Auto-detect end-of-zeroing WITHOUT a user confirm. Assumes start_zero()
        has already been called (SET_ZERO sent, update stream silenced).

        The controller emits no unsolicited "zeroed" event, so we cannot purely
        listen — we have to read status. To respect the golden rule (no USB
        chatter while the controller is mid-operation) as far as detection
        allows, we stay SILENT for `initial_quiet_s` (most of the ~15-22 s
        zeroing happens untouched), THEN poll status slowly: one brief
        START/ACK/STOP read every `poll_interval_s`, watching the 'zeroing' bit
        (0x20) clear and 'zeroed' bit (0x10) set. This is single-threaded on the
        device — the web status poller stays gated by _busy throughout, so there
        is never a concurrent reader/writer (the thing that actually locks the
        link). Returns True once zeroed, or False on timeout.

        `progress(elapsed_s, status)` is called after each poll if provided.
        """
        t_start = time.monotonic()
        if initial_quiet_s > 0:
            time.sleep(initial_quiet_s)
        deadline = t_start + timeout_s
        while time.monotonic() < deadline:
            self._status_cache_ts = 0.0          # force a fresh streamed read
            st = self.get_status()
            if progress is not None:
                progress(time.monotonic() - t_start, st)
            if st.zeroed and not st.zeroing:
                return True
            time.sleep(poll_interval_s)
        self._status_cache_ts = 0.0
        return self.get_status().zeroed

    def auto_zero(
        self,
        initial_quiet_s: float = 12.0,
        poll_interval_s: float = 2.5,
        timeout_s: float = 45.0,
        progress=None,
    ) -> bool:
        """
        One-shot, no-confirm closed-loop bring-up: SET_ZERO → silent wait →
        slow status poll until zeroed → ensure closed-loop is engaged. Mirrors
        prepare_closed_loop() but detects completion automatically instead of
        via a blocking confirm callback. Returns True if closed-loop is active.
        """
        self.start_zero()
        zeroed = self.wait_until_zeroed(initial_quiet_s, poll_interval_s,
                                        timeout_s, progress)
        st = self.get_status()
        if zeroed and not st.position_control_mode:
            self.enable_closed_loop()
            time.sleep(0.2)
            st = self.get_status()
        return st.position_control_mode

    def prepare_closed_loop(self, zero_settle_s: float = 22.0, confirm=None) -> bool:
        """
        Bring up closed loop exactly as Kinesis does: just zero the strain gauge.

        A USB capture of Kinesis shows it NEVER sends SET_POSCONTROLMODE — the
        controller auto-engages closed-loop mode itself when zeroing completes.
        Sending our own mode commands around zeroing is what diverged from
        Kinesis, so we don't: we zero, wait, and confirm closed loop is active.

        `confirm` is forwarded to zero() — pass a prompt that blocks until the
        'Zeroed' LED is solid. Returns True if closed-loop mode is active after.
        """
        self.zero(settle_s=zero_settle_s, confirm=confirm)
        st = self.get_status()
        if not st.position_control_mode:
            # Zeroing didn't auto-engage closed loop — fall back to setting it.
            self.enable_closed_loop()
            time.sleep(0.2)
            st = self.get_status()
        return st.position_control_mode

    def get_max_travel(self) -> float:
        """
        Return the actuator travel range in µm that the controller detected from
        the actuator's built-in calibration resistor (APT protocol p.227,
        MGMSG_PZ_REQ_MAXTRAVEL). Reported in 100 nm steps.

        A value of 0.0 means the controller does NOT recognise a position-sensing
        actuator on this channel — closed-loop position commands will fault, and
        you must check the actuator/feedback cabling or configure it in Kinesis.
        A DRV517 should report ~30.0 µm.
        """
        self._conn.flush_input()
        self._conn.send(_build_short_msg(MsgID.PZ_REQ_MAXTRAVEL, param1=self._chan))
        raw = self._conn.recv(10)
        _, travel_raw = struct.unpack_from("<HH", raw, 6)
        return travel_raw * 0.1

    def get_control_mode(self) -> ControlMode:
        self._conn.flush_input()
        self._conn.send(
            _build_short_msg(MsgID.PZ_REQ_POSCONTROLMODE, param1=self._chan)
        )
        raw = self._conn.recv(6)
        return ControlMode(raw[3])   # byte 3 = mode in the GET response

    def set_input_source(self, source: InputVoltsSource) -> None:
        """Set whether position/voltage setpoint comes from software, external signal, or joystick."""
        data = struct.pack("<HH", self._chan, int(source))
        self._conn.send(_build_long_msg(MsgID.PZ_SET_INPUTVOLTSSRC, data))
        time.sleep(0.05)

    # ------------------------------------------------------------------
    # Open-loop voltage control

    def set_voltage(self, volts: float) -> None:
        """
        Set piezo drive voltage.
        volts: absolute voltage in V (0 – max_voltage_v).
        """
        if not 0.0 <= volts <= self._max_v:
            raise ValueError(f"Voltage {volts} V out of range 0–{self._max_v} V")
        raw = round((volts / self._max_v) * VOLT_SCALE)
        data = struct.pack("<Hh", self._chan, raw)
        self._conn.send(_build_long_msg(MsgID.PZ_SET_OUTPUTVOLTS, data))

    def set_voltage_percent(self, pct: float) -> None:
        """Set voltage as a percentage (0–100 %)."""
        self.set_voltage((pct / 100.0) * self._max_v)

    def get_voltage(self) -> float:
        """Return current output voltage in V."""
        self._conn.flush_input()
        self._conn.send(
            _build_short_msg(MsgID.PZ_REQ_OUTPUTVOLTS, param1=self._chan)
        )
        raw = self._conn.recv(10)
        _, raw_v = struct.unpack_from("<Hh", raw, 6)
        return (raw_v / VOLT_SCALE) * self._max_v

    # ------------------------------------------------------------------
    # Closed-loop position control

    def move_to(self, position_um: float) -> None:
        """
        Move to an absolute position in µm (0–30 µm).
        Requires the controller to be in closed-loop mode.
        """
        if not 0.0 <= position_um <= POS_RANGE_UM:
            raise ValueError(
                f"Position {position_um} µm out of range 0–{POS_RANGE_UM} µm"
            )
        raw = round((position_um / POS_RANGE_UM) * POS_SCALE)
        data = struct.pack("<HH", self._chan, raw)
        self._conn.send(_build_long_msg(MsgID.PZ_SET_OUTPUTPOS, data))

    @property
    def last_position_um(self) -> float:
        """
        Position in µm from the most recent streamed update, with NO new serial
        I/O. Use this right after wait_for_position()/zero() (which already
        refreshed it) to avoid an extra START/STOP cycle on the just-settled move.
        """
        return max(0.0, (self._last_pos_raw / POS_SCALE) * POS_RANGE_UM)

    @property
    def last_voltage(self) -> float:
        """Output voltage (V) from the most recent streamed update — no serial I/O."""
        return (self._last_volts_raw / VOLT_SCALE) * self._max_v

    def get_position(self) -> float:
        """
        Return current position in µm, read from the streamed status update —
        the same source Kinesis reads position from. We deliberately do NOT
        poll REQ_OUTPUTPOS in a tight loop (our old wait_for_position fired it
        ~100×/move); Kinesis sends at most one REQ_OUTPUTPOS per move and reads
        position from the status stream, which is what get_status() refreshes.
        """
        self._status_cache_ts = 0.0      # force a fresh streamed read
        self.get_status()
        # Position is a signed int16: near 0% extension it can read slightly
        # negative (APT protocol p.199). Clamp to the physical 0 floor.
        um = (self._last_pos_raw / POS_SCALE) * POS_RANGE_UM
        return max(0.0, um)

    def wait_for_position(
        self, target_um: float, tol_um: float = 0.3,
        settle_s: float = 3.0, timeout_s: float = None
    ) -> bool:
        """
        Wait for a move to settle, then read position ONCE.

        Same rule as zero(): do no USB traffic while the controller is servoing
        (rapid status polling during an active move locks it). A DRV517 move
        completes in ~1-2 s, so we wait settle_s quietly then read once.
        timeout_s is accepted for compatibility but unused. Returns True if the
        settled position is within tol_um of target.
        """
        time.sleep(settle_s)
        self._status_cache_ts = 0.0
        self.get_status()
        return abs(self.last_position_um - target_um) <= tol_um

    # ------------------------------------------------------------------
    # PI constants (closed-loop tuning)

    def set_pi_consts(self, p_gain: float, i_gain: float) -> None:
        """
        Set proportional and integral gains for the closed-loop controller.
        Valid range: 0–255 for both (APT protocol p.202, MGMSG_PZ_SET_PICONSTS).
        Typical starting values: p_gain=100, i_gain=15.
        """
        p = int(max(0, min(255, p_gain)))
        i = int(max(0, min(255, i_gain)))
        data = struct.pack("<HHH", self._chan, p, i)
        self._conn.send(_build_long_msg(MsgID.PZ_SET_PICONSTS, data))

    def get_pi_consts(self) -> tuple[float, float]:
        """Return (p_gain, i_gain)."""
        self._conn.flush_input()
        self._conn.send(
            _build_short_msg(MsgID.PZ_REQ_PICONSTS, param1=self._chan)
        )
        raw = self._conn.recv(12)
        _, p, i = struct.unpack_from("<HHH", raw, 6)
        return float(p), float(i)

    # ------------------------------------------------------------------
    # Status

    # How often to actually poll the controller for status
    _STATUS_TTL = 3.0   # seconds

    def get_status(self) -> PZStatus:
        """
        Read controller status from streamed MGMSG_PZ_GET_PZSTATUSUPDATE (0x0661)
        messages: chan(2) + volts_raw(2) + pos_raw(2) + status_bits(4).

        This BPC301 (FW 3.2.2) does NOT answer the polled MGMSG_PZ_REQ_PZSTATUSUPDATE
        (0x0660) — valid status only arrives via the spontaneous update stream
        triggered by MGMSG_HW_START_UPDATEMSGS (0x0011). An inline 0x0662 ACK
        keeps that short stream from tripping the ~50-message limit (protocol p.207).

        Cached for _STATUS_TTL seconds so the web UI can poll at 300 ms without a
        full streaming round-trip every call.
        """
        if time.monotonic() - self._status_cache_ts < self._STATUS_TTL:
            return self._status_cache

        self._conn.flush_input()
        self._conn.send(_build_short_msg(MsgID.HW_START_UPDATEMSGS))
        self._ack()        # inline keep-alive (single-threaded, like Kinesis)
        time.sleep(0.25)   # Give controller time to send 2-3 updates at ~10 Hz

        # Short timeout so read() returns as soon as bytes arrive instead of
        # blocking the full 2-second serial timeout waiting for N bytes.
        old_timeout = self._conn._ser.timeout
        self._conn._ser.timeout = 0.1
        buf = self._conn._ser.read(128)   # up to 8 × 16-byte messages
        self._conn._ser.timeout = old_timeout

        # Stop the stream and drain any bytes still in flight.
        self._conn.send(_build_short_msg(MsgID.HW_STOP_UPDATEMSGS))
        time.sleep(0.15)  # 150 ms >> 16 ms USB latency; all in-flight bytes arrive
        n = self._conn._ser.in_waiting
        if n:
            self._conn._ser.read(n)
        self._conn.flush_input()

        # Keep the last valid status word found in the streamed messages, and
        # cache position/voltage from the same update (Kinesis reads them here).
        upd = self._latest_update(buf)
        if upd is not None:
            self._last_volts_raw, self._last_pos_raw, bits = upd
        else:
            bits = 0
        result = PZStatus(bits)
        self._status_cache = result
        self._status_cache_ts = time.monotonic()
        return result

    # ------------------------------------------------------------------
    # Output channel enable / disable

    def enable_output(self) -> None:
        self._conn.send(
            _build_short_msg(MsgID.MOD_SET_CHANENABLESTATE,
                             param1=self._chan, param2=0x01)
        )

    def disable_output(self) -> None:
        self._conn.send(
            _build_short_msg(MsgID.MOD_SET_CHANENABLESTATE,
                             param1=self._chan, param2=0x02)
        )

    # ------------------------------------------------------------------
    # Convenience scanning helpers

    def scan(
        self,
        start_um: float,
        stop_um: float,
        steps: int,
        dwell_s: float = 0.1,
        callback=None,
    ) -> list[tuple[float, float]]:
        """
        Step through positions from start_um to stop_um (closed-loop).
        callback(pos_um, step_idx) is called at each step if provided.
        Returns list of (commanded_um, actual_um) pairs.
        """
        self.enable_closed_loop()
        results = []
        positions = [
            start_um + (stop_um - start_um) * i / (steps - 1)
            for i in range(steps)
        ]
        for idx, pos in enumerate(positions):
            self.move_to(pos)
            time.sleep(dwell_s)
            actual = self.get_position()
            results.append((pos, actual))
            if callback:
                callback(pos, idx)
        return results

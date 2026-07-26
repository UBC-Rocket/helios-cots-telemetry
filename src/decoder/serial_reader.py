"""
Serial port reader/writer with COBS framing.

Owns the connection lifecycle and exposes a single blocking call —
read_packet() — that returns one complete COBS frame at a time, plus
write_frame() for sending framed uplink commands back out the same port.
"""

import sys
import threading
import time
from typing import Generator

import serial


_MAX_PACKET_BYTES = 4096
_COBS_DELIMITER = 0x00
_RECONNECT_DELAY = 5.0  # Seconds to wait before retrying connection
_RECONNECT_MAX_RETRIES = 0  # Max retries before giving up (0 = infinite)


class SerialReader:
  """
  Opens a serial port and yields raw COBS-encoded frames (without the
  0x00 delimiter) via read_packet().

  The link is full duplex: write_frame() sends uplink frames on the same
  port while read_packet() is blocked waiting on the downlink, so the two
  are expected to run on different threads.

  Args:
    port:     Serial device path (e.g. /dev/ttyUSB0, COM3).
    baud:     Baud rate. Defaults to 57600.
    timeout:  Per-byte read timeout in seconds. Defaults to 1.0.
  """

  def __init__(self, port: str, baud: int = 57600, timeout: float = 1.0) -> None:
    self._port = port
    self._baud = baud
    self._timeout = timeout
    self._ser: serial.Serial | None = None
    # Guards `_ser` so a write can't land on a handle the reader thread is
    # swapping out mid-reconnect. Never held across a reconnect's sleep.
    self._write_lock = threading.Lock()


  def __enter__(self) -> "SerialReader":
    self._open_port_with_retry()
    return self

  def __exit__(self, *_) -> None:
    with self._write_lock:
      if self._ser and self._ser.is_open:
        self._ser.close()


  def _open_port_with_retry(self) -> None:
    """
    Attempt to open the serial port, retrying if unavailable.
    Waits and retries up to _RECONNECT_MAX_RETRIES times.
    """
    retries = 0
    while True:
      try:
        ser = serial.Serial(self._port, self._baud, timeout=self._timeout)
        with self._write_lock:
          self._ser = ser
        print(f"[INFO] Connected to {self._port} at {self._baud} baud", file=sys.stderr, flush=True)
        return
      except serial.SerialException as exc:
        retries += 1
        if _RECONNECT_MAX_RETRIES > 0 and retries >= _RECONNECT_MAX_RETRIES:
          print(
            f"[ERROR] Failed to open {self._port} after {retries} attempts",
            file=sys.stderr,
            flush=True,
          )
          raise
        
        print(
          f"[WARNING] Cannot open {self._port} (attempt {retries}/{_RECONNECT_MAX_RETRIES if _RECONNECT_MAX_RETRIES > 0 else '∞'}). "
          f"Retrying in {_RECONNECT_DELAY}s...",
          file=sys.stderr,
          flush=True,
        )
        time.sleep(_RECONNECT_DELAY)


  def read_packet(self) -> bytes | None:
    """
    Block until a complete COBS frame arrives (delimited by 0x00).

    Returns:
      Raw COBS-encoded bytes (delimiter stripped), or None on timeout/disconnect.
      Raises SerialException if port is permanently unavailable.
    """
    assert self._ser is not None, "SerialReader must be used as a context manager"

    buffer = bytearray()

    while True:
      try:
        byte = self._ser.read(1)

        if not byte: # Read timeout — report only if we had a partial packet
          if buffer:
            print(
              f"[WARNING] Timeout with {len(buffer)} bytes in buffer",
              file=sys.stderr,
              flush=True,
            )
          return None

        if byte[0] == _COBS_DELIMITER:
          if buffer:
            return bytes(buffer)
          continue  # Empty frame between delimiters — keep reading

        buffer.append(byte[0])

        if len(buffer) > _MAX_PACKET_BYTES:
          print("[ERROR] Buffer overflow, discarding packet", file=sys.stderr, flush=True)
          buffer.clear()

      except serial.SerialException as exc:
        print(
          f"[ERROR] Serial port disconnected: {exc}",
          file=sys.stderr,
          flush=True,
        )
        raise

  def write_frame(self, frame: bytes) -> None:
    """
    Send one already-framed packet out the port and block until it is flushed.

    Blocking is deliberate: the caller reports a command as uplinked only once
    the bytes have actually left for the modem.

    Args:
      frame: Complete wire frame, delimiter included — see
             decoder.uplink.encode_command_frame().

    Raises:
      SerialException: If the port is closed or the write fails.
    """
    with self._write_lock:
      ser = self._ser
      if ser is None or not ser.is_open:
        raise serial.SerialException(f"{self._port} is not open")

      ser.write(frame)
      ser.flush()


  def packets(self) -> Generator[bytes, None, None]:
    """
    Convenience generator — yields non-None packets indefinitely.
    Automatically reconnects if the port is disconnected.

    Usage:
      with SerialReader(port, baud) as reader:
        for raw in reader.packets():
          ...
    """
    while True:
      try:
        raw = self.read_packet()
        if raw is not None:
          yield raw
      except serial.SerialException:
        print(
          f"[WARNING] Port disconnected. Attempting to reconnect...",
          file=sys.stderr,
          flush=True,
        )
        self._open_port_with_retry()
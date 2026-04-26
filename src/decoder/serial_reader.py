"""
Serial port reader with COBS framing.

Owns the connection lifecycle and exposes a single blocking call —
read_packet() — that returns one complete COBS frame at a time.
"""

import sys
import time
from typing import Generator

import serial


_MAX_PACKET_BYTES = 4096
_COBS_DELIMITER = 0x00
_RECONNECT_DELAY = 2.0  # Seconds to wait before retrying connection
_RECONNECT_MAX_RETRIES = 10  # Max retries before giving up (0 = infinite)


class SerialReader:
  """
  Opens a serial port and yields raw COBS-encoded frames (without the
  0x00 delimiter) via read_packet().

  Args:
    port:     Serial device path (e.g. /dev/ttyUSB0, COM3).
    baud:     Baud rate. Defaults to 115200.
    timeout:  Per-byte read timeout in seconds. Defaults to 1.0.
  """

  def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0) -> None:
    self._port = port
    self._baud = baud
    self._timeout = timeout
    self._ser: serial.Serial | None = None


  def __enter__(self) -> "SerialReader":
    self._open_port_with_retry()
    return self

  def __exit__(self, *_) -> None:
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
        self._ser = serial.Serial(self._port, self._baud, timeout=self._timeout)
        print(f"[INFO] Connected to {self._port} at {self._baud} baud", file=sys.stderr)
        return
      except serial.SerialException as exc:
        retries += 1
        if _RECONNECT_MAX_RETRIES > 0 and retries >= _RECONNECT_MAX_RETRIES:
          print(
            f"[ERROR] Failed to open {self._port} after {retries} attempts",
            file=sys.stderr,
          )
          raise
        
        print(
          f"[WARNING] Cannot open {self._port} (attempt {retries}/{_RECONNECT_MAX_RETRIES if _RECONNECT_MAX_RETRIES > 0 else '∞'}). "
          f"Retrying in {_RECONNECT_DELAY}s...",
          file=sys.stderr,
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
            )
          return None

        if byte[0] == _COBS_DELIMITER:
          if buffer:
            return bytes(buffer)
          continue  # Empty frame between delimiters — keep reading

        buffer.append(byte[0])

        if len(buffer) > _MAX_PACKET_BYTES:
          print("[ERROR] Buffer overflow, discarding packet", file=sys.stderr)
          buffer.clear()

      except serial.SerialException as exc:
        print(
          f"[ERROR] Serial port disconnected: {exc}",
          file=sys.stderr,
        )
        raise

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
        )
        self._open_port_with_retry()
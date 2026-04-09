"""
Serial port reader with COBS framing.

Owns the connection lifecycle and exposes a single blocking call —
read_packet() — that returns one complete COBS frame at a time.
"""

import sys
from typing import Generator

import serial


_MAX_PACKET_BYTES = 4096
_COBS_DELIMITER = 0x00


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
    self._ser = serial.Serial(self._port, self._baud, timeout=self._timeout)
    return self

  def __exit__(self, *_) -> None:
    if self._ser and self._ser.is_open:
      self._ser.close()


  def read_packet(self) -> bytes | None:
    """
    Block until a complete COBS frame arrives (delimited by 0x00).

    Returns:
      Raw COBS-encoded bytes (delimiter stripped), or None on timeout.
    """
    assert self._ser is not None, "SerialReader must be used as a context manager"

    buffer = bytearray()

    while True:
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

  def packets(self) -> Generator[bytes, None, None]:
    """
    Convenience generator — yields non-None packets indefinitely.

    Usage:
      with SerialReader(port, baud) as reader:
        for raw in reader.packets():
          ...
    """
    while True:
      raw = self.read_packet()
      if raw is not None:
        yield raw
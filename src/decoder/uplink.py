"""
Command uplink framing: protobuf → CRC-16 → COBS.

This is the exact inverse of decoder.packet, because FALCON frames both
directions of the link identically. Its RX path
(firmware/src/radio/command_thread.c, decode_rx_payload) reads bytes up to the
first 0x00 delimiter, COBS-decodes them, strips a trailing little-endian
CRC-16 computed with Zephyr's crc16_ccitt(0x0000, ...), then nanopb-parses a
GroundCommand — so a frame built here has the same layout as the telemetry
frames the rocket sends the other way.
"""

from cobs import cobs

from decoder.packet import crc16
from generated import GroundCommand


# Wire-frame delimiter. COBS output never contains a zero byte, so this is what
# FALCON scans for to find the end of a frame.
_COBS_DELIMITER = 0x00

# GNSS_SPI_MAX_COBS_SIZE (firmware/src/radio/gnss_spi.h) — the fixed RX buffer
# FALCON scans for the delimiter. A frame whose delimiter falls outside it is
# logged as "Unterminated COBS frame" and dropped, so the delimiter counts
# against this budget.
_MAX_FRAME_BYTES = 256

# MAX_FRAME_SIZE (firmware/src/radio/command_thread.c) — FALCON's COBS decode
# destination buffer, which has to hold the protobuf plus its 2-byte CRC.
_MAX_DECODED_BYTES = 253


class FrameTooLargeError(ValueError):
  """The command does not fit in FALCON's fixed-size receive buffers."""


def encode_command_frame(payload: bytes) -> bytes:
  """
  Wrap a serialized GroundCommand in the frame FALCON expects on the radio.

  Args:
    payload: Serialized GroundCommand protobuf bytes, forwarded verbatim.

  Returns:
    COBS-encoded bytes including the trailing 0x00 delimiter, ready to write
    straight to the RFD serial port.

  Raises:
    FrameTooLargeError: If the frame would overrun FALCON's receive buffers.
  """
  body = payload + crc16(payload).to_bytes(2, byteorder="little")

  if len(body) > _MAX_DECODED_BYTES:
    raise FrameTooLargeError(
      f"protobuf + CRC is {len(body)} bytes, over FALCON's "
      f"{_MAX_DECODED_BYTES}-byte decode buffer"
    )

  frame = cobs.encode(body) + bytes([_COBS_DELIMITER])

  if len(frame) > _MAX_FRAME_BYTES:
    raise FrameTooLargeError(
      f"COBS frame is {len(frame)} bytes, over FALCON's "
      f"{_MAX_FRAME_BYTES}-byte receive buffer"
    )

  return frame


def describe_command(payload: bytes) -> str:
  """
  Best-effort one-line summary of a command, for logging only.

  Never raises: an unparseable payload still gets relayed, since the ground
  station — not this relay — decides what is worth putting on the air.
  """
  try:
    cmd = GroundCommand.parse(payload)
  except Exception:
    return f"unparseable command ({len(payload)} bytes)"

  if cmd.camera is not None:
    fields = _named_fields(
      power=cmd.camera.vtx_runcam_power,
      recording=cmd.camera.camera_recording,
    )
    detail = f"camera({fields})"
  elif cmd.rfd_config is not None:
    fields = _named_fields(
      min_freq_khz=cmd.rfd_config.min_freq_khz,
      max_freq_khz=cmd.rfd_config.max_freq_khz,
      net_id=cmd.rfd_config.net_id,
      tx_power_dbm=cmd.rfd_config.tx_power_dbm,
      air_speed_kbps=cmd.rfd_config.air_speed_kbps,
      num_channels=cmd.rfd_config.num_channels,
    )
    detail = f"rfd_config({fields})"
  else:
    detail = "no payload"

  return (
    f"#{cmd.command_id} {detail} "
    f"from '{cmd.operator}' at {cmd.issued_at_ms}ms"
  )


def _named_fields(**fields: object) -> str:
  """Render only the oneof fields the operator actually set."""
  present = [f"{name}={value}" for name, value in fields.items() if value is not None]
  return ", ".join(present) if present else "unset"

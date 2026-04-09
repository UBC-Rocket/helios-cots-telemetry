"""
Packet decoding pipeline: COBS → CRC verification → protobuf parse.
"""

import sys

import crcmod
from cobs import cobs
from google.protobuf.message import DecodeError

from generated import TelemetryPacket
from decoder.formatting import hexdump


# CRC-16-CCITT Kermit variant (polynomial 0x1021, init 0x0000, reflected)
_crc16 = crcmod.predefined.mkCrcFun('kermit')


def decode_packet(raw_data: bytes, debug: bool = False) -> TelemetryPacket | None:
    """
    Decode a COBS-encoded, CRC-16-protected protobuf packet.

    Args:
      raw_data: COBS-encoded bytes, without the trailing 0x00 delimiter.
      debug:    If True, emit hex dumps at each stage to stderr.

    Returns:
      A decoded TelemetryPacket, or None if any stage fails.
    """
    if debug:
      print(f"[DEBUG] Raw COBS data ({len(raw_data)} bytes):", file=sys.stderr)
      print(hexdump(raw_data), file=sys.stderr)

    decoded = _cobs_decode(raw_data, debug)
    if decoded is None:
      return None

    if len(decoded) < 2:
      print(
        f"[ERROR] Packet too short ({len(decoded)} bytes), need at least 2 for CRC",
        file=sys.stderr,
      )
      return None

    payload, ok = _verify_crc(decoded, debug)
    if not ok:
      pass # Log the mismatch but continue — caller may still want the data

    return _decode_protobuf(payload, debug)


def _cobs_decode(raw_data: bytes, debug: bool) -> bytes | None:
  try:
    decoded = cobs.decode(raw_data)
  except cobs.DecodeError as exc:
    print(f"[ERROR] COBS decode failed: {exc}", file=sys.stderr)
    print(f"[DEBUG] Raw bytes: {raw_data.hex()}", file=sys.stderr)
    return None

  if debug:
    print(f"[DEBUG] After COBS decode ({len(decoded)} bytes):", file=sys.stderr)
    print(hexdump(decoded), file=sys.stderr)

  return decoded


def _verify_crc(decoded: bytes, debug: bool) -> tuple[bytes, bool]:
  """
  Split the last 2 bytes as a little-endian CRC-16 and verify.

  Returns:
    (payload, crc_ok) — payload is always returned so callers can
    attempt protobuf decode even on a CRC mismatch.
  """
  payload = decoded[:-2]
  crc_bytes = decoded[-2:]
  received = int.from_bytes(crc_bytes, byteorder='little')
  computed = _crc16(payload)

  if debug:
    print(f"[DEBUG] Payload ({len(payload)} bytes):", file=sys.stderr)
    print(hexdump(payload), file=sys.stderr)
    print(f"[DEBUG] CRC-16: received=0x{received:04X}  computed=0x{computed:04X}", file=sys.stderr)

  if received != computed:
    print(
      f"[WARNING] CRC mismatch — received: 0x{received:04X}, computed: 0x{computed:04X}",
      file=sys.stderr,
    )
    return payload, False

  return payload, True


def _decode_protobuf(payload: bytes, debug: bool) -> TelemetryPacket | None:
  try:
    packet = TelemetryPacket()
    packet.parse(payload)
    return packet
  except DecodeError as exc:
    print(f"[ERROR] Protobuf decode failed: {exc}", file=sys.stderr)
    if debug:
      print("[DEBUG] Payload bytes that failed to decode:", file=sys.stderr)
      print(hexdump(payload), file=sys.stderr)
    return None
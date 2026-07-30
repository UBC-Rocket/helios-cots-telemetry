"""
Ground-side RFD900x reconfiguration from an `rfd_config` GroundCommand.

FALCON applies an `rfd_config` to the rocket-side modem when the command comes
over the air. This module applies the same settings to the *ground* modem, so
the link comes back up on the new config instead of going deaf.

It runs after the command has already been uplinked — reconfiguring first would
change the ground modem before the rocket ever heard the command. See
main._relay_commands().

The modem is driven over the same serial port the downlink uses, via the SiK
escape sequence:

    <1s silence>  +++  <1s silence>  ->  OK        enter AT mode
    ATI5 / ATS<n>?                                 read current config
    ATS<n>=<value>                                 set each register
    AT&W                                           commit to EEPROM
    ATZ                                            reboot on the new config

AT mode is a request/response dialogue and cannot share the line with the
downlink, so the whole sequence happens inside a SerialReader.at_session(),
which stands the reader down for its duration (~5s of lost telemetry).
"""

import re
import sys
import time
from dataclasses import dataclass, field

from decoder.serial_reader import AtPort, SerialReader
from generated import GroundCommand, RfdConfig


# SiK requires the host line to be silent either side of the escape sequence;
# anything sent inside the guard window makes the modem treat +++ as data.
_GUARD_SECONDS = 1.0

# How long to wait for an OK/ERROR before calling a command failed.
_RESPONSE_TIMEOUT = 2.0

# ATZ reboots the modem; it answers nothing until it comes back.
_REBOOT_SECONDS = 2.0

# RfdConfig field -> S-register, per the proto comments. All 1:1, no unit
# conversion. Ordered so an aborted sequence still leaves a coherent band:
# frequency bounds and channel count land before net id and air speed.
_REGISTERS: tuple[tuple[str, int], ...] = (
  ("min_freq_khz",   8),
  ("max_freq_khz",   9),
  ("num_channels",  10),
  ("net_id",         3),
  ("tx_power_dbm",   4),
  ("air_speed_kbps", 2),
)

# A register value is the last thing on its own line — "25" from a bare ATS3?
# reply, or "25" from "S3:NETID=25" on firmware that echoes the name. Matching
# per line keeps the echoed "ATS3?" out of it, since its digits are followed
# by '?' rather than end-of-line. The trailing class has to include \r: the
# modem sends CRLF, and $ only matches ahead of the \n.
_VALUE_RE = re.compile(r"(?:^|\D)(\d+)[ \t\r]*$", re.MULTILINE)


class AtCommandError(RuntimeError):
  """The modem rejected a command, or did not answer one in time."""


@dataclass(frozen=True)
class RegisterSnapshot:
  """
  What the ground modem was set to immediately before we overwrote it.

  Held so the old config can be restored if the rocket never comes back on the
  new one. Nothing reverts automatically today — this is the record a future
  revert would read.
  """

  values: dict[int, int] = field(default_factory=dict)  # S-register -> value
  raw_dump: str = ""                                    # verbatim ATI5 output
  captured_at: float = 0.0                              # time.time()


_last_snapshot: RegisterSnapshot | None = None


def last_snapshot() -> RegisterSnapshot | None:
  """The most recent pre-write reading of the ground modem, if any."""
  return _last_snapshot


def extract_rfd_config(payload: bytes) -> RfdConfig | None:
  """
  Pull the RfdConfig out of a serialized GroundCommand, if it carries one.

  Best-effort by design: an unparseable payload is still relayed over the air
  (see uplink.describe_command), so it must not raise here either — it just
  means there is nothing to apply locally.
  """
  try:
    cmd = GroundCommand.parse(payload)
  except Exception:
    return None

  return cmd.rfd_config


def describe_rfd_config(cfg: RfdConfig) -> str:
  """Render only the fields that are actually set, for logging."""
  present = [
    f"{name}={value}"
    for name, _ in _REGISTERS
    if (value := getattr(cfg, name)) is not None
  ]
  return ", ".join(present) if present else "unset"


def apply_rfd_config(
  reader: SerialReader,
  cfg: RfdConfig,
  attempts: int = 2,
) -> None:
  """
  Reprogram the ground modem's S-registers to match `cfg`.

  Blocking — call it from a worker thread (asyncio.to_thread), never on the
  event loop. Holds the serial port exclusively for the whole call.

  Args:
    reader:   The open SerialReader owning the radio port.
    cfg:      Fields left unset are left alone on the modem.
    attempts: Total tries before giving up. Each failed attempt reboots the
              modem back to its saved config first.

  Raises:
    AtCommandError: Every attempt failed. The modem has been rebooted onto its
                    previously saved config, so the link is unchanged.
  """
  writes = [
    (register, value)
    for name, register in _REGISTERS
    if (value := getattr(cfg, name)) is not None
  ]

  if not writes:
    print("[RFD] rfd_config set no fields, nothing to apply", flush=True)
    return

  summary = ", ".join(f"S{register}={value}" for register, value in writes)
  print(f"[RFD] Applying to ground modem: {summary}", flush=True)

  with reader.at_session() as port:
    snapshot_taken = False

    for attempt in range(1, attempts + 1):
      in_at_mode = False
      try:
        _enter_at_mode(port)
        in_at_mode = True

        # Strictly before any write: once AT&W lands, the old values are gone
        # from EEPROM and there is nothing left to read back.
        if not snapshot_taken:
          _capture_snapshot(port, [register for register, _ in writes])
          snapshot_taken = True

        for register, value in writes:
          _command(port, f"ATS{register}={value}")

        _command(port, "AT&W")          # commit to EEPROM
        _reboot(port)                   # ATZ — comes back on the new config
        return

      except AtCommandError as exc:
        # Registers set before the failure are volatile until AT&W, so a reboot
        # puts the modem back exactly where it was. Only reachable if the
        # escape landed — otherwise ATZ would go out over the air as garbage.
        if in_at_mode:
          _reboot(port)

        if attempt == attempts:
          raise

        print(
          f"[RFD] Attempt {attempt}/{attempts} failed: {exc}. Retrying…",
          file=sys.stderr,
          flush=True,
        )


def _enter_at_mode(port: AtPort) -> None:
  """Escape into AT command mode, guard times included."""
  port.reset_input_buffer()
  time.sleep(_GUARD_SECONDS)
  port.write_raw(b"+++")
  time.sleep(_GUARD_SECONDS)
  _expect_ok(port, "+++")


def _command(port: AtPort, text: str) -> None:
  """Send one AT command and require an OK."""
  port.write_raw(f"{text}\r\n".encode())
  _expect_ok(port, text)


def _reboot(port: AtPort) -> None:
  """
  ATZ the modem and wait for it to come back.

  ATZ answers nothing — the modem is gone mid-reply — so there is no OK to
  wait for, only the reboot to sit out.
  """
  port.write_raw(b"ATZ\r\n")
  time.sleep(_REBOOT_SECONDS)
  port.reset_input_buffer()


def _capture_snapshot(port: AtPort, registers: list[int]) -> None:
  """
  Record what the modem is set to right now, for a possible later revert.

  Non-fatal: a modem that won't answer a query should not block the reconfig
  the operator asked for, so a failure here is logged and the previous
  snapshot (if any) is left in place.
  """
  global _last_snapshot

  try:
    values, raw_dump = _read_registers(port, registers)
  except Exception as exc:
    values, raw_dump = {}, ""
    print(f"[RFD] WARNING: reading current config failed ({exc})", file=sys.stderr, flush=True)

  if not values:
    print(
      "[RFD] WARNING: could not read current config — no revert values available",
      file=sys.stderr,
      flush=True,
    )
    return

  _last_snapshot = RegisterSnapshot(
    values=values,
    raw_dump=raw_dump,
    captured_at=time.time(),
  )

  held = ", ".join(f"S{r}={v}" for r, v in values.items())
  print(f"[RFD] Pre-write config: {held}  (revert values held in memory)", flush=True)
  if raw_dump:
    print(f"[RFD] ATI5 dump:\n{raw_dump}", flush=True)


def _read_registers(
  port: AtPort,
  registers: list[int],
) -> tuple[dict[int, int], str]:
  """
  Read S-registers off the modem, plus the full ATI5 dump for the log.

  Best-effort per register: one the modem won't answer for is left out of the
  result rather than failing the whole read, since a partial picture of the
  config is still worth more than none. Callers check for an empty dict.

  Must be called with the modem already in AT mode.
  """
  port.write_raw(b"ATI5\r\n")
  raw_dump = _read_reply(port, _RESPONSE_TIMEOUT).strip()

  values: dict[int, int] = {}
  for register in registers:
    port.write_raw(f"ATS{register}?\r\n".encode())
    reply = _read_reply(port, _RESPONSE_TIMEOUT, stop_on=("OK", "ERROR"))

    # Last match, not first: the value follows the modem's echo of the query.
    found = _VALUE_RE.findall(reply.replace("OK", ""))
    if "ERROR" in reply or not found:
      print(
        f"[RFD] WARNING: no value read for S{register}: {reply.strip()!r}",
        file=sys.stderr,
        flush=True,
      )
      continue
    values[register] = int(found[-1])

  return values, raw_dump


def _expect_ok(port: AtPort, context: str) -> None:
  """
  Read until the modem answers OK, or raise.

  Scans the accumulated text rather than matching whole lines: SiK echoes what
  it is sent, and downlink bytes already in flight when the port went quiet can
  still be sitting in front of the reply.
  """
  reply = _read_reply(port, _RESPONSE_TIMEOUT, stop_on=("OK", "ERROR"))

  if "ERROR" in reply:
    raise AtCommandError(f"{context} rejected by modem: {reply.strip()!r}")
  if "OK" not in reply:
    raise AtCommandError(f"no response to {context} within {_RESPONSE_TIMEOUT}s")


def _read_reply(
  port: AtPort,
  timeout: float,
  stop_on: tuple[str, ...] = (),
) -> str:
  """
  Accumulate bytes until one of `stop_on` shows up or the deadline passes.

  With no `stop_on` it always runs the full timeout, which is what multi-line
  replies like ATI5 need — there is no terminator to watch for.
  """
  deadline = time.monotonic() + timeout
  buffer = bytearray()

  while time.monotonic() < deadline:
    buffer.extend(port.read_available())
    text = buffer.decode("ascii", errors="replace")
    if any(token in text for token in stop_on):
      return text

  return buffer.decode("ascii", errors="replace")

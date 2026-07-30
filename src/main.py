"""
Entry point for the serial telemetry decoder.

Configuration is read from environment variables first, with CLI flags
as overrides. This makes the service easy to configure in Docker Compose
without rebuilding the image.

Environment variables:
  SERIAL_PORT        Serial device path          (default: RADIO_PORT)
  SERIAL_BAUD        Baud rate                   (default: 57600)
  SERIAL_TIMEOUT     Per-byte read timeout (s)   (default: 1.0)
  CSV_OUTPUT_PATH    CSV log file path           (default: no logging)
  COMMAND_ADDRESSES  Comma-separated Helios addresses to take ground
                     commands from (default: COMMAND_ADDRESSES below)
"""

import argparse
import asyncio
import os
import sys
import contextlib
from collections.abc import AsyncIterator

import serial
from helios import HeliosClient
from helios.generated.helios.transport import Event

from decoder.csv_logger import CsvLogger
from decoder.formatting import print_compact, print_verbose
from decoder.packet import decode_packet
from decoder.rfd_config import apply_rfd_config, extract_rfd_config
from decoder.serial_reader import SerialReader
from decoder.uplink import FrameTooLargeError, describe_command, encode_command_frame

RADIO_PORT = "/dev/radio"

NODE_URI = "Helios.FALCON.SRAD_Telemetry"

# Helios event carrying a serialized GroundCommand (falcon-protos).
COMMAND_EVENT = "command"

# Addresses we take ground commands from. The core routes an EventPublish on an
# exact (address, event_name) match, so this has to be the address the publisher
# puts on the message — not the publisher's own node URI:
#
#   * helios-mission-control (Helios.Services.Mission_Control) publishes its
#     commands with override_address=NODE_URI, so they arrive here under our own
#     address. That is the path that carries traffic today.
#   * Mission_Control is subscribed as well, so commands still reach the radio
#     if a publisher drops the override and posts on its own node URI instead.
#
# A subscription to an address the core doesn't know is rejected with an
# EventError that the SDK only logs, so listing both costs nothing.
COMMAND_ADDRESSES = (
  NODE_URI,
  "Helios.Services.Mission_Control",
)

def build_config() -> argparse.Namespace:
  """Parse CLI args, falling back to environment variables for each option."""
  parser = argparse.ArgumentParser(
    description="Decode COBS/CRC/Protobuf telemetry packets from a serial port",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
  )
  parser.add_argument(
    "-p", "--port",
    default=RADIO_PORT,
    help="Serial device (e.g. /dev/ttyUSB0).  Env: SERIAL_PORT",
  )
  parser.add_argument(
    "-b", "--baud",
    type=int,
    default=int(os.environ.get("SERIAL_BAUD", 57600)),
    help="Baud rate.  Env: SERIAL_BAUD",
  )
  parser.add_argument(
    "-t", "--timeout",
    type=float,
    default=float(os.environ.get("SERIAL_TIMEOUT", 1.0)),
    help="Read timeout in seconds.  Env: SERIAL_TIMEOUT",
  )
  parser.add_argument(
    "-v", "--verbose",
    action="store_true",
    help="Print all fields (default: compact one-liner)",
  )
  parser.add_argument(
    "-d", "--debug",
    action="store_true",
    help="Hex-dump each decode stage to stderr",
  )
  parser.add_argument(
    "-o", "--output",
    default=os.environ.get("CSV_OUTPUT_PATH"),
    metavar="FILE",
    help="CSV log file path.  Env: CSV_OUTPUT_PATH",
  )
  parser.add_argument(
    "-c", "--command-address",
    action="append",
    metavar="ADDRESS",
    help=(
      "Helios address to take ground commands from; repeatable. "
      "Env: COMMAND_ADDRESSES (comma-separated)"
    ),
  )

  args = parser.parse_args()

  if not args.port:
    parser.error(
      "Serial port is required — pass -p/--port or set SERIAL_PORT"
    )

  if not args.command_address:
    from_env = os.environ.get("COMMAND_ADDRESSES", "")
    args.command_address = [
      a.strip() for a in from_env.split(",") if a.strip()
    ] or list(COMMAND_ADDRESSES)

  return args


async def _wait_first(*events: asyncio.Event) -> None:
  """Return as soon as any one of the given events is set."""
  tasks = [asyncio.create_task(e.wait()) for e in events]
  try:
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
  finally:
    for t in tasks:
      t.cancel()
      with contextlib.suppress(asyncio.CancelledError):
        await t


async def helios_manager(
  sdk: HeliosClient,
  ready: asyncio.Event,
  connection_lost: asyncio.Event,
  stop: asyncio.Event,
  retry_delays: tuple[int, ...] = (2, 5),
) -> None:
  """
  Manages the Helios connection lifecycle independently of the reader.

  Flow:
    1. Try to connect.
    2. On success  → set `ready`, then wait for either a `connection_lost`
                      signal (reader got a send failure) or a `stop` signal.
    3. On failure  → clear `ready`, back off, then loop.
    4. On stop     → disconnect and return.
  """
  attempt = 0

  while not stop.is_set():
    connection_lost.clear()
    try:
      await sdk.connect()
      ready.set()
      label = "Connected" if attempt == 0 else "Reconnected"
      print(f"[Helios] {label}", flush=True)
      attempt = 0

      # Stay here until the reader reports a dead connection or we shut down
      await _wait_first(connection_lost, stop)
      ready.clear()

      if stop.is_set():
        break

      print("[Helios] Connection lost — scheduling reconnect…", file=sys.stderr, flush=True)

    except Exception as e:
      ready.clear()
      delay = retry_delays[min(attempt, len(retry_delays) - 1)]
      label = "Initial connection" if attempt == 0 else "Reconnect"
      print(
        f"[Helios] {label} failed: {e}. Retrying in {delay}s…",
        file=sys.stderr,
        flush=True,
      )
      attempt += 1
      # Interruptible sleep — exits early if stop fires
      with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay)

  ready.clear()
  with contextlib.suppress(Exception):
    await sdk.disconnect()
  print("[Helios] Manager exited.", flush=True)


async def _relay_commands(
  events: AsyncIterator[Event],
  address: str,
  reader: SerialReader,
) -> None:
  """
  Forward every command on one subscription to the radio.

  The Helios payload is already a serialized GroundCommand, so it is framed and
  relayed byte-for-byte — re-encoding it here would risk dropping fields this
  build's protos don't know about yet.

  One bad command must never take the relay down with it, so every failure is
  logged and skipped: a dropped command can be re-sent by the operator, but a
  dead relay silently ignores the rest of the flight.
  """
  async for event in events:
    try:
      payload = bytes(event.data or b"")
      if not payload:
        print(f"[Uplink] Empty command from {address}, ignored", file=sys.stderr, flush=True)
        continue

      summary = describe_command(payload)

      try:
        frame = encode_command_frame(payload)
      except FrameTooLargeError as e:
        print(f"[Uplink] Dropped {summary}: {e}", file=sys.stderr, flush=True)
        continue

      try:
        await asyncio.to_thread(reader.write_frame, frame)
      except Exception as e:
        # A dead port is the reader's problem to reconnect; drop this command
        # and keep the subscription alive so the next one still gets a chance.
        print(f"[Uplink] Radio write failed for {summary}: {e}", file=sys.stderr, flush=True)
        continue

      print(f"[Uplink] Sent {summary} to RFD ({len(frame)} bytes)", flush=True)

      # Now that the rocket has the command, bring the ground modem onto the
      # same settings. Strictly after the send: reconfiguring first would move
      # the ground modem off the frequency the command still had to go out on.
      cfg = extract_rfd_config(payload)
      if cfg is not None:
        try:
          await asyncio.to_thread(apply_rfd_config, reader, cfg)
          print("[RFD] Ground modem reconfigured", flush=True)
        except Exception as e:
          # The modem was rebooted back onto its saved config, so the link is
          # no worse off than before — log it and keep the relay alive.
          print(
            f"[RFD] Ground reconfig failed for {summary}: {e}",
            file=sys.stderr,
            flush=True,
          )

    except Exception as e:
      print(
        f"[Uplink] Unexpected error handling a command from {address}: "
        f"{type(e).__name__}: {e}",
        file=sys.stderr,
        flush=True,
      )


async def command_uplink(
  sdk: HeliosClient,
  reader: SerialReader,
  addresses: list[str],
  ready: asyncio.Event,
  connection_lost: asyncio.Event,
  stop: asyncio.Event,
) -> None:
  """
  Subscribe to ground commands and relay them out the radio port.

  Shares helios_manager's lifecycle events rather than managing its own
  connection: it waits for a live link, holds the subscriptions open for as
  long as that link lasts, and re-subscribes after each reconnect — Helios
  drops a client's subscriptions when it disconnects.
  """
  while not stop.is_set():
    await _wait_first(ready, stop)
    if stop.is_set():
      break

    tasks: list[asyncio.Task] = []
    try:
      async with contextlib.AsyncExitStack() as stack:
        for address in addresses:
          events = await stack.enter_async_context(
            sdk.subscribe_event(address=address, event_name=COMMAND_EVENT)
          )
          tasks.append(asyncio.create_task(_relay_commands(events, address, reader)))

        print(
          f"[Uplink] Listening for '{COMMAND_EVENT}' on {', '.join(addresses)}",
          flush=True,
        )

        # Hold the subscriptions open until the link drops or we shut down
        await _wait_first(connection_lost, stop)

    except Exception as e:
      print(f"[Uplink] Subscription failed: {e}", file=sys.stderr, flush=True)
    finally:
      # Teardown must not fail: awaiting a relay that died re-raises its
      # exception, which would otherwise escape and end the relay for good.
      for task in tasks:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
          await task

    # Give the manager a moment to reconnect before trying to re-subscribe
    if not stop.is_set():
      with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=1.0)

  print("[Uplink] Relay exited.", flush=True)


async def main_loop(args: argparse.Namespace) -> None:
  """Main loop — read packets, decode them, log and display."""
  print(f"Opening {args.port} at {args.baud} baud…", flush=True)

  helios_sdk = HeliosClient(
    core_address="Helios",
    core_port=5000,
    node_uri=NODE_URI,
  )

  # Shared coordination events
  helios_ready      = asyncio.Event()   # set = currently connected
  connection_lost   = asyncio.Event()   # reader sets this on send failure
  stop              = asyncio.Event()   # graceful shutdown signal

  logger_ctx    = CsvLogger(args.output) if args.output else _NullLogger()
  serial_reader = SerialReader(args.port, args.baud, args.timeout)

  # Helios runs in the background — the reader never waits on it
  manager_task = asyncio.create_task(
    helios_manager(helios_sdk, helios_ready, connection_lost, stop)
  )
  uplink_task = asyncio.create_task(
    command_uplink(
      helios_sdk, serial_reader, args.command_address,
      helios_ready, connection_lost, stop,
    )
  )

  try:
    with serial_reader as reader, logger_ctx as logger:
      if args.output:
        print(f"Logging to {args.output}", flush=True)
      print("Connected. Listening for packets…\n", flush=True)

      packet_count = 0

      while True:
        raw = await asyncio.to_thread(next, reader.packets(), None)
        if raw is None or len(raw) < 15:   # drop malformed hardware frames
          continue

        packet_count += 1

        if args.debug:
          print(f"[{packet_count}] Raw COBS ({len(raw)} bytes): {raw.hex()}")

        packet = decode_packet(raw, debug=args.debug)
        if packet is None:
          continue

        # Helios send: non-blocking
        if helios_ready.is_set():
          try:
            await helios_sdk.publish_event(
              event_name="telemetry",
              data=bytes(packet),
            )
          except Exception as e:
            print(f"[Helios] Send failed: {e}", file=sys.stderr, flush=True)
            helios_ready.clear()
            connection_lost.set()   # wake the manager to reconnect

        if logger:
          logger.write(packet)

        if args.verbose:
          print_verbose(packet_count, packet)
        else:
          print_compact(packet_count, packet)

  except serial.SerialException as exc:
    print(f"\n[ERROR] Serial error: {exc}", file=sys.stderr, flush=True)
    print("[ERROR] Failed to establish connection. Check port availability.", file=sys.stderr, flush=True)
  except KeyboardInterrupt:
    print("\nExiting…", flush=True)
    if args.output:
        print(f"CSV saved to {args.output}", flush=True)
  except Exception as exc:
    print(f"\n[ERROR] Unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
  finally:
    stop.set()                           # tell the background tasks to exit cleanly
    # Uplink first: it unsubscribes over a connection the manager still owns
    await uplink_task
    await manager_task                   # wait for it to disconnect and return

# Used when CSV logging is disabled
class _NullLogger:
  def __enter__(self): return None
  def __exit__(self, *_): pass


if __name__ == "__main__":
  args = build_config()
  try:
    asyncio.run(main_loop(args))
  except KeyboardInterrupt:
    pass
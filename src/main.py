"""
Entry point for the serial telemetry decoder.

Configuration is read from environment variables first, with CLI flags
as overrides. This makes the service easy to configure in Docker Compose
without rebuilding the image.

Environment variables:
  SERIAL_PORT        Serial device path          (default: RADIO_PORT)
  SERIAL_BAUD        Baud rate                   (default: 115200)
  SERIAL_TIMEOUT     Per-byte read timeout (s)   (default: 1.0)
  CSV_OUTPUT_PATH    CSV log file path           (default: no logging)
"""

import argparse
import asyncio
import os
import sys
import contextlib

import serial
from helios import HeliosClient

from decoder.csv_logger import CsvLogger
from decoder.formatting import print_compact, print_verbose
from decoder.packet import decode_packet
from decoder.serial_reader import SerialReader

RADIO_PORT = "/dev/radio"

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
    default=int(os.environ.get("SERIAL_BAUD", 115200)),
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

  args = parser.parse_args()

  if not args.port:
    parser.error(
      "Serial port is required — pass -p/--port or set SERIAL_PORT"
    )

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
      print(f"[Helios] {label}")
      attempt = 0

      # Stay here until the reader reports a dead connection or we shut down
      await _wait_first(connection_lost, stop)
      ready.clear()

      if stop.is_set():
        break

      print("[Helios] Connection lost — scheduling reconnect…", file=sys.stderr)

    except Exception as e:
      ready.clear()
      delay = retry_delays[min(attempt, len(retry_delays) - 1)]
      label = "Initial connection" if attempt == 0 else "Reconnect"
      print(
        f"[Helios] {label} failed: {e}. Retrying in {delay}s…",
        file=sys.stderr,
      )
      attempt += 1
      # Interruptible sleep — exits early if stop fires
      with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay)

  ready.clear()
  with contextlib.suppress(Exception):
    await sdk.disconnect()
  print("[Helios] Manager exited.")


async def main_loop(args: argparse.Namespace) -> None:
  """Main loop — read packets, decode them, log and display."""
  print(f"Opening {args.port} at {args.baud} baud…")

  helios_sdk = HeliosClient(
    core_address="Helios",
    core_port=5000,
    node_uri="Helios.FALCON.Telemetry",
  )

  # Shared coordination events
  helios_ready      = asyncio.Event()   # set = currently connected
  connection_lost   = asyncio.Event()   # reader sets this on send failure
  stop              = asyncio.Event()   # graceful shutdown signal

  # Helios runs in the background — the reader never waits on it
  manager_task = asyncio.create_task(
    helios_manager(helios_sdk, helios_ready, connection_lost, stop)
  )

  logger_ctx    = CsvLogger(args.output) if args.output else _NullLogger()
  serial_reader = SerialReader(args.port, args.baud, args.timeout)

  try:
    with serial_reader as reader, logger_ctx as logger:
      if args.output:
        print(f"Logging to {args.output}")
      print("Connected. Listening for packets…\n")

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
              data=bytes(raw),
            )
          except Exception as e:
            print(f"[Helios] Send failed: {e}", file=sys.stderr)
            helios_ready.clear()
            connection_lost.set()   # wake the manager to reconnect

        if logger:
          logger.write(packet)

        if args.verbose:
          print_verbose(packet_count, packet)
        else:
          print_compact(packet_count, packet)

  except serial.SerialException as exc:
    print(f"\n[ERROR] Serial error: {exc}", file=sys.stderr)
    print("[ERROR] Failed to establish connection. Check port availability.", file=sys.stderr)
  except KeyboardInterrupt:
    print("\nExiting…")
    if args.output:
        print(f"CSV saved to {args.output}")
  except Exception as exc:
    print(f"\n[ERROR] Unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
  finally:
    stop.set()                           # tell the manager to exit cleanly
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
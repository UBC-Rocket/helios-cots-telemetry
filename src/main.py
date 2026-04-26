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
import os
import sys

import serial
import helios_python_sdk as helios

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


def run(args: argparse.Namespace) -> None:
  """Main loop — read packets, decode them, log and display."""
  print(f"Opening {args.port} at {args.baud} baud...")

  # CsvLogger is a no-op context manager substitute when logging is disabled
  logger_ctx = CsvLogger(args.output) if args.output else _NullLogger()
  serial_reader = SerialReader(args.port, args.baud, args.timeout)
  helios_sdk = helios.HeliosClient(retry=999, retry_delay=5.0)

  try:
    with serial_reader as reader, logger_ctx as logger, helios_sdk as client:
      if args.output:
        print(f"Logging to {args.output}")
      print("Connected. Listening for packets...\n")

      packet_count = 0
      for raw in reader.packets():
        packet_count += 1

        if args.debug:
          print(f"[{packet_count}] Raw COBS ({len(raw)} bytes): {raw.hex()}")

        # TODO: Send packet to SDK here

        packet = decode_packet(raw, debug=args.debug)
        if packet is None:
          continue

        if logger:
          logger.write(packet)

        if args.verbose:
          print_verbose(packet_count, packet)
        else:
          print_compact(packet_count, packet)

  except serial.SerialException as exc:
    print(f"\n[ERROR] Serial error: {exc}", file=sys.stderr)
    print("[ERROR] Failed to establish connection. Check port availability.", file=sys.stderr)
    sys.exit(1)
  except KeyboardInterrupt:
    print("\nExiting...")
    if args.output:
      print(f"CSV saved to {args.output}")
  except Exception as exc:
    print(f"\n[ERROR] Unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)


# Used when CSV logging is disabled
class _NullLogger:
  def __enter__(self): return None
  def __exit__(self, *_): pass


if __name__ == "__main__":
  run(build_config())
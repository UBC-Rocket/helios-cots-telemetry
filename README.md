# Helios COTS Telemetry Decoder

A Python-based telemetry decoder for COTS (Commercial Off-The-Shelf) satellite systems, providing packet decoding, parsing, and logging capabilities.

## Features

- **Protocol Buffer Support**: Message serialization and deserialization using Protocol Buffers
- **Serial Communication**: Read and decode telemetry data from serial interfaces
- **Command Uplink**: Relay ground commands from Helios out the RFD to FALCON
- **Multiple Output Formats**: CSV logging and structured data formatting
- **COBS Encoding**: Support for Consistent Overhead Byte Stuffing
- **CRC Validation**: Data integrity checking with CRC module
- **Dockerized**: Containerized deployment support

## Prerequisites

- Python 3.13 or higher
- pip or uv package manager

## Installation

1. Clone the repository:
```bash
git clone https://github.com/UBC-Rocket/helios-cots-telemetry
cd helios-cots-telemetry
```

2. Install dependencies:
```bash
make deps
```

Or using uv:
```bash
uv sync
```

## Usage

### Running the Decoder

```bash
# Basic usage - read from serial port
uv run src/main.py -p /dev/ttyUSB0

# With custom baud rate (default: 57600)
uv run src/main.py -p /dev/ttyACM0 -b 9600

# Verbose mode (shows raw hex data)
uv run src/main.py -p /dev/ttyUSB0 -v

# With custom timeout (in seconds)
uv run src/main.py -p /dev/ttyUSB0 -t 2.0

# Log output to CSV file
uv run src/main.py -p /dev/ttyUSB0 -o telemetry.csv

# Using environment variables
SERIAL_PORT=/dev/ttyUSB0 SERIAL_BAUD=57600 uv run src/main.py

# Take ground commands from a different Helios address (repeatable)
uv run src/main.py -c Helios.Services.Mission_Control
```

### Configuration via Environment Variables

The decoder can be configured using environment variables (useful for Docker):
- `SERIAL_BAUD` - Baud rate (default: `57600`)
- `SERIAL_TIMEOUT` - Per-byte read timeout in seconds (default: `1.0`)
- `CSV_OUTPUT_PATH` - Path for CSV log file (optional)
- `COMMAND_ADDRESSES` - Comma-separated Helios addresses to take ground commands from (default: `Helios.FALCON.SRAD_Telemetry,Helios.Services.Mission_Control`)

## Command Uplink

The decoder is bidirectional. Alongside publishing decoded telemetry to Helios as
`telemetry`, it subscribes to the `command` event and relays each one out the RFD
on the same serial port, where the FALCON firmware
([UBC-Rocket/FALCON](https://github.com/UBC-Rocket/FALCON)) receives it.

The Helios payload is already a serialized `GroundCommand` (from `falcon-protos`),
so it is forwarded byte-for-byte and only wrapped in the wire framing that FALCON's
`command_thread.c` expects — the same framing the rocket uses for its downlink:

```
[ GroundCommand protobuf | CRC-16 (little-endian) ]  ->  COBS  ->  0x00
```

The CRC is CRC-16-CCITT/KERMIT (poly `0x1021`, init `0x0000`, reflected), matching
Zephyr's `crc16_ccitt(0x0000, ...)`. Frames are size-checked against FALCON's fixed
receive buffers (253 bytes of protobuf + CRC, 256 bytes on the wire) and dropped
with a log line if they would overrun them; a realistic command is well under 60 bytes.

### Which address commands arrive on

Helios routes an event on an exact `(address, event_name)` match, using the address
the *publisher* puts on the message rather than its own node URI. `helios-mission-control`
publishes commands with `override_address=Helios.FALCON.SRAD_Telemetry`, so they arrive
under this node's own address. Both that and `Helios.Services.Mission_Control` are
subscribed by default, so commands still get through if a publisher posts on its own
node URI instead. Subscribing to an address the core doesn't know is harmless — the
core rejects it with an `EventError` that the SDK just logs.

### RFD reconfiguration

A `GroundCommand` carrying `rfd_config` is uplinked like any other command, and FALCON
applies it to the **rocket-side** modem. This decoder does **not** currently reprogram
the ground-side RFD, so an `rfd_config` command will drop the link until the ground
modem is changed to match by other means.

### Docker

Build and run using Docker:
```bash
docker build -t helios-telemetry .
docker run -e SERIAL_PORT=/dev/ttyUSB0 helios-telemetry
```

Or using the Makefile:
```bash
make run  
```

## Configuration

Edit `config.json` to customize:
- Serial port settings
- Baud rate
- Output file paths
- Logging parameters

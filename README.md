# Helios COTS Telemetry Decoder

A Python-based telemetry decoder for COTS (Commercial Off-The-Shelf) satellite systems, providing packet decoding, parsing, and logging capabilities.

## Features

- **Protocol Buffer Support**: Message serialization and deserialization using Protocol Buffers
- **Serial Communication**: Read and decode telemetry data from serial interfaces
- **Command Uplink**: Relay ground commands from Helios out the RFD to FALCON
- **Ground RFD Reconfiguration**: Apply an `rfd_config` command to the local modem over AT, after uplinking it, and report the result to Helios
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

A `GroundCommand` carrying `rfd_config` is uplinked like any other command — FALCON applies it
to the **rocket-side** modem — and then the decoder applies the same settings to the
**ground-side** modem, so both ends move together instead of the link going deaf.

Order matters: the frame goes out first, and only once it has flushed does the ground modem get
touched. Reconfiguring first would move the ground modem off the settings the command still had
to be transmitted on.

The ground modem is driven over the same serial port using the SiK escape sequence:

```
<1s silence>  +++  <1s silence>  ->  OK    enter AT mode
ATI5 / ATS<n>?                             read and log the current config
ATS<n>=<value>                             set each register the command sets
AT&W                                       commit to EEPROM
ATZ                                        reboot on the new config
```

Fields map straight onto S-registers, with no unit conversion. Only the fields the command
actually sets are written; the rest are left alone.

| `RfdConfig` field | register |
| --- | --- |
| `air_speed_kbps` | `S2` |
| `net_id` | `S3` |
| `tx_power_dbm` | `S4` |
| `min_freq_khz` | `S8` |
| `max_freq_khz` | `S9` |
| `num_channels` | `S10` |

AT mode is a request/response dialogue, so it cannot share the line with the downlink. For the
duration of the sequence — roughly 5 seconds — the reader stands down and any telemetry that
arrives is lost. Uplink writes are held off too, so a command arriving in the meantime waits
rather than interleaving into the dialogue.

Before writing anything, the current values of the registers about to change are read back and
logged, along with the full `ATI5` dump, and kept in memory (`rfd_config.last_snapshot()`) so the
old config is on hand if the rocket never comes back on the new one. Nothing reverts
automatically today. The snapshot has to happen before `AT&W`, since that erases the old values
from EEPROM.

If the sequence fails — no `OK` to `+++`, or a register the modem rejects — the whole thing is
retried once. Registers set before a failure are volatile until `AT&W`, so each failed attempt
ends in an `ATZ` that reboots the modem back onto its saved config, leaving the link exactly as
it was. A final failure is logged to stderr and nothing is published; the relay carries on.

### Reporting the ground config: `current_rfd_config`

The node publishes a `current_rfd_config` event describing what the **ground** modem is set to.
The payload is a bare serialized `RfdConfig` — deliberately *not* wrapped in a `GroundCommand`,
since this is the ground station reporting state rather than an operator issuing an order. A
subscriber can treat the most recent one as current.

It is published twice over a normal run:

- **At startup**, from a read of all six registers. The read happens as soon as the port opens,
  before waiting on Helios, so its few seconds of downlink cost land while nothing is flying —
  a Helios link that only came up mid-flight would otherwise trigger the blackout at the worst
  moment. The reading is then held until there is somewhere to send it. If Helios never
  connects, nothing is published.
- **After every successful reconfiguration**, from a second read taken once the modem has
  rebooted. Reading back rather than echoing the requested values confirms `AT&W` actually
  persisted, and fills in the registers the command left alone. This costs another AT session on
  top of one that already interrupted the downlink.

Reads are best-effort per register: one the modem won't answer for is logged and left unset on
the message rather than failing the whole thing. Since nothing is written, these sessions exit
with `ATO` rather than a reboot.

Note that `ATO` answers nothing — like `ATZ`, the modem is already transparent by the time a
reply would land, so an `OK` would go out over the air rather than come back to the host. Do not
wait for one and fall back to `ATZ` on the timeout: by then the modem is in data mode, so the
`ATZ` is transmitted as payload, parks at the front of FALCON's receive buffer, and corrupts the
next command frame into a CRC failure.

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

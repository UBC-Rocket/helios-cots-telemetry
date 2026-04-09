# Helios COTS Telemetry Decoder

A Python-based telemetry decoder for COTS (Commercial Off-The-Shelf) satellite systems, providing packet decoding, parsing, and logging capabilities.

## Features

- **Protocol Buffer Support**: Message serialization and deserialization using Protocol Buffers
- **Serial Communication**: Read and decode telemetry data from serial interfaces
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
python src/main.py -p /dev/ttyUSB0

# With custom baud rate (default: 115200)
python src/main.py -p /dev/ttyACM0 -b 9600

# Verbose mode (shows raw hex data)
python src/main.py -p /dev/ttyUSB0 -v

# With custom timeout (in seconds)
python src/main.py -p /dev/ttyUSB0 -t 2.0

# Log output to CSV file
python src/main.py -p /dev/ttyUSB0 -o telemetry.csv

# Using environment variables
SERIAL_PORT=/dev/ttyUSB0 SERIAL_BAUD=115200 python src/main.py
```

### Configuration via Environment Variables

The decoder can be configured using environment variables (useful for Docker):
- `SERIAL_PORT` - Serial device path (default: `/dev/radio`)
- `SERIAL_BAUD` - Baud rate (default: `115200`)
- `SERIAL_TIMEOUT` - Per-byte read timeout in seconds (default: `1.0`)
- `CSV_OUTPUT_PATH` - Path for CSV log file (optional)

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
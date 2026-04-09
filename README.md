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
python src/main.py
```

### Example

```bash
python src/example.py
```

### Docker

Build and run using Docker:
```bash
docker build -t helios-telemetry .
docker run helios-telemetry
```

Or using the Makefile:
```bash
make build
make run
```

## Configuration

Edit `config.json` to customize:
- Serial port settings
- Baud rate
- Output file paths
- Logging parameters
"""
CSV logger for TelemetryPacket data.
"""

import csv
from datetime import datetime
from pathlib import Path

from generated import TelemetryPacket
from decoder.formatting import flight_state_name


COLUMNS = [
  "recv_time",
  "counter",
  "timestamp_ms",
  "state",
  "accel_x",
  "accel_y",
  "accel_z",
  "gyro_x",
  "gyro_y",
  "gyro_z",
  "kf_altitude",
  "kf_velocity",
  "kf_alt_variance",
  "kf_vel_variance",
  "baro0_healthy",
  "baro1_healthy",
  "baro0_pressure",
  "baro0_temperature",
  "baro0_altitude",
  "baro0_nis",
  "baro0_faults",
  "baro1_pressure",
  "baro1_temperature",
  "baro1_altitude",
  "baro1_nis",
  "baro1_faults",
  "ground_altitude",
  "gps_latitude",
  "gps_longitude",
  "gps_altitude",
  "gps_speed",
  "gps_sats",
  "gps_fix",
]


def packet_to_row(packet: TelemetryPacket) -> list:
  """Convert a TelemetryPacket to a CSV row aligned with COLUMNS."""
  return [
    datetime.now().isoformat(timespec='milliseconds'),
    packet.counter,
    packet.timestamp_ms,
    flight_state_name(packet.state),
    packet.accel_x,
    packet.accel_y,
    packet.accel_z,
    packet.gyro_x,
    packet.gyro_y,
    packet.gyro_z,
    packet.kf_altitude,
    packet.kf_velocity,
    packet.kf_alt_variance,
    packet.kf_vel_variance,
    packet.baro0_healthy,
    packet.baro1_healthy,
    packet.baro0_pressure,
    packet.baro0_temperature,
    packet.baro0_altitude,
    packet.baro0_nis,
    packet.baro0_faults,
    packet.baro1_pressure,
    packet.baro1_temperature,
    packet.baro1_altitude,
    packet.baro1_nis,
    packet.baro1_faults,
    packet.ground_altitude,
    packet.gps_latitude,
    packet.gps_longitude,
    packet.gps_altitude,
    packet.gps_speed,
    packet.gps_sats,
    packet.gps_fix,
  ]


class CsvLogger:
  """
  Writes TelemetryPacket rows to a CSV file.

  Opens the file on __enter__ and writes the header row immediately,
  so a zero-packet run still produces a valid (header-only) CSV.

  Args:
    path: Destination file path.  Parent directories are created
      automatically if they do not exist.

  Usage:
    with CsvLogger("/data/telemetry.csv") as log:
      log.write(packet)
  """

  def __init__(self, path: str | Path) -> None:
    self._path = Path(path)
    self._file = None
    self._writer = None

  def __enter__(self) -> "CsvLogger":
    self._path.parent.mkdir(parents=True, exist_ok=True)
    self._file = open(self._path, "w", newline="")
    self._writer = csv.writer(self._file)
    self._writer.writerow(COLUMNS)
    return self

  def __exit__(self, *_) -> None:
    if self._file:
      self._file.close()

  def write(self, packet: TelemetryPacket) -> None:
    """Append one packet as a CSV row and flush immediately."""
    assert self._writer is not None, "CsvLogger must be used as a context manager"
    self._writer.writerow(packet_to_row(packet))
    if self._file:
      self._file.flush()
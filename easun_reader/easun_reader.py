#!/usr/bin/env python3

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import serial


# ---------------------------------------------------------------------------
# Protocol parsing
# ---------------------------------------------------------------------------

def fields(response: str):
    """Convert '(foo bar baz\\r' into ['foo', 'bar', 'baz']."""
    response = response.strip()

    if not response.startswith("("):
        raise ValueError(f"Unexpected response: {response!r}")

    return response[1:].split()


def require_count(command, f, count):
    if len(f) < count:
        raise ValueError(
            f"{command}: expected at least {count} fields, got {len(f)}: {f}"
        )


def parse_hop(response):
    f = fields(response)
    require_count("HOP", f, 9)

    return {
        "output_voltage": float(f[0]),
        "output_frequency": float(f[1]),
        "output_apparent_power": int(f[2]),
        "output_active_power": int(f[3]),
        "output_load": int(f[4]),
        "dc_bus_voltage": int(f[5]),
        "rated_power": int(f[6]),
        "output_current": float(f[7]),

        # Not fully decoded by the reverse-engineered protocol.
        "output_status_code": f[8],
    }


def parse_hgrid(response):
    f = fields(response)
    # Erweitert auf 7 Werte, um die Netzleistung (Bezug/Einspeisung) zu erfassen
    require_count("HGRID", f, 7)

    return {
        "grid_voltage": float(f[0]),
        "grid_frequency": float(f[1]),
        "grid_high_voltage_limit": int(f[2]),
        "grid_low_voltage_limit": int(f[3]),
        "grid_high_frequency_limit": int(f[4]),
        "grid_low_frequency_limit": int(f[5]),
        "grid_active_power": int(f[6]),
    }


def parse_hbat(response):
    f = fields(response)
    require_count("HBAT", f, 8)

    return {
        "battery_series_count": int(f[0]),
        "battery_voltage": float(f[1]),
        "battery_capacity": int(f[2]),

        # These two meanings are currently marked tentative in the
        # reverse-engineered documentation.
        "battery_charging_current_tentative": int(f[3]),
        "battery_discharging_current_tentative": int(f[4]),

        "battery_dc_bus_voltage": int(f[5]),

        # Keep the undecoded values available for investigation.
        "battery_flags_1": f[6],
        "battery_flags_2": f[7],
    }


def parse_hpv(response):
    f = fields(response)
    require_count("HPV", f, 9)

    return {
        "pv_voltage": float(f[0]),
        "pv_current": float(f[1]),
        "pv_power": int(f[2]),

        # f[3] and f[4] are not sufficiently understood.

        "pv_operating_mode_tentative": int(f[5]),
        "pv_max_voltage": float(f[6]),

        # f[7] has tentatively been identified as a PV temperature, but
        # HTEMP provides better-defined temperature fields.

        "pv_max_power": int(f[8]),
    }


def parse_htemp(response):
    f = fields(response)
    require_count("HTEMP", f, 8)

    return {
        "pv_temperature": int(f[0]),
        "inverter_temperature": int(f[1]),
        "boost_temperature": int(f[2]),

        # Tentative/unknown values retained as diagnostics.
        "fan_state_tentative": int(f[3]),
        "secondary_temperature_tentative": int(f[4]),
        "fan_1_speed_raw": int(f[5]),
        "fan_2_speed_raw": int(f[6]),
        "temperature_flags": f[7],
    }


def parse_hgen(response):
    f = fields(response)
    require_count("HGEN", f, 7)

    # Protocol research indicates these generation counters are most likely kWh.
    return {
        "inverter_date": f[0],       # YYMMDD
        "inverter_time": f[1],       # HH:MM
        "pv_energy_today": float(f[2]),
        "pv_energy_month": float(f[3]),
        "pv_energy_year": float(f[4]),
        "pv_energy_total": float(f[5]),
        "generation_flags": f[6],
    }


def parse_himsg1(response):
    f = fields(response)
    require_count("HIMSG1", f, 3)

    return {
        "software_version": f[0],
        "software_date": f[1],

        # Exact meaning not yet completely established.
        "firmware_variant": f[2],
    }


COMMANDS = {
    "HOP": parse_hop,
    "HGRID": parse_hgrid,
    "HBAT": parse_hbat,
    "HPV": parse_hpv,
    "HTEMP": parse_htemp,
    "HGEN": parse_hgen,
    "HIMSG1": parse_himsg1,
}


# ---------------------------------------------------------------------------
# Serial communication
# ---------------------------------------------------------------------------

def query(ser, command):
    ser.reset_input_buffer()

    request = (command + "\r").encode("ascii")
    ser.write(request)
    ser.flush()

    response = ser.read_until(b"\r")

    if not response:
        raise TimeoutError(f"{command}: no response")

    try:
        return response.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError(
            f"{command}: non-ASCII response: {response.hex(' ')}"
        )


def poll_inverter(ser):
    result = {}
    raw = {}
    errors = {}
    successful = 0

    for command, parser in COMMANDS.items():
        try:
            response = query(ser, command)
            raw[command] = response.rstrip("\r")

            values = parser(response)
            result.update(values)

            successful += 1

        except Exception as exc:
            errors[command] = str(exc)

        # Do not hammer the inverter serial interface.
        time.sleep(0.10)

    result["_raw"] = raw

    if errors:
        result["_errors"] = errors

    result["timestamp"] = datetime.now(timezone.utc).isoformat()

    return result, successful


# ---------------------------------------------------------------------------
# Home Assistant MQTT discovery
# ---------------------------------------------------------------------------

SENSORS = {
    # Output
    "output_voltage": {
        "name": "Output voltage",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
    },
    "output_frequency": {
        "name": "Output frequency",
        "unit

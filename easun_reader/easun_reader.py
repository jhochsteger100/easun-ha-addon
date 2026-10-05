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
    require_count("HGRID", f, 6)

    return {
        "grid_voltage": float(f[0]),
        "grid_frequency": float(f[1]),
        "grid_high_voltage_limit": int(f[2]),
        "grid_low_voltage_limit": int(f[3]),
        "grid_high_frequency_limit": int(f[4]),
        "grid_low_frequency_limit": int(f[5]),
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

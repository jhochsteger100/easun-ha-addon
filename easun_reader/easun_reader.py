#!/usr/bin/env python3

import argparse
import json
import os
import sys
import time
import queue
from datetime import datetime, timezone

import serial

# Warteschlange fuer eingehende MQTT-Befehle
COMMAND_QUEUE = queue.Queue()

# ---------------------------------------------------------------------------
# Steuerbefehle (Voltronic PI30 Standard)
# ---------------------------------------------------------------------------
# HINWEIS: Hier mappen wir die Home Assistant Auswahl auf die echten
# RS232-Befehle. EASun/Voltronic nutzt standardmäßig POPxx und PCPxx.
# Falls Ihr spezifisches Modell abweicht, können die Werte hier angepasst werden.
COMMAND_MAP = {
    "output_priority": {
        "SUF": "POP00",  # Utility First / SUF
        "Solar": "POP01", # Solar First
        "SUB": "POP02",  # SBU / SUB
    },
    "charger_priority": {
        "Utility": "PCP00", # Utility First
        "Solar": "PCP01",   # Solar First
        "SNU": "PCP02",     # Solar and Utility
        "OSO": "PCP03",     # Only Solar
    }
}

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
        "output_status_code": f[8],
    }

def parse_hgrid(response):
    f = fields(response)
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
        "battery_charging_current_tentative": int(f[3]),
        "battery_discharging_current_tentative": int(f[4]),
        "battery_dc_bus_voltage": int(f[5]),

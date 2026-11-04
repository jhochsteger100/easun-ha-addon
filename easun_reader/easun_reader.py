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
        "unit": "Hz",
        "device_class": "frequency",
        "state_class": "measurement",
    },
    "output_apparent_power": {
        "name": "Output apparent power",
        "unit": "VA",
        "device_class": "apparent_power",
        "state_class": "measurement",
    },
    "output_active_power": {
        "name": "Output active power",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
    },
    "output_load": {
        "name": "Output load",
        "unit": "%",
        "state_class": "measurement",
    },
    "output_current": {
        "name": "Output current",
        "unit": "A",
        "device_class": "current",
        "state_class": "measurement",
    },
    "dc_bus_voltage": {
        "name": "DC bus voltage",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
    },
    "rated_power": {
        "name": "Rated power",
        "unit": "VA",
        "entity_category": "diagnostic",
    },

    # Grid
    "grid_voltage": {
        "name": "Grid voltage",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
    },
    "grid_frequency": {
        "name": "Grid frequency",
        "unit": "Hz",
        "device_class": "frequency",
        "state_class": "measurement",
    },
    "grid_active_power": {
        "name": "Netzleistung (Bezug/Einspeisung)",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
    },
    "grid_high_voltage_limit": {
        "name": "Grid High Voltage Limit",
        "unit": "V",
        "device_class": "voltage",
        "entity_category": "diagnostic",
    },
    "grid_low_voltage_limit": {
        "name": "Grid Low Voltage Limit",
        "unit": "V",
        "device_class": "voltage",
        "entity_category": "diagnostic",
    },
    "grid_high_frequency_limit": {
        "name": "Grid High Frequency Limit",
        "unit": "Hz",
        "device_class": "frequency",
        "entity_category": "diagnostic",
    },
    "grid_low_frequency_limit": {
        "name": "Grid Low Frequency Limit",
        "unit": "Hz",
        "device_class": "frequency",
        "entity_category": "diagnostic",
    },

    # Battery
    "battery_voltage": {
        "name": "Battery voltage",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
    },
    "battery_capacity": {
        "name": "Battery capacity",
        "unit": "%",
        "device_class": "battery",
        "state_class": "measurement",
    },

    # PV
    "pv_voltage": {
        "name": "PV voltage",
        "unit": "V",
        "device_class": "voltage",
        "state_class": "measurement",
    },
    "pv_current": {
        "name": "PV current",
        "unit": "A",
        "device_class": "current",
        "state_class": "measurement",
    },
    "pv_power": {
        "name": "PV power",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
    },
    "pv_max_voltage": {
        "name": "PV max voltage",
        "unit": "V",
        "device_class": "voltage",
        "entity_category": "diagnostic",
    },
    "pv_max_power": {
        "name": "PV max power",
        "unit": "W",
        "device_class": "power",
        "entity_category": "diagnostic",
    },

    # Temperatures
    "pv_temperature": {
        "name": "PV temperature",
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
    },
    "inverter_temperature": {
        "name": "Inverter temperature",
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
    },
    "boost_temperature": {
        "name": "Boost temperature",
        "unit": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
    },

    # Generation
    "pv_energy_today": {
        "name": "PV energy today",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "total",
    },
    "pv_energy_month": {
        "name": "PV energy this month",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "total",
    },
    "pv_energy_year": {
        "name": "PV energy this year",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "total",
    },
    "pv_energy_total": {
        "name": "PV energy total",
        "unit": "kWh",
        "device_class": "energy",
        "state_class": "total_increasing",
    },

    # Extra Sensors
    "fan_1_speed_raw": {
        "name": "Luefter 1 Geschwindigkeit",
        "entity_category": "diagnostic",
        "state_class": "measurement",
    },
    "fan_2_speed_raw": {
        "name": "Luefter 2 Geschwindigkeit",
        "entity_category": "diagnostic",
        "state_class": "measurement",
    },
    "battery_charging_current_tentative": {
        "name": "Batterie Ladestrom (Details)",
        "unit": "A",
        "device_class": "current",
        "state_class": "measurement",
    },
    "battery_discharging_current_tentative": {
        "name": "Batterie Entladestrom (Details)",
        "unit": "A",
        "device_class": "current",
        "state_class": "measurement",
    },
    "pv_operating_mode_tentative": {
        "name": "PV Betriebsmodus",
        "entity_category": "diagnostic",
    },

    # Diagnostics
    "output_status_code": {
        "name": "Output status code",
        "entity_category": "diagnostic",
    },
    "software_version": {
        "name": "Software version",
        "entity_category": "diagnostic",
    },
    "software_date": {
        "name": "Software date",
        "entity_category": "diagnostic",
    },
    "firmware_variant": {
        "name": "Firmware variant",
        "entity_category": "diagnostic",
    },
}


def create_mqtt_client(args):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print(
            "MQTT mode requires paho-mqtt:\n"
            "  python3 -m pip install paho-mqtt",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=args.device_id,
        )
    except (AttributeError, TypeError):
        client = mqtt.Client(client_id=args.device_id)

    if args.mqtt_user:
        client.username_pw_set(
            args.mqtt_user,
            args.mqtt_password,
        )

    if args.mqtt_tls:
        client.tls_set()

    availability_topic = f"{args.mqtt_topic}/availability"

    client.will_set(
        availability_topic,
        payload="offline",
        qos=1,
        retain=True,
    )

    client.connect(
        args.mqtt_host,
        args.mqtt_port,
        keepalive=60,
    )

    client.loop_start()

    client.publish(
        availability_topic,
        "online",
        qos=1,
        retain=True,
    )

    return client


def publish_discovery(client, args):
    state_topic = f"{args.mqtt_topic}/state"
    availability_topic = f"{args.mqtt_topic}/availability"

    device = {
        "identifiers": [args.device_id],
        "name": args.device_name,
        "manufacturer": "EASUN Power",
        "model": "Hybrid Solar Inverter / SMT-III protocol",
    }

    for key, sensor in SENSORS.items():
        config = {
            "name": sensor["name"],
            "unique_id": f"{args.device_id}_{key}",
            "state_topic": state_topic,
            "value_template": "{{ value_json." + key + " }}",
            "availability_topic": availability_topic,
            "device": device,
        }

        if "unit" in sensor:
            config["unit_of_measurement"] = sensor["unit"]

        if "device_class" in sensor:
            config["device_class"] = sensor["device_class"]

        if "state_class" in sensor:
            config["state_class"] = sensor["state_class"]

        if "entity_category" in sensor:
            config["entity_category"] = sensor["entity_category"]

        discovery_topic = (
            f"{args.discovery_prefix}/sensor/"
            f"{args.device_id}/{key}/config"
        )

        client.publish(
            discovery_topic,
            json.dumps(config),
            qos=1,
            retain=True,
        )


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Read EASUN SMT-III inverter over RS232"
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--stdout",
        action="store_true",
        help="Print readings to stdout",
    )
    mode.add_argument(
        "--mqtt",
        action="store_true",
        help="Publish readings via MQTT",
    )

    parser.add_argument(
        "--port",
        default="/dev/ttyUSB0",
        help="Serial port (default: /dev/ttyUSB0)",
    )

    parser.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Polling interval in seconds (default: 10)",
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Read once and exit",
    )

    parser.add_argument("--mqtt-host")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-user")
    parser.add_argument(
        "--mqtt-password",
        default=os.environ.get("MQTT_PASSWORD"),
    )
    parser.add_argument("--mqtt-tls", action="store_true")

    parser.add_argument(
        "--mqtt-topic",
        default="easun/inverter",
    )

    parser.add_argument(
        "--discovery-prefix",
        default="homeassistant",
    )

    parser.add_argument(
        "--device-id",
        default="easun_inverter",
    )

    parser.add_argument(
        "--device-name",
        default="EASUN Inverter",
    )

    args = parser.parse_args()

    if args.mqtt and not args.mqtt_host:
        parser.error("--mqtt requires --mqtt-host")

    return args


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    mqtt_client = None

    if args.mqtt:
        mqtt_client = create_mqtt_client(args)
        publish_discovery(mqtt_client, args)

    state = {}

    try:
        with serial.Serial(
            args.port,
            baudrate=2400,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=2,
        ) as ser:

            time.sleep(0.2)
            ser.reset_input_buffer()

            while True:
                data, successful = poll_inverter(ser)

                for key, value in data.items():
                    if key not in ("_raw", "_errors"):
                        state[key] = value

                state["_raw"] = data.get("_raw", {})

                if "_errors" in data:
                    state["_errors"] = data["_errors"]
                else:
                    state.pop("_errors", None)

                if args.stdout:
                    print(
                        json.dumps(
                            state,
                            indent=2,
                            sort_keys=True,
                        ),
                        flush=True,
                    )

                if mqtt_client:
                    if successful:
                        mqtt_client.publish(
                            f"{args.mqtt_topic}/availability",
                            "online",
                            qos=1,
                            retain=True,
                        )

                        mqtt_client.publish(
                            f"{args.mqtt_topic}/state",
                            json.dumps(state),
                            qos=1,
                            retain=True,
                        )
                    else:
                        mqtt_client.publish(
                            f"{args.mqtt_topic}/availability",
                            "offline",
                            qos=1,
                            retain=True,
                        )

                if args.once:
                    break

                time.sleep(args.interval)

    except KeyboardInterrupt:
        pass

    finally:
        if mqtt_client:
            mqtt_client.publish(
                f"{args.mqtt_topic}/availability",
                "offline",
                qos=1,
                retain=True,
            )

            time.sleep(0.2)
            mqtt_client.loop_stop()
            mqtt_client.disconnect()


if __name__ == "__main__":
    main()

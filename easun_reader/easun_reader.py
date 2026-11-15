#!/usr/init/env python3
#!/usr/bin/env python3

import argparse
import json
import os
import sys
import time
import queue
from datetime import datetime, timezone

import serial

COMMAND_QUEUE = queue.Queue()

# ---------------------------------------------------------------------------
# Steuerbefehle (Nur funktionierende PI30 Befehle für Menüs 1, 2, 3, 8, 11, 16 + Spannungen)
# ---------------------------------------------------------------------------
COMMAND_MAP = {
    "output_priority": {
        "SUB": "POP00",
        "SBU": "POP01",
        "SUF": "POP02",
        "PEC": "POP03",
    },
    "charger_priority": {
        "CSO": "PCP00",
        "SNU": "PCP01",
        "0S0": "PCP02",
        "Sot": "PCP03",
    },
    "ac_input_range": {
        "Appliance (APL)": "PGR00",
        "UPS": "PGR01",
    },
    "max_utility_charge_current": {
        "2A": "MUCHGC002",
        "10A": "MUCHGC010",
        "20A": "MUCHGC020",
        "30A": "MUCHGC030",
    },
    "max_total_charge_current": {
        "10A": "MCHGC010",
        "20A": "MCHGC020",
        "30A": "MCHGC030",
        "40A": "MCHGC040",
        "50A": "MCHGC050",
        "60A": "MCHGC060",
    },
    "eco_mode": {
        "Enable": "PEa",
        "Disable": "PEd",
    },
    # Spannungs- und Schwellenwerte (Menüs 12, 13, 17, 24, 26, 27, 29)
    "back_to_grid_voltage": lambda val: f"V{int(float(val)*10):04d}",
    "back_to_battery_voltage": lambda val: f"FUL{int(float(val)*10):03d}",
    "battery_reboot_voltage": lambda val: f"PBD{int(float(val)*10):03d}",
    "battery_low_voltage": lambda val: f"PBC{int(float(val)*10):03d}",
    "bulk_charging_voltage": lambda val: f"BVC{int(float(val)*10):03d}",
    "floating_charging_voltage": lambda val: f"BVF{int(float(val)*10):03d}",
    "low_dc_cutoff_voltage": lambda val: f"CUT{int(float(val)*10):03d}",
}

# ---------------------------------------------------------------------------
# Protocol parsing
# ---------------------------------------------------------------------------

def fields(response: str):
    response = response.strip()
    if not response.startswith("("):
        raise ValueError(f"Unexpected response: {response!r}")
    return response[1:].split()

def require_count(command, f, count):
    if len(f) < count:
        raise ValueError(f"{command}: expected at least {count} fields, got {len(f)}: {f}")

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
        "pv_operating_mode_tentative": int(f[5]),
        "pv_max_voltage": float(f[6]),
        "pv_max_power": int(f[8]),
    }

def parse_htemp(response):
    f = fields(response)
    require_count("HTEMP", f, 8)
    return {
        "pv_temperature": int(f[0]),
        "inverter_temperature": int(f[1]),
        "boost_temperature": int(f[2]),
        "fan_state_tentative": int(f[3]),
        "secondary_temperature_tentative": int(f[4]),
        "fan_1_speed_raw": int(f[5]),
        "fan_2_speed_raw": int(f[6]),
        "temperature_flags": f[7],
    }

def parse_hgen(response):
    f = fields(response)
    require_count("HGEN", f, 7)
    return {
        "inverter_date": f[0],
        "inverter_time": f[1],
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
# Serial communication & CRC
# ---------------------------------------------------------------------------

def crc_pi30(command):
    crc = 0
    for char in command:
        crc = (crc ^ (ord(char) << 8)) & 0xFFFF
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    
    crc_high = (crc >> 8) & 0xFF
    crc_low = crc & 0xFF
    
    if crc_high in (0x28, 0x0D, 0x0A): crc_high += 1
    if crc_low in (0x28, 0x0D, 0x0A): crc_low += 1
        
    return bytes([crc_high, crc_low])

def query(ser, command):
    ser.reset_input_buffer()
    
    if command.startswith(("POP", "PCP", "PGR", "MUCH", "MCHG", "PE", "V", "FUL", "PBD", "PBC", "BVC", "BVF", "CUT")):
        crc_bytes = crc_pi30(command)
        request = command.encode("ascii") + crc_bytes + b"\r"
    else:
        request = (command + "\r").encode("ascii")
    
    ser.write(request)
    ser.flush()

    response = ser.read_until(b"\r")
    if not response:
        raise TimeoutError(f"{command}: no response")

    try:
        return response.decode("ascii", errors="ignore")
    except Exception as e:
        raise ValueError(f"{command}: decode error {e}")

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
        
        time.sleep(0.10)

    result["_raw"] = raw
    if errors:
        result["_errors"] = errors
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    return result, successful

# ---------------------------------------------------------------------------
# MQTT Callbacks
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, rc, properties=None):
    topic = f"{userdata['mqtt_topic']}/set/#"
    client.subscribe(topic)
    print(f"Subscribed to commands on {topic}")

def on_message(client, userdata, msg):
    payload = msg.payload.decode("utf-8").strip()
    COMMAND_QUEUE.put((msg.topic, payload))
    print(f"Received command: {msg.topic} -> {payload}")

# ---------------------------------------------------------------------------
# SENSORS, SELECTS & NUMBERS (Bereinigt von "unknown" Entitäten)
# ---------------------------------------------------------------------------
SENSORS = {
    "output_voltage": {"name": "Output voltage", "unit": "V", "device_class": "voltage", "state_class": "measurement"},
    "output_frequency": {"name": "Output frequency", "unit": "Hz", "device_class": "frequency", "state_class": "measurement"},
    "output_apparent_power": {"name": "Output apparent power", "unit": "VA", "device_class": "apparent_power", "state_class": "measurement"},
    "output_active_power": {"name": "Output active power", "unit": "W", "device_class": "power", "state_class": "measurement"},
    "output_load": {"name": "Output load", "unit": "%", "state_class": "measurement"},
    "output_current": {"name": "Output current", "unit": "A", "device_class": "current", "state_class": "measurement"},
    "dc_bus_voltage": {"name": "DC bus voltage", "unit": "V", "device_class": "voltage", "state_class": "measurement"},
    "rated_power": {"name": "Rated power", "unit": "VA", "entity_category": "diagnostic"},
    
    "grid_voltage": {"name": "Grid voltage", "unit": "V", "device_class": "voltage", "state_class": "measurement"},
    "grid_frequency": {"name": "Grid frequency", "unit": "Hz", "device_class": "frequency", "state_class": "measurement"},
    "grid_active_power": {"name": "Netzleistung (Bezug/Einspeisung)", "unit": "W", "device_class": "power", "state_class": "measurement"},
    
    "battery_voltage": {"name": "Battery voltage", "unit": "V", "device_class": "voltage", "state_class": "measurement"},
    "battery_capacity": {"name": "Battery capacity", "unit": "%", "device_class": "battery", "state_class": "measurement"},
    
    "pv_voltage": {"name": "PV voltage", "unit": "V", "device_class": "voltage", "state_class": "measurement"},
    "pv_current": {"name": "PV current", "unit": "A", "device_class": "current", "state_class": "measurement"},
    "pv_power": {"name": "PV power", "unit": "W", "device_class": "power", "state_class": "measurement"},
    
    "pv_temperature": {"name": "PV temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement"},
    "inverter_temperature": {"name": "Inverter temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement"},
    "boost_temperature": {"name": "Boost temperature", "unit": "°C", "device_class": "temperature", "state_class": "measurement"},
    
    "pv_energy_today": {"name": "PV energy today", "unit": "kWh", "device_class": "energy", "state_class": "total"},
    "pv_energy_total": {"name": "PV energy total", "unit": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    
    "output_status_code": {"name": "Output status code", "entity_category": "diagnostic"},
    "software_version": {"name": "Software version", "entity_category": "diagnostic"},
}

SELECTS = {
    "output_priority": {"name": "Menü 01: Ausgangspriorität", "options": ["SUB", "SBU", "SUF", "PEC"], "icon": "mdi:power-flow"},
    "charger_priority": {"name": "Menü 16: Ladequelle", "options": ["CSO", "SNU", "0S0", "Sot"], "icon": "mdi:battery-charging"},
    "ac_input_range": {"name": "Menü 03: AC Eingang", "options": ["Appliance (APL)", "UPS"], "icon": "mdi:current-ac"},
    "max_utility_charge_current": {"name": "Menü 11: Netz-Ladestrom", "options": ["2A", "10A", "20A", "30A"], "icon": "mdi:current-dc"},
    "max_total_charge_current": {"name": "Menü 02: Gesamt-Ladestrom", "options": ["10A", "20A", "30A", "40A", "50A", "60A"], "icon": "mdi:battery-charging-high"},
    "eco_mode": {"name": "Menü 08: ECO Modus", "options": ["Enable", "Disable"], "icon": "mdi:leaf"},
}

# Numbers mit "mode": "box" für reines Eingabefeld statt Slider
NUMBERS = {
    "back_to_grid_voltage": {"name": "Menü 12: SBU Rückkehr zum Netz", "min": 22.0, "max": 25.5, "step": 0.5, "unit": "V", "icon": "mdi:flash-alert"},
    "back_to_battery_voltage": {"name": "Menü 13: SBU Rückkehr zur Batterie", "min": 24.0, "max": 29.0, "step": 0.5, "unit": "V", "icon": "mdi:battery-arrow-up"},
    "battery_reboot_voltage": {"name": "Menü 17: Batterie Neustart-Spannung", "min": 20.0, "max": 27.0, "step": 0.1, "unit": "V", "icon": "mdi:restart"},
    "battery_low_voltage": {"name": "Menü 24: Batterie Low Voltage Alarm", "min": 20.0, "max": 27.0, "step": 0.1, "unit": "V", "icon": "mdi:battery-alert"},
    "bulk_charging_voltage": {"name": "Menü 26: Bulk Ladespannung (CV)", "min": 24.0, "max": 30.0, "step": 0.1, "unit": "V", "icon": "mdi:battery-charging-100"},
    "floating_charging_voltage": {"name": "Menü 27: Floating Ladespannung", "min": 24.0, "max": 30.0, "step": 0.1, "unit": "V", "icon": "mdi:battery-charging-outline"},
    "low_dc_cutoff_voltage": {"name": "Menü 29: Low DC Abschaltspannung", "min": 20.0, "max": 26.0, "step": 0.1, "unit": "V", "icon": "mdi:power-plug-off"},
}

def create_mqtt_client(args):
    import paho.mqtt.client as mqtt
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=args.device_id)
    except (AttributeError, TypeError):
        client = mqtt.Client(client_id=args.device_id)

    if args.mqtt_user:
        client.username_pw_set(args.mqtt_user, args.mqtt_password)
    
    client.user_data_set({"mqtt_topic": args.mqtt_topic})
    client.on_connect = on_connect
    client.on_message = on_message

    availability_topic = f"{args.mqtt_topic}/availability"
    client.will_set(availability_topic, payload="offline", qos=1, retain=True)
    client.connect(args.mqtt_host, args.mqtt_port, keepalive=60)
    client.loop_start()
    client.publish(availability_topic, "online", qos=1, retain=True)
    return client

def publish_discovery(client, args):
    state_topic = f"{args.mqtt_topic}/state"
    availability_topic = f"{args.mqtt_topic}/availability"

    device = {
        "identifiers": [args.device_id],
        "name": args.device_name,
        "manufacturer": "EASun",
        "model": "Hybrid Inverter SMT-III",
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
        for attr in ["unit_of_measurement", "device_class", "state_class", "entity_category"]:
            if attr.replace("_of_measurement", "") in sensor:
                config[attr] = sensor[attr.replace("_of_measurement", "")]

        client.publish(f"{args.discovery_prefix}/sensor/{args.device_id}/{key}/config", json.dumps(config), qos=1, retain=True)

    for key, select in SELECTS.items():
        config = {
            "name": select["name"],
            "unique_id": f"{args.device_id}_{key}",
            "state_topic": state_topic,
            "command_topic": f"{args.mqtt_topic}/set/{key}",
            "value_template": "{{ value_json." + key + " }}",
            "options": select["options"],
            "icon": select["icon"],
            "availability_topic": availability_topic,
            "device": device,
        }
        client.publish(f"{args.discovery_prefix}/select/{args.device_id}/{key}/config", json.dumps(config), qos=1, retain=True)

    # Hier wird "mode": "box" gesetzt, damit Home Assistant ein Eingabefeld statt eines Sliders anzeigt
    for key, num in NUMBERS.items():
        config = {
            "name": num["name"],
            "unique_id": f"{args.device_id}_{key}",
            "state_topic": state_topic,
            "command_topic": f"{args.mqtt_topic}/set/{key}",
            "value_template": "{{ value_json." + key + " }}",
            "min": num["min"],
            "max": num["max"],
            "step": num["step"],
            "mode": "box",
            "unit_of_measurement": num["unit"],
            "icon": num["icon"],
            "availability_topic": availability_topic,
            "device": device,
        }
        client.publish(f"{args.discovery_prefix}/number/{args.device_id}/{key}/config", json.dumps(config), qos=1, retain=True)

def parse_args():
    parser = argparse.ArgumentParser(description="Read/Write EASUN SMT-III inverter over RS232")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stdout", action="store_true", help="Print readings to stdout")
    mode.add_argument("--mqtt", action="store_true", help="Publish readings via MQTT")
    parser.add_argument("--port", default="/dev/ttyUSB0", help="Serial port")
    parser.add_argument("--interval", type=float, default=10.0, help="Polling interval")
    parser.add_argument("--once", action="store_true", help="Read once and exit")
    parser.add_argument("--mqtt-host")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-user")
    parser.add_argument("--mqtt-password", default=os.environ.get("MQTT_PASSWORD"))
    parser.add_argument("--mqtt-topic", default="easun/inverter")
    parser.add_argument("--discovery-prefix", default="homeassistant")
    parser.add_argument("--device-id", default="easun_inverter")
    parser.add_argument("--device-name", default="EASUN Inverter")
    return parser.parse_args()

def main():
    args = parse_args()
    mqtt_client = create_mqtt_client(args) if args.mqtt else None
    if mqtt_client:
        publish_discovery(mqtt_client, args)

    state = {
        "output_priority": "SUB",
        "charger_priority": "SNU",
        "ac_input_range": "Appliance (APL)",
        "max_utility_charge_current": "20A",
        "max_total_charge_current": "50A",
        "eco_mode": "Disable",
        "back_to_grid_voltage": 23.0,
        "back_to_battery_voltage": 27.0,
        "battery_reboot_voltage": 23.5,
        "battery_low_voltage": 21.0,
        "bulk_charging_voltage": 28.2,
        "floating_charging_voltage": 27.0,
        "low_dc_cutoff_voltage": 21.0,
    }

    try:
        with serial.Serial(args.port, baudrate=2400, bytesize=8, parity=serial.PARITY_NONE, stopbits=1, timeout=2) as ser:
            time.sleep(0.2)
            ser.reset_input_buffer()

            while True:
                while not COMMAND_QUEUE.empty():
                    topic, payload = COMMAND_QUEUE.get()
                    command_key = topic.split("/")[-1]
                    
                    rs232_cmd = None
                    if command_key in COMMAND_MAP:
                        mapping = COMMAND_MAP[command_key]
                        if callable(mapping):
                            try:
                                rs232_cmd = mapping(payload)
                            except Exception as ex:
                                print(f"Fehler beim Generieren des Befehls für {command_key}: {ex}")
                        elif payload in mapping:
                            rs232_cmd = mapping[payload]

                    if rs232_cmd:
                        try:
                            print(f"Sende Befehl an Inverter: {rs232_cmd}")
                            response = query(ser, rs232_cmd)
                            print(f"Antwort: {response}")
                            if "ACK" in response:
                                if command_key in NUMBERS:
                                    state[command_key] = float(payload)
                                else:
                                    state[command_key] = payload
                        except Exception as e:
                            print(f"Fehler beim Senden von {rs232_cmd}: {e}")
                    else:
                        print(f"Unbekannter Befehl: {command_key} -> {payload}")
                    
                    time.sleep(0.5)

                data, successful = poll_inverter(ser)
                for key, value in data.items():
                    if key not in ("_raw", "_errors"):
                        state[key] = value

                state["_raw"] = data.get("_raw", {})
                if "_errors" in data:
                    state["_errors"] = data["_errors"]
                else:
                    state.pop("_errors", None)

                if mqtt_client:
                    if successful:
                        mqtt_client.publish(f"{args.mqtt_topic}/availability", "online", qos=1, retain=True)
                        mqtt_client.publish(f"{args.mqtt_topic}/state", json.dumps(state), qos=1, retain=True)
                    else:
                        mqtt_client.publish(f"{args.mqtt_topic}/availability", "offline", qos=1, retain=True)

                if args.once:
                    break

                time.sleep(args.interval)

    except KeyboardInterrupt:
        pass
    finally:
        if mqtt_client:
            mqtt_client.publish(f"{args.mqtt_topic}/availability", "offline", qos=1, retain=True)
            time.sleep(0.2)
            mqtt_client.loop_stop()
            mqtt_client.disconnect()

if __name__ == "__main__":
    main()

#!/bin/sh
echo "Starte EASun Python Reader..."
exec python3 -u /easun_reader.py \
  --mqtt \
  --port /dev/ttyUSB0 \
  --mqtt-host core-mosquitto \
  --mqtt-user "Easun" \
  --mqtt-password "Easun"

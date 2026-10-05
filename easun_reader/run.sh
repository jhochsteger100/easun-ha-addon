#!/usr/bin/env bash
echo "Starte EASun Python Reader..."
python3 /easun_reader.py \
  --mqtt \
  --port /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AQ02YZQL-if00-port0 \
  --mqtt-host core-mosquitto \
  --mqtt-user "Easun" \
  --mqtt-password "Easun"

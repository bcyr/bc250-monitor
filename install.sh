#!/bin/sh
# Installs bc250-monitor to /opt/bc250-monitor and enables the systemd service.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo ./install.sh" >&2; exit 1; }
cd "$(dirname "$0")"

install -d /opt/bc250-monitor
install -m 0755 monitor.py /opt/bc250-monitor/monitor.py
install -m 0644 dashboard.html /opt/bc250-monitor/dashboard.html
install -m 0644 bc250-monitor.service /etc/systemd/system/bc250-monitor.service

systemctl daemon-reload
systemctl enable --now bc250-monitor
systemctl restart bc250-monitor
echo "bc250-monitor installed — http://$(hostname -I | cut -d' ' -f1):${BC250_MON_PORT:-9095}"

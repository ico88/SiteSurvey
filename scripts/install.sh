#!/usr/bin/env bash
# Installazione su Raspberry Pi OS (Bookworm/Bullseye). Eseguire come root dalla cartella del progetto.
set -euo pipefail
cd "$(dirname "$0")/.."

[ "$(id -u)" -eq 0 ] || { echo "eseguire con sudo"; exit 1; }

apt-get update
apt-get install -y python3-venv python3-dev i2c-tools libjpeg-dev zlib1g-dev libfreetype6-dev \
    libusb-1.0-0   # libusb: controllo Hytera via USB (pyusb)

# I2C per il display
if command -v raspi-config >/dev/null; then raspi-config nonint do_i2c 0 || true; fi

id sitesurvey >/dev/null 2>&1 || useradd --system --home /var/lib/sitesurvey --shell /usr/sbin/nologin sitesurvey
for g in gpio i2c dialout plugdev; do getent group "$g" >/dev/null && usermod -aG "$g" sitesurvey; done

# regola udev: accesso non-root al device di controllo Hytera (USB VID 238b)
install -m 0644 scripts/99-hytera.rules /etc/udev/rules.d/99-hytera.rules 2>/dev/null || true
udevadm control --reload-rules 2>/dev/null || true

python3 -m venv /opt/sitesurvey/venv
/opt/sitesurvey/venv/bin/pip install --upgrade pip
/opt/sitesurvey/venv/bin/pip install ".[pi]"

install -d -m 0755 /etc/sitesurvey
[ -f /etc/sitesurvey/config.toml ] || install -m 0644 config/config.example.toml /etc/sitesurvey/config.toml
install -d -o sitesurvey -g sitesurvey /var/lib/sitesurvey

# l'interfaccia USB della radio non deve diventare la rotta di default
install -m 0644 scripts/90-radio-usb.conf /etc/NetworkManager/conf.d/90-radio-usb.conf 2>/dev/null || true

install -m 0644 systemd/sitesurvey.service /etc/systemd/system/sitesurvey.service
ln -sf /opt/sitesurvey/venv/bin/sitesurvey /usr/local/bin/sitesurvey
systemctl daemon-reload
systemctl enable --now sitesurvey.service
echo
echo "Installato. Modificare /etc/sitesurvey/config.toml e poi: sudo systemctl restart sitesurvey"
echo "Log: journalctl -u sitesurvey -f      Stato: sudo sitesurvey ctl status"

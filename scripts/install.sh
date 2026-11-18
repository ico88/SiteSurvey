#!/usr/bin/env bash
# Installazione su Raspberry Pi OS (Bookworm/Bullseye). Eseguire come root dalla cartella del progetto.
set -euo pipefail
cd "$(dirname "$0")/.."

[ "$(id -u)" -eq 0 ] || { echo "eseguire con sudo"; exit 1; }

apt-get update
# Pacchetti precompilati da apt per le dipendenze "pesanti" (niente compilazione
# da sorgente): su Raspberry Pi 2 compilare numpy richiederebbe decine di minuti
# e potrebbe esaurire la RAM. Il venv userà questi via --system-site-packages.
apt-get install -y python3-venv python3-dev i2c-tools libjpeg-dev zlib1g-dev libfreetype6-dev \
    libusb-1.0-0 \
    python3-numpy python3-pil python3-serial python3-evdev python3-usb || true
# python3-lgpio solo dove disponibile (Bookworm/trixie)
apt-get install -y python3-lgpio 2>/dev/null || true

# SPI per il TFT 3.5" (framebuffer) e I2C per gli OLED
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_spi 0 || true
  raspi-config nonint do_i2c 0 || true
fi

id sitesurvey >/dev/null 2>&1 || useradd --system --home /var/lib/sitesurvey --shell /usr/sbin/nologin sitesurvey
# input = touch del TFT; plugdev = USB Hytera; gpio/i2c/dialout = display/GPS
for g in gpio i2c spi dialout plugdev input video; do getent group "$g" >/dev/null && usermod -aG "$g" sitesurvey; done

# regola udev: accesso non-root al device di controllo Hytera (USB VID 238b)
install -m 0644 scripts/99-hytera.rules /etc/udev/rules.d/99-hytera.rules 2>/dev/null || true
udevadm control --reload-rules 2>/dev/null || true

# venv con accesso ai pacchetti di sistema (numpy/Pillow/evdev/pyserial da apt):
# pip non li ricompila, installa solo cio' che manca (luma.oled, gpiozero, pyusb).
python3 -m venv --system-site-packages /opt/sitesurvey/venv
/opt/sitesurvey/venv/bin/pip install --upgrade pip
# --prefer-binary: usa i wheel quando ci sono, evita build da sorgente inutili.
/opt/sitesurvey/venv/bin/pip install --prefer-binary ".[pi]"

install -d -m 0755 /etc/sitesurvey
[ -f /etc/sitesurvey/config.toml ] || install -m 0644 config/config.example.toml /etc/sitesurvey/config.toml
# file dei segreti (chiavi radio): separato, permessi stretti, MAI nel repo
if [ ! -f /etc/sitesurvey/secrets.toml ]; then
  install -m 0600 -o sitesurvey -g sitesurvey config/secrets.example.toml /etc/sitesurvey/secrets.toml
  echo "Creato /etc/sitesurvey/secrets.toml (metti qui xnl_key)."
fi
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

#!/bin/bash
# Installiert bzw. aktualisiert den FX-Server. Läuft als root, beliebig oft wiederholbar.
#   bash deploy/install.sh install   -> Erstinstallation (schickt am Ende die Test-WhatsApp mit Dashboard-Link)
#   bash deploy/install.sh update    -> nach einem Update aus GitHub
set -uo pipefail
MODUS="${1:-install}"
REPO=/opt/fx-server
ETC=/etc/fx-server
DATA=/var/lib/fx-server
LOGS=/var/log/fx-server
mkdir -p "$ETC" "$DATA/public" "$LOGS"
chmod 700 "$ETC"; chmod 755 "$DATA" "$DATA/public"
exec >>"$LOGS/install.log" 2>&1
echo "===== $(date) install.sh $MODUS ($(git -C $REPO rev-parse --short HEAD 2>/dev/null)) ====="
cd "$REPO" || exit 1
export DEBIAN_FRONTEND=noninteractive

# 1) Systempakete (nur wenn etwas fehlt)
PAKETE="python3-venv python3-pip git caddy sqlite3 curl"
FEHLT=""
for p in $PAKETE; do dpkg -s "$p" >/dev/null 2>&1 || FEHLT="$FEHLT $p"; done
if [ -n "$FEHLT" ]; then
  for i in $(seq 1 20); do
    apt-get -o DPkg::Lock::Timeout=300 update && apt-get -o DPkg::Lock::Timeout=300 install -y $FEHLT && break
    echo "apt fehlgeschlagen (Versuch $i)"; sleep 15
  done
fi

# 2) Python-Umgebung (pip nur, wenn sich requirements.txt geändert hat)
[ -x venv/bin/python ] || python3 -m venv venv
HASH=$(sha256sum requirements.txt | cut -c1-16)
if [ "$(cat $DATA/.req_hash 2>/dev/null)" != "$HASH" ]; then
  for i in $(seq 1 5); do
    venv/bin/pip install -q --upgrade pip && venv/bin/pip install -q -r requirements.txt && echo "$HASH" > $DATA/.req_hash && break
    echo "pip fehlgeschlagen (Versuch $i)"; sleep 10
  done
fi

# 3) Geheimer Dashboard-Pfad + Adresse (Server-IP als kostenlose sslip.io-Adresse mit HTTPS)
if [ ! -s "$ETC/secret" ]; then
  tr -dc 'a-zA-Z0-9' </dev/urandom | head -c 28 > "$ETC/secret"
fi
SECRET=$(cat "$ETC/secret")
IP=$(curl -s --max-time 5 http://169.254.169.254/hetzner/v1/metadata/public-ipv4 || true)
[[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || IP=$(curl -s --max-time 10 https://api.ipify.org || true)
if [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  HOST="${IP//./-}.sslip.io"
  echo "$HOST" > "$ETC/host"
fi
HOST=$(cat "$ETC/host" 2>/dev/null || echo "")
touch "$ETC/env"; chmod 600 "$ETC/env"
if [ -n "$HOST" ]; then
  grep -v '^DASHBOARD_URL=' "$ETC/env" > "$ETC/env.neu" || true
  echo "DASHBOARD_URL=https://$HOST/$SECRET/" >> "$ETC/env.neu"
  mv "$ETC/env.neu" "$ETC/env"; chmod 600 "$ETC/env"

  # 4) Webserver
  sed -e "s|__HOST__|$HOST|g" -e "s|__SECRET__|$SECRET|g" deploy/Caddyfile.tmpl > /etc/caddy/Caddyfile
  systemctl enable caddy >/dev/null 2>&1
  systemctl reload caddy 2>/dev/null || systemctl restart caddy
fi

# 5) Dienste und Zeitpläne
OLD_ALERT=0
if systemctl list-unit-files gbpjpy-alert.service >/dev/null 2>&1 && systemctl is-enabled gbpjpy-alert >/dev/null 2>&1; then
  systemctl disable --now gbpjpy-alert; OLD_ALERT=1   # alte Einzelinstallation ablösen
fi
cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable fx-alert.service >/dev/null 2>&1
for t in fx-report fx-setup fx-evaluate fx-refresh fx-update fx-backtest; do systemctl enable --now "$t.timer" >/dev/null 2>&1; done
systemctl restart fx-alert.service
chmod 600 "$DATA/fx.db" 2>/dev/null || true

# Neue Strategien (AMD): Backtest einmalig sofort im Hintergrund statt erst Sonntag 12:00
if [ ! -f "$DATA/.backtest_gartley_v1" ]; then
  touch "$DATA/.backtest_amd_v1" "$DATA/.backtest_gartley_v1"
  systemctl start --no-block fx-backtest.service || true
fi

# Trainingsseite (web/training/): Link einmalig per Nachricht schicken
if [ -n "$HOST" ] && [ -f web/training/index.html ] && [ ! -f "$DATA/.training_link_v1" ]; then
  FX_MELDUNG="🏋️ Dein Trainings- und Ernährungsplan (Link geheim halten):
https://$HOST/$SECRET/training/" venv/bin/python -c "import os; from fxserver.common import whatsapp; whatsapp(os.environ['FX_MELDUNG'])" && touch "$DATA/.training_link_v1"
fi

# 6) Erstbefüllung + Test
venv/bin/python -m fxserver.refresh || true
if [ "$MODUS" = "install" ]; then
  sleep 5
  venv/bin/python -m fxserver.cli test || true
fi
echo "===== fertig $(date) ====="

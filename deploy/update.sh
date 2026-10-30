#!/bin/bash
# Holt neue Versionen aus GitHub und spielt sie ein (läuft alle 10 Min per Timer).
# Schlägt die Installation fehl, wird automatisch auf die vorherige Version zurückgesetzt.
set -uo pipefail
REPO=/opt/fx-server
LOGS=/var/log/fx-server
mkdir -p "$LOGS"
exec >>"$LOGS/update.log" 2>&1
cd "$REPO" || exit 0
git fetch -q origin main || { echo "$(date) fetch fehlgeschlagen"; exit 0; }
ALT=$(git rev-parse HEAD)
NEU=$(git rev-parse origin/main)
[ "$ALT" = "$NEU" ] && exit 0

echo "===== $(date) Update ${ALT:0:7} -> ${NEU:0:7} ====="
git reset -q --hard "$NEU"
melden() { FX_MELDUNG="$1" venv/bin/python -c "import os; from fxserver.common import whatsapp; whatsapp(os.environ['FX_MELDUNG'])" || true; }
if bash deploy/install.sh update && venv/bin/python -c "import fxserver.report, fxserver.evaluate, fxserver.refresh, fxserver.setup, fxserver.backtest, fxserver.treiber, fxserver.sources.markt, fxserver.amd, fxserver.tage"; then
  melden "🔄 FX-Server aktualisiert: $(git log -1 --pretty=%s)"
else
  echo "Update fehlgeschlagen – zurück auf ${ALT:0:7}"
  git reset -q --hard "$ALT"
  bash deploy/install.sh update
  melden "⚠️ FX-Server: Update ${NEU:0:7} fehlgeschlagen, alte Version läuft weiter."
fi

#!/usr/bin/env bash
# اجرای ربات با Restart خودکار (لینوکس/مک):   ./run.sh
# همان رفتار run.ps1: کد 0 = توقف، کد 3 = Restart فوری، کرش = Restart با backoff،
# جلوگیری از Restart Loop، و Rollback خودکار نسخه‌ای که بعد از به‌روزرسانی بالا نمی‌آید.
set -u
cd "$(dirname "$0")"
export STARD_SUPERVISED=1 PYTHONUTF8=1
PY="${PYTHON:-python3}"
mkdir -p logs
log() { echo "$(date '+%F %T') $*" | tee -a logs/supervisor.log; }

BASE_DELAY="${STARD_RESTART_DELAY:-5}"
delay=$BASE_DELAY; fast=0; crashes=()
while true; do
  log "starting bot"
  start=$(date +%s)
  "$PY" -m bot; code=$?
  ran=$(( $(date +%s) - start ))
  [ "$code" -eq 0 ] && { log "bot stopped normally"; exit 0; }
  [ "$code" -eq 3 ] && { log "restart requested"; delay=$BASE_DELAY; fast=0; continue; }
  log "bot crashed (exit $code after ${ran}s)"
  if [ "$ran" -lt 60 ]; then fast=$((fast + 1)); else fast=0; delay=$BASE_DELAY; fi
  if [ "$fast" -ge 3 ] && [ -f data/update-pending.json ]; then
    commit=$("$PY" -c "import json;print(json.load(open('data/update-pending.json')).get('from_commit') or '')")
    if [ -n "$commit" ]; then
      log "new version keeps crashing - rolling back to $commit"
      git checkout --force --detach "$commit" && "$PY" -m pip install -q -r requirements.txt
      rm -f data/update-pending.json; fast=0; continue
    fi
  fi
  now=$(date +%s); kept=()
  for c in "${crashes[@]:-}"; do [ -n "$c" ] && [ $((now - c)) -lt 600 ] && kept+=("$c"); done
  crashes=("${kept[@]:-}" "$now")
  if [ "${#crashes[@]}" -gt 5 ]; then
    log "too many crashes in 10 min - waiting 10 minutes"; sleep 600; crashes=()
  else
    log "restarting in ${delay}s"; sleep "$delay"; delay=$(( delay * 2 > 300 ? 300 : delay * 2 ))
  fi
done

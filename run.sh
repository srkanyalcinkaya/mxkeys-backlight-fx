#!/bin/zsh
# MX Keys Işık — arka planda başlatır; menü çubuğunda simge çıkar.
#   ./run.sh            pencereyle başlat        ./run.sh --hidden   sadece menü çubuğu
#   ./run.sh --fg       terminalde, ön planda    ./run.sh --stop     kapat
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
case "$1" in
  --stop)
    pid=$(lsof -nP -t -iTCP:8765 -sTCP:LISTEN 2>/dev/null | head -1)
    [ -z "$pid" ] && { echo "Çalışmıyordu."; exit; }
    curl -sf -m 3 localhost:8765/quit >/dev/null || kill "$pid" 2>/dev/null   # eski sürümde /quit yok
    for i in 1 2 3 4 5 6; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && echo "Kapanmadı (pid $pid)." || echo "MX Keys Işık kapatıldı."
    exit ;;
  --fg)   shift; exec .venv/bin/python "$PWD/app.py" "$@" ;;
esac
nohup .venv/bin/python "$PWD/app.py" "$@" >/dev/null 2>&1 &
disown
echo "MX Keys Işık başlatıldı — menü çubuğundaki klavye simgesine bak. Kapatmak için: simge → Çıkış  (ya da ./run.sh --stop)"

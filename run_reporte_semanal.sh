#!/usr/bin/env bash
# run_reporte_semanal.sh
# -----------------------
# Wrapper pensado para invocarse desde cron 2 veces al día. Carga las
# credenciales, corre wlc_reporte.py contra los WLCs de wlcs.json y deja
# de ejecutarse automáticamente después de FECHA_FIN (para no tener que
# acordarse de quitar el cron cuando termine la semana de muestreo).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Última fecha (YYYY-MM-DD) en la que debe correr. Ajusta al terminar
# la semana de muestreo, o deja vacío ("") para que nunca expire.
FECHA_FIN="2026-09-21"

if [[ -n "$FECHA_FIN" ]]; then
  HOY="$(date +%F)"
  if [[ "$HOY" > "$FECHA_FIN" ]]; then
    echo "[$(date '+%F %T')] FECHA_FIN ($FECHA_FIN) superada; no se ejecuta. Revisa/quita el cron."
    exit 0
  fi
fi

# Credenciales: wlc_credenciales.env exporta WLC1_PASSWORD, WLC2_PASSWORD, etc.
if [[ -f "$DIR/wlc_credenciales.env" ]]; then
  source "$DIR/wlc_credenciales.env"
else
  echo "[$(date '+%F %T')] ERROR: no existe $DIR/wlc_credenciales.env (copia wlc_credenciales.env.example)" >&2
  exit 1
fi

PYTHON="$DIR/.venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

echo "[$(date '+%F %T')] Iniciando reporte WLC..."
"$PYTHON" "$DIR/wlc_reporte.py" --config "$DIR/wlcs.json" --outdir "$DIR/informes"
echo "[$(date '+%F %T')] Reporte WLC finalizado."

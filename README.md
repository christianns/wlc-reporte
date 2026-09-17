# wlc_reporte

Conecta por SSH a uno o varios Cisco WLC (AireOS, probado sobre 8.10.162.0) vía
[Netmiko](https://github.com/ktbyers/netmiko), recolecta métricas de clientes
inalámbricos y genera:

- Un **reporte ejecutivo** en Markdown (+ CSV/Excel) por cada WLC y por corrida.
- Un **histórico acumulativo** (CSV) que va sumando una fila por muestra, pensado
  para correr el script repetidamente (ej. 2 veces al día durante una semana).
- Un **resumen semanal** (`resumen_semanal.md`) que se regenera solo en cada
  corrida a partir de todo el histórico acumulado hasta ese momento, con un
  gráfico de tendencia (usuarios vs. tiempo) embebido directamente en el
  Markdown.

Métricas: total de usuarios conectados, usuarios por AP, usuarios por SSID/WLAN.

## Requisitos

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Opcionales (ver requirements.txt): pandas + openpyxl (Excel), matplotlib (gráfico de tendencia)
```

## Configuración

**Nunca subas credenciales ni la topología real a git** — este repo versiona
solo las plantillas `.example`; los archivos reales quedan ignorados por
[`.gitignore`](.gitignore).

```bash
cp wlcs.json.example wlcs.json
cp wlc_credenciales.env.example wlc_credenciales.env
chmod 600 wlc_credenciales.env
```

Edita `wlcs.json` con tus WLCs reales:

```json
[
  {"nombre": "WLC-Sede-1", "host": "192.168.1.10", "user": "admin", "password_env": "WLC1_PASSWORD"},
  {"nombre": "WLC-Sede-2", "host": "192.168.2.10", "user": "admin", "password_env": "WLC2_PASSWORD"}
]
```

El password **nunca** va en `wlcs.json`: se lee de la variable de entorno que
indiques en `password_env`. Esas variables se definen en `wlc_credenciales.env`:

```bash
export WLC1_PASSWORD='...'
export WLC2_PASSWORD='...'
```

## Uso manual

Un solo WLC:
```bash
python wlc_reporte.py --host 192.168.1.10 --user admin --password secret
```

Varios WLC (usa `wlcs.json`):
```bash
source wlc_credenciales.env
python wlc_reporte.py --config wlcs.json --outdir informes
```

Otras opciones: `--debug` (guarda salidas crudas de los comandos), `--timeout` /
`--read-timeout` (ajustar si el WLC responde lento).

## Automatización (cron)

[`run_reporte_semanal.sh`](run_reporte_semanal.sh) es el wrapper pensado para
cron: carga `wlc_credenciales.env`, corre el reporte contra todos los WLC de
`wlcs.json`, y deja de ejecutarse solo después de la fecha `FECHA_FIN` definida
dentro del script (para no tener que acordarse de quitar el cron job al
terminar la semana de muestreo).

```bash
chmod +x run_reporte_semanal.sh
crontab -e
```

```
0 14 * * * /ruta/al/proyecto/run_reporte_semanal.sh >> /ruta/al/proyecto/cron.log 2>&1
0 18 * * * /ruta/al/proyecto/run_reporte_semanal.sh >> /ruta/al/proyecto/cron.log 2>&1
```

Si corres esto en WSL, habilita `systemd` (`/etc/wsl.conf` → `[boot]
systemd=true`, luego `wsl --shutdown` desde Windows) y `sudo systemctl enable
--now cron` para que el cron siga activo sin depender de tener una terminal
abierta.

## Salida generada (no versionada)

```
informes/
├── historico/
│   ├── historico_total.csv   # 1 fila por corrida por WLC
│   ├── historico_aps.csv     # 1 fila por AP por corrida
│   └── historico_ssid.csv    # 1 fila por SSID por corrida
├── <WLC-1>/
│   ├── reporte_ejecutivo_wlc_<timestamp>.md
│   ├── resumen_semanal.md    # se regenera en cada corrida
│   └── ...
└── <WLC-2>/...
```

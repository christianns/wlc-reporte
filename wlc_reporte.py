#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wlc_reporte.py
--------------
Conecta por SSH a uno o varios Cisco WLC (AireOS) vía Netmiko, recolecta
métricas de clientes inalámbricos y genera un reporte ejecutivo en Markdown
+ CSV/Excel por cada WLC, un histórico acumulativo (CSV) y un resumen
semanal (Markdown) que se regenera solo en cada corrida — pensado para
corridas repetidas (ej. 2 veces al día durante una semana, vía cron).

Métricas obtenidas:
  - Total de usuarios (clientes) asociados      (show client summary)
  - Usuarios por AP                             (show ap summary)
  - Usuarios por SSID/WLAN                      (show wlan summary, con
    fallback a 'show client wlan <id>' si la versión no muestra la columna)

Uso (un solo WLC):
  python wlc_reporte.py --host 192.168.1.10 --user admin --password secret
  python wlc_reporte.py --host 192.168.1.10 --debug   # guarda salidas crudas

Uso (varios WLC desde archivo de configuración, ver wlcs.json.example):
  python wlc_reporte.py --config wlcs.json --outdir informes

Requisitos:
  pip install netmiko
  # Opcional para Excel con gráficos: pip install pandas openpyxl
  # Opcional para el gráfico de tendencia (usuarios vs tiempo) en el resumen
  # semanal: pip install matplotlib
"""

import argparse
import base64
import csv
import getpass
import io
import json
import os
import re
import sys
from datetime import datetime

try:
    from netmiko import ConnectHandler
    from netmiko.exceptions import (
        NetmikoTimeoutException,
        NetmikoAuthenticationException,
        ReadTimeout,
    )
except ImportError:
    sys.exit("ERROR: Netmiko no está instalado. Ejecuta: pip install netmiko")

MAC_ANYWHERE = re.compile(r'([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}')


# ----------------------------------------------------------------------------
# Parsers: basados en las posiciones del ENCABEZADO real (no en posiciones
# fijas), para soportar los distintos formatos de AireOS (7.x / 8.x / 8.10),
# con o sin columnas IP Address / DSE Location / Clients.
# ----------------------------------------------------------------------------
def _columns_from_header(header_line):
    """[(nombre, start, end)] usando grupos de palabras separados por 2+ espacios."""
    return [(m.group(), m.start(), m.end())
            for m in re.finditer(r'\S+(?:[ ]+\S+)*?(?=\s{2,}|$)', header_line)]


def _header_line(lines, must_contain):
    for line in lines:
        if all(k in line for k in must_contain):
            return line
    return None


def parse_total_clients(output: str) -> int:
    m = re.search(r'Number of Clients[.\s]*(\d+)', output)
    if m:
        return int(m.group(1))
    return sum(1 for l in output.splitlines()
               if re.match(r'^\s*([0-9a-fA-F]{2}:){5}', l))


def parse_ap_summary(output: str):
    """'show ap summary' -> [{ap, clients, location}]. Requiere columna Clients."""
    lines = output.splitlines()
    header = _header_line(lines, ('AP Name', 'Clients'))
    if not header:
        return []
    cols = _columns_from_header(header.lstrip())
    names = [c[0] for c in cols]
    starts = [c[1] for c in cols]
    try:
        i_ap = next(i for i, n in enumerate(names) if n.startswith('AP Name'))
        i_cl = next(i for i, n in enumerate(names) if n.startswith('Clients'))
        i_loc = next((i for i, n in enumerate(names) if n.startswith('Location')), None)
    except StopIteration:
        return []

    seen = {}
    for line in lines:
        if not MAC_ANYWHERE.search(line):
            continue

        def cell(idx):
            s = starts[idx]
            e = starts[idx + 1] if idx + 1 < len(starts) else len(line) + 20
            return line[s:e].strip() if s < len(line) else ''

        raw = cell(i_cl)
        if raw.isdigit():
            clients = int(raw)
        elif line.split() and line.split()[-1].isdigit():   # fallback
            clients = int(line.split()[-1])
        else:
            continue
        name_cell = cell(i_ap)
        ap_name = name_cell.split()[0] if name_cell else line.split()[0]
        if ap_name not in seen:   # dedupe: la sección IPv6 repite los APs
            seen[ap_name] = {'ap': ap_name, 'clients': clients,
                             'location': cell(i_loc) if i_loc is not None else ''}
    return list(seen.values())


def parse_wlan_summary(output: str):
    """'show wlan summary' -> [{wlan_id, ssid, status, clients}].
    'clients' será None si la versión no incluye la columna Clients."""
    lines = output.splitlines()
    header = _header_line(lines, ('WLAN ID',))
    if not header:
        return []
    has_clients = any(n.startswith('Clients') for n, _, _ in _columns_from_header(header))
    wlans = []
    for line in lines:
        parts = re.split(r'\s{2,}', line.strip())
        if len(parts) < 3 or not parts[0].strip().isdigit():
            continue
        clients = None
        if has_clients:
            for p in parts[3:]:
                if p.strip().isdigit():
                    clients = int(p)
                    break
        wlans.append({'wlan_id': int(parts[0]),
                      'ssid': parts[1].split(' / ')[-1].strip(),
                      'status': parts[2].strip(),
                      'clients': clients})
    return wlans


def parse_client_wlan_count(output: str):
    """'show client wlan <id>' -> 'Number of Clients in WLAN... N'."""
    m = re.search(r'Number of Clients[^0-9]*(\d+)', output)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------------
# Configuración multi-WLC
# ----------------------------------------------------------------------------
def cargar_wlcs(path):
    """Lee un JSON con la lista de WLCs a consultar. Cada entrada:
      {"nombre": "WLC-Principal", "host": "192.168.1.10", "user": "admin",
       "password_env": "WLC1_PASSWORD"}
    El password NUNCA va en el archivo: se lee de la variable de entorno
    indicada en 'password_env' (ver wlc_credenciales.env.example)."""
    with open(path, encoding="utf-8") as f:
        wlcs = json.load(f)

    faltantes = []
    for w in wlcs:
        for campo in ("nombre", "host", "user", "password_env"):
            if campo not in w:
                sys.exit(f"ERROR: la entrada {w} en {path} no tiene el campo '{campo}'.")
        w["password"] = os.getenv(w["password_env"])
        if not w["password"]:
            faltantes.append(w["password_env"])
    if faltantes:
        sys.exit("ERROR: faltan variables de entorno con el password de estos WLC: "
                 f"{', '.join(faltantes)}. Expórtalas antes de correr el script "
                 "(ver wlc_credenciales.env.example).")
    return wlcs


def registrar_historico(data, nombre_wlc, historico_dir, timestamp):
    """Agrega (append) una fila por métrica a 3 CSV acumulativos, pensados para
    correr el script muchas veces (ej. 2x/día durante una semana) sin perder
    las muestras anteriores. No sobrescribe: cada corrida suma filas nuevas."""
    os.makedirs(historico_dir, exist_ok=True)

    def append(nombre_archivo, fieldnames, filas):
        ruta = os.path.join(historico_dir, nombre_archivo)
        existe = os.path.exists(ruta)
        with open(ruta, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            if not existe:
                w.writeheader()
            for fila in filas:
                w.writerow(fila)
        return ruta

    append("historico_total.csv", ["timestamp", "wlc", "total_clients"],
           [{"timestamp": timestamp, "wlc": nombre_wlc,
             "total_clients": data["total_clients"]}])
    append("historico_aps.csv",
           ["timestamp", "wlc", "ap", "location", "clients"],
           [{"timestamp": timestamp, "wlc": nombre_wlc, **a} for a in data["aps"]])
    append("historico_ssid.csv",
           ["timestamp", "wlc", "wlan_id", "ssid", "status", "clients"],
           [{"timestamp": timestamp, "wlc": nombre_wlc, **w} for w in data["wlans"]])
    print(f"[✔] Histórico actualizado en: {historico_dir}")


def _leer_historico(historico_dir, nombre_archivo, nombre_wlc):
    ruta = os.path.join(historico_dir, nombre_archivo)
    if not os.path.exists(ruta):
        return []
    with open(ruta, encoding="utf-8") as f:
        return [row for row in csv.DictReader(f) if row.get("wlc") == nombre_wlc]


def generar_grafico_tendencia_base64(filas_total, nombre_wlc):
    """Línea de usuarios totales vs tiempo con TODAS las muestras acumuladas,
    devuelta como PNG codificado en base64 (para embeber en el .md, sin
    generar un archivo aparte que haya que llevar junto al reporte).
    Requiere matplotlib (opcional); si no está instalado, se omite sin
    romper el resto del resumen semanal."""
    try:
        import matplotlib
        matplotlib.use("Agg")   # sin display, solo generar la imagen en memoria
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("[i] matplotlib no instalado; se omite el gráfico de tendencia "
              "(pip install matplotlib).")
        return None

    fechas = [datetime.fromisoformat(r["timestamp"]) for r in filas_total]
    totales = [int(r["total_clients"]) for r in filas_total]

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(fechas, totales, marker="o", linewidth=1.8, color="#2563eb")
    ax.set_title(f"Usuarios conectados vs tiempo — {nombre_wlc}")
    ax.set_xlabel("Fecha/Hora de la muestra")
    ax.set_ylabel("Usuarios totales")
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d-%b %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def generar_resumen_semanal(nombre_wlc, historico_dir, directorio):
    """Regenera (sobrescribe) un resumen de tendencia con TODO el histórico
    acumulado de este WLC hasta ahora. Se llama después de cada muestra, así
    que el resumen siempre refleja las muestras tomadas hasta el momento —
    no hace falta esperar a que termine la semana de muestreo."""
    filas_total = sorted(_leer_historico(historico_dir, "historico_total.csv", nombre_wlc),
                         key=lambda r: r["timestamp"])
    if not filas_total:
        return None
    filas_aps = _leer_historico(historico_dir, "historico_aps.csv", nombre_wlc)
    filas_wlans = _leer_historico(historico_dir, "historico_ssid.csv", nombre_wlc)

    totales = [int(r["total_clients"]) for r in filas_total]
    n = len(totales)
    promedio = round(sum(totales) / n, 1)
    max_total = max(totales)
    i_min, i_max = totales.index(min(totales)), totales.index(max(totales))

    L = []
    L.append(f"# Resumen Semanal – Red Inalámbrica (WLC {nombre_wlc})")
    L.append(f"\n**Generado:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    L.append(f"**Periodo cubierto:** {filas_total[0]['timestamp']} → {filas_total[-1]['timestamp']}  ")
    L.append(f"**Muestras acumuladas:** {n}\n")

    L.append("## 1. Tendencia de usuarios totales conectados\n")
    grafico_b64 = generar_grafico_tendencia_base64(filas_total, nombre_wlc)
    if grafico_b64:
        L.append(f"![Tendencia de usuarios conectados](data:image/png;base64,{grafico_b64})\n")
    L.append("| # | Fecha/Hora | Total | Distribución |")
    L.append("|--:|---|---:|---|")
    for i, r in enumerate(filas_total, 1):
        t = int(r["total_clients"])
        L.append(f"| {i} | {r['timestamp']} | {t} | `{barra(t, max_total)}` |")
    L.append("")
    L.append(f"- **Promedio:** {promedio} usuarios")
    L.append(f"- **Mínimo:** {totales[i_min]} usuarios ({filas_total[i_min]['timestamp']})")
    L.append(f"- **Máximo:** {totales[i_max]} usuarios ({filas_total[i_max]['timestamp']})")
    if n >= 2:
        delta = totales[-1] - totales[0]
        L.append(f"- **Variación primera→última muestra:** {'+' if delta >= 0 else ''}{delta} usuarios")
    L.append("")

    por_ap = {}
    for r in filas_aps:
        por_ap.setdefault(r["ap"], []).append(int(r["clients"]))
    ap_top = None
    if por_ap:
        ap_ordenados = sorted(por_ap.items(), key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
        ap_top = ap_ordenados[0]
        L.append("## 2. Usuarios por AP (acumulado de la semana)\n")
        L.append("| AP | Promedio | Mínimo | Máximo | Muestras |")
        L.append("|---|---:|---:|---:|---:|")
        for ap, vals in ap_ordenados:
            L.append(f"| {ap} | {round(sum(vals)/len(vals), 1)} | {min(vals)} | {max(vals)} | {len(vals)} |")
        L.append("")

    por_ssid = {}
    for r in filas_wlans:
        if not r.get("clients"):
            continue
        por_ssid.setdefault(r["ssid"], []).append(int(r["clients"]))
    if por_ssid:
        ssid_ordenados = sorted(por_ssid.items(), key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
        L.append("## 3. Usuarios por SSID (acumulado de la semana)\n")
        L.append("| SSID | Promedio | Mínimo | Máximo | Muestras |")
        L.append("|---|---:|---:|---:|---:|")
        for ssid, vals in ssid_ordenados:
            L.append(f"| {ssid} | {round(sum(vals)/len(vals), 1)} | {min(vals)} | {max(vals)} | {len(vals)} |")
        L.append("")

    L.append("## 4. Observaciones\n")
    if ap_top:
        ap, vals = ap_top
        L.append(f"- ℹ️ El AP con mayor carga promedio en la semana es **{ap}** "
                 f"({round(sum(vals)/len(vals), 1)} usuarios en promedio sobre {len(vals)} muestras).")
    if n < 14:
        L.append(f"- ℹ️ Este resumen se actualiza automáticamente con cada nueva muestra "
                 f"(llevas {n} de las muestras esperadas en la semana).")
    L.append("- ℹ️ Los datos completos de cada muestra están en los CSV de "
             f"`{historico_dir}` (columna `wlc` = `{nombre_wlc}`).\n")

    os.makedirs(directorio, exist_ok=True)
    ruta = os.path.join(directorio, "resumen_semanal.md")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[✔] Resumen semanal actualizado: {ruta}")
    return ruta


# ----------------------------------------------------------------------------
# Recolección
# ----------------------------------------------------------------------------
def recolectar(host, user, password, debug=False, outdir=".", timeout=30, read_timeout=60):
    device = {
        "device_type": "cisco_wlc",     # driver Netmiko para AireOS (WLC 5520)
        "host": host,
        "username": user,
        "password": password,
        "session_timeout": timeout,
        "conn_timeout": timeout,
        "fast_cli": False,              # el CLI del WLC es lento; no acelerar
    }

    raw = {}
    fallos = []

    def run(cmd):
        """Envía un comando. Si el WLC no responde a tiempo (ReadTimeout, típico
        en AireOS con comandos lentos o nombres de AP largos) o hay cualquier
        otro error, no se aborta todo el reporte: se registra el fallo y se
        sigue con lo demás."""
        print(f"[+] {cmd}")
        try:
            out = conn.send_command(cmd, read_timeout=read_timeout)
        except ReadTimeout as e:
            print(f"[!] Timeout esperando respuesta a '{cmd}': {e}")
            fallos.append(cmd)
            out = ""
        except Exception as e:
            print(f"[!] Error ejecutando '{cmd}': {e}")
            fallos.append(cmd)
            out = ""
        raw[cmd] = out
        return out

    print(f"[+] Conectando a {host} ...")
    try:
        conn = ConnectHandler(**device)
    except NetmikoAuthenticationException:
        sys.exit(f"ERROR: Autenticación fallida contra {host}. Revisa usuario/password.")
    except NetmikoTimeoutException:
        sys.exit(f"ERROR: No se pudo conectar a {host} (timeout). "
                 "Revisa IP/puerto SSH, ACLs o conectividad de red.")
    except Exception as e:
        sys.exit(f"ERROR: No se pudo conectar a {host}: {e}")

    try:
        print("[+] Conectado. Deshabilitando paginación...")
        run("config paging disable")

        out_clients = run("show client summary")
        out_aps = run("show ap summary")
        out_wlans = run("show wlan summary")

        aps = parse_ap_summary(out_aps)
        wlans = parse_wlan_summary(out_wlans)

        # Fallback por SSID: si show wlan summary no trae columna Clients
        # (comportamiento conocido de varias versiones de AireOS), se pregunta
        # el conteo por cada WLAN con 'show client wlan <id>'.
        if wlans and all(w['clients'] is None for w in wlans):
            print("[i] 'show wlan summary' no muestra clientes; usando 'show client wlan <id>' por WLAN...")
            for w in wlans:
                n = parse_client_wlan_count(run(f"show client wlan {w['wlan_id']}"))
                w['clients'] = n if n is not None else 0
    finally:
        try:
            conn.disconnect()
            print("[+] Desconectado.")
        except Exception:
            pass

    if fallos:
        print(f"[!] {len(fallos)} comando(s) fallaron y se omitieron del reporte: "
              f"{', '.join(fallos)}")

    if debug:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for cmd, out in raw.items():
            fname = re.sub(r'[^a-z0-9]+', '_', cmd.lower()).strip('_')
            path = os.path.join(outdir, f"raw_{fname}_{stamp}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"[debug] salida cruda guardada: {path}")

    return {
        "total_clients": parse_total_clients(out_clients),
        "aps": aps,
        "wlans": wlans,
    }


# ----------------------------------------------------------------------------
# Reporte ejecutivo (Markdown)
# ----------------------------------------------------------------------------
def barra(valor, maximo, ancho=30):
    if maximo == 0:
        return ""
    n = round(valor / maximo * ancho)
    return "█" * n + "░" * (ancho - n)


def generar_reporte(data, host, directorio=".", sufijo=""):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = data["total_clients"]
    aps = sorted(data["aps"], key=lambda x: x["clients"], reverse=True)
    wlans = sorted(data["wlans"], key=lambda x: (x["clients"] or 0), reverse=True)
    max_ap = max((a["clients"] for a in aps), default=0)
    max_wlan = max((w["clients"] or 0 for w in wlans), default=0)
    n_aps = len(aps)
    promedio = round(total / n_aps, 1) if n_aps else 0
    top_ap = aps[0] if aps else None

    L = []
    L.append(f"# Reporte Ejecutivo – Red Inalámbrica (WLC {host})")
    L.append(f"\n**Fecha de generación:** {ahora}  ")
    L.append(f"**Fuente:** Cisco WLC 5520 (AireOS) — {host}\n")

    L.append("## 1. Resumen general\n")
    L.append("| Indicador | Valor |")
    L.append("|---|---:|")
    L.append(f"| Total de usuarios conectados | **{total}** |")
    L.append(f"| Puntos de acceso (APs) | {n_aps} |")
    L.append(f"| Promedio de usuarios por AP | {promedio} |")
    if top_ap:
        L.append(f"| AP con mayor carga | {top_ap['ap']} ({top_ap['clients']} usuarios) |")
    L.append("")

    L.append("## 2. Usuarios por punto de acceso (AP)\n")
    L.append("| AP | Ubicación | Clientes | Distribución |")
    L.append("|---|---|---:|---|")
    for a in aps:
        L.append(f"| {a['ap']} | {a['location']} | {a['clients']} "
                 f"| `{barra(a['clients'], max_ap)}` |")
    L.append("")

    L.append("## 3. Usuarios por SSID (WLAN)\n")
    L.append("| SSID | Estado | Clientes | Distribución |")
    L.append("|---|---|---:|---|")
    for w in wlans:
        c = w["clients"] if w["clients"] is not None else "N/D"
        L.append(f"| {w['ssid']} | {w['status']} | {c} "
                 f"| `{barra(w['clients'] or 0, max_wlan)}` |")
    L.append("")

    L.append("## 4. Observaciones y recomendaciones\n")
    if top_ap and total > 0 and top_ap["clients"] / total > 0.35:
        L.append(f"- ⚠️ El AP **{top_ap['ap']}** concentra el "
                 f"**{round(top_ap['clients']/total*100)}%** de los usuarios: "
                 "revisar balanceo de carga (RF Load Balancing) o cobertura en su zona.")
    L.append(f"- ℹ️ Capacidad promedio actual: **{promedio} usuarios/AP** sobre {n_aps} APs; "
             "dimensionar crecimiento futuro sobre este baseline.")
    L.append("- ℹ️ Se recomienda ejecutar este reporte periódicamente "
             "(diario/semanal) para construir un histórico de ocupación.")
    L.append("- ℹ️ La columna **Distribución** es una barra visual proporcional al "
             "valor más alto de esa misma tabla (no un porcentaje del total de la red): "
             "permite comparar de un vistazo la carga relativa entre APs o entre SSID.\n")

    os.makedirs(directorio, exist_ok=True)
    nombre_archivo = f"reporte_ejecutivo_wlc_{sufijo}.md" if sufijo else "reporte_ejecutivo_wlc.md"
    ruta_md = os.path.join(directorio, nombre_archivo)
    with open(ruta_md, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[✔] Reporte Markdown: {ruta_md}")
    return ruta_md


def exportar_csv(data, directorio=".", sufijo=""):
    os.makedirs(directorio, exist_ok=True)
    aps = sorted(data["aps"], key=lambda x: x["clients"], reverse=True)
    wlans = sorted(data["wlans"], key=lambda x: (x["clients"] or 0), reverse=True)
    suf = f"_{sufijo}" if sufijo else ""

    ruta_ap = os.path.join(directorio, f"usuarios_por_ap{suf}.csv")
    with open(ruta_ap, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ap", "location", "clients"])
        w.writeheader()
        for a in aps:
            w.writerow(a)

    ruta_ssid = os.path.join(directorio, f"usuarios_por_ssid{suf}.csv")
    with open(ruta_ssid, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["wlan_id", "ssid", "status", "clients"])
        w.writeheader()
        for x in wlans:
            w.writerow(x)

    print(f"[✔] CSVs: {ruta_ap} | {ruta_ssid}")
    return ruta_ap, ruta_ssid


def exportar_excel(data, host, directorio=".", sufijo=""):
    try:
        import pandas as pd
    except ImportError:
        print("[i] pandas/openpyxl no instalados; se omite el Excel.")
        return None

    os.makedirs(directorio, exist_ok=True)
    aps = sorted(data["aps"], key=lambda x: x["clients"], reverse=True)
    wlans = sorted(data["wlans"], key=lambda x: (x["clients"] or 0), reverse=True)
    nombre_archivo = f"reporte_wlc_{sufijo}.xlsx" if sufijo else "reporte_wlc.xlsx"
    ruta = os.path.join(directorio, nombre_archivo)
    with pd.ExcelWriter(ruta, engine="openpyxl") as writer:
        pd.DataFrame([{"WLC": host, "Fecha": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "Total clientes": data["total_clients"]}]).to_excel(
            writer, sheet_name="Resumen", index=False)
        pd.DataFrame(aps).to_excel(writer, sheet_name="Usuarios por AP", index=False)
        pd.DataFrame(wlans).to_excel(writer, sheet_name="Usuarios por SSID", index=False)
        try:
            from openpyxl.chart import BarChart, Reference
            wb = writer.book
            ws_ap = writer.sheets["Usuarios por AP"]
            col = list(pd.DataFrame(aps).columns).index("clients") + 1
            ch = BarChart(); ch.title = "Usuarios por AP"
            ch.add_data(Reference(ws_ap, min_col=col, min_row=1, max_row=1 + len(aps)),
                        titles_from_data=True)
            ch.set_categories(Reference(ws_ap, min_col=1, min_row=2, max_row=1 + len(aps)))
            ws_ap.add_chart(ch, "H2")
            ws_w = writer.sheets["Usuarios por SSID"]
            colw = list(pd.DataFrame(wlans).columns).index("clients") + 1
            ch2 = BarChart(); ch2.title = "Usuarios por SSID"
            ch2.add_data(Reference(ws_w, min_col=colw, min_row=1, max_row=1 + len(wlans)),
                         titles_from_data=True)
            ch2.set_categories(Reference(ws_w, min_col=2, min_row=2, max_row=1 + len(wlans)))
            ws_w.add_chart(ch2, "H2")
        except Exception as e:
            print(f"[i] No se pudieron agregar gráficos: {e}")
    print(f"[✔] Excel: {ruta}")
    return ruta


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def procesar_wlc(nombre, host, user, password, args, stamp_archivo, stamp_historico):
    """Recolecta y reporta un WLC; devuelve True si se pudo generar el reporte."""
    print(f"\n{'=' * 70}\n[WLC] {nombre} ({host})\n{'=' * 70}")
    slug = re.sub(r'[^A-Za-z0-9._-]+', '_', nombre).strip('_') or "wlc"
    directorio = os.path.join(args.outdir, slug)
    try:
        data = recolectar(host, user, password, debug=args.debug, outdir=directorio,
                          timeout=args.timeout, read_timeout=args.read_timeout)
    except SystemExit as e:
        # recolectar() usa sys.exit() ante fallas de conexión/auth; en modo
        # multi-WLC no queremos abortar los demás WLCs de la lista por uno solo.
        print(f"[!] {nombre}: {e}")
        return False

    print(f"\n=== MÉTRICAS: {nombre} ===")
    print(f"Total de usuarios : {data['total_clients']}")
    for a in sorted(data["aps"], key=lambda x: x["clients"], reverse=True):
        print(f"  {a['ap']:<25} {a['clients']}")
    for w in sorted(data["wlans"], key=lambda x: (x["clients"] or 0), reverse=True):
        print(f"  SSID {w['ssid']:<25} {w['clients']}")

    generar_reporte(data, host, directorio, sufijo=stamp_archivo)
    exportar_csv(data, directorio, sufijo=stamp_archivo)
    exportar_excel(data, host, directorio, sufijo=stamp_archivo)
    historico_dir = os.path.join(args.outdir, "historico")
    registrar_historico(data, nombre, historico_dir, stamp_historico)
    generar_resumen_semanal(nombre, historico_dir, directorio)
    return True


def main():
    p = argparse.ArgumentParser(description="Reporte ejecutivo de clientes en Cisco WLC")
    p.add_argument("--config", help="Ruta a un JSON con varios WLCs (ver wlcs.json.example). "
                                     "Si se usa, --host/--user/--password se ignoran.")
    p.add_argument("--host", help="IP o hostname del WLC (para un solo WLC, sin --config)")
    p.add_argument("--user", default=os.getenv("WLC_USER"), help="Usuario SSH (o WLC_USER)")
    p.add_argument("--password", default=os.getenv("WLC_PASSWORD"),
                   help="Password SSH (o WLC_PASSWORD)")
    p.add_argument("--debug", action="store_true",
                   help="Guardar salidas crudas de los comandos para diagnóstico")
    p.add_argument("--outdir", default=".", help="Directorio de salida (default: actual)")
    p.add_argument("--timeout", type=int, default=30,
                   help="Timeout de conexión SSH en segundos (default: 30)")
    p.add_argument("--read-timeout", type=int, default=60,
                   help="Timeout de lectura por comando en segundos (default: 60). "
                        "Auméntalo si el WLC tarda en responder a 'show ap summary' "
                        "con muchos APs.")
    args = p.parse_args()

    if not args.config and not args.host:
        p.error("indica --config <archivo.json> (varios WLCs) o --host <ip> (uno solo)")

    os.makedirs(args.outdir, exist_ok=True)
    ahora = datetime.now()
    stamp_archivo = ahora.strftime("%Y%m%d_%H%M%S")      # para nombres de archivo
    stamp_historico = ahora.isoformat(timespec="seconds")  # para filas del histórico

    if args.config:
        wlcs = cargar_wlcs(args.config)
        resultados = [procesar_wlc(w["nombre"], w["host"], w["user"], w["password"],
                                   args, stamp_archivo, stamp_historico) for w in wlcs]
        ok = sum(resultados)
        print(f"\n[✔] {ok}/{len(wlcs)} WLC procesados correctamente.")
        if ok < len(wlcs):
            sys.exit(1)
    else:
        user = args.user or input("Usuario SSH: ")
        password = args.password or getpass.getpass("Password: ")
        if not procesar_wlc(args.host, args.host, user, password,
                            args, stamp_archivo, stamp_historico):
            sys.exit(1)


if __name__ == "__main__":
    main()

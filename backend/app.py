"""
Monitor de Cámaras Frigoríficas - Backend (Flask + SQLite)

API REST que almacena y sirve lecturas de temperatura y humedad de sensores
instalados en heladeras, exhibidoras y cámaras de supermercados de CABA.
Cada sensor tiene nombre, dirección, coordenadas y un tipo de equipo con sus
rangos de operación (basados en las pautas del Código Alimentario Argentino).

Si no hay sensores reales conectados, el backend simula las lecturas:
  - Una lectura cada 10 minutos, variando al azar DENTRO del rango del equipo.
  - Cada tanto ocurre una FALLA (el equipo deja de enfriar): la temperatura
    supera el rango normal durante 40-120 minutos y se dispara la alarma.

Diseñado para correr en un hosting gratuito (Render, Railway, Fly.io, etc.):
  - La simulación es "perezosa": en cada request se completan las lecturas que
    faltan desde la última guardada. Así, aunque el servicio se duerma por
    inactividad, al despertar el historial queda continuo.
  - Las fallas se calculan de forma determinística a partir de la hora, así
    son consistentes aunque el servidor se reinicie.
  - Toda la hora se guarda en UTC (ISO 8601) y el navegador la convierte a hora local.
"""

import csv
import io
import math
import os
import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request, send_from_directory

# ============================================================
# CONFIGURACIÓN (todo sobreescribible por variables de entorno)
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

DATABASE = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "sensor_data.db"))
SIM_ENABLED = os.environ.get("SIMULATION_ENABLED", "true").lower() == "true"
SIM_INTERVAL = int(os.environ.get("SIM_INTERVAL_SECONDS", "600"))  # 1 lectura cada 10 min
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
INGEST_API_KEY = os.environ.get("INGEST_API_KEY")                  # para sensores reales

SCHEMA_VERSION = 4
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
AR_TZ = timezone(timedelta(hours=-3))  # Argentina (sin horario de verano)

# ============================================================
# TIPOS DE EQUIPO (rangos normales de operación)
#   temp / hum:  rango normal; la simulación varía al azar dentro de él
#   alarm_low:   por debajo -> "Baja Temperatura" (None = sin alarma de baja)
#   Por encima del máximo de temperatura -> "Temperatura Alta"
#   Humedad fuera de rango -> "Humedad fuera de rango"
# ============================================================
EQUIPMENT = {
    "lacteos": {
        "label": "Heladera de lácteos y fiambres",
        "temp": (2.0, 5.0), "hum": (70.0, 85.0), "alarm_low": 2.0, "icon": "fa-cheese",
    },
    "carnes": {
        "label": "Heladera de carnes, pollos y pescados",
        "temp": (0.0, 2.0), "hum": (70.0, 85.0), "alarm_low": 0.0, "icon": "fa-drumstick-bite",
    },
    "verduleria": {
        "label": "Exhibidora de frutas y verduras",
        "temp": (6.0, 10.0), "hum": (90.0, 95.0), "alarm_low": 6.0, "icon": "fa-carrot",
    },
    "congelados": {
        "label": "Cámara de congelados",
        "temp": (-20.0, -15.0), "hum": (75.0, 90.0), "alarm_low": None, "icon": "fa-snowflake",
    },
}

# ============================================================
# SENSORES (direcciones ilustrativas dentro de CABA)
#   fail_prob: probabilidad de que ocurra una falla en cada bloque de 6 horas
# ============================================================
SENSORS = [
    {"id": 1, "name": "Supermercado 1", "address": "Av. Santa Fe 3253", "barrio": "Palermo",
     "lat": -34.5880, "lng": -58.4115, "equipment": "lacteos", "fail_prob": 0.12},
    {"id": 2, "name": "Supermercado 2", "address": "Av. Las Heras 2100", "barrio": "Recoleta",
     "lat": -34.5903, "lng": -58.3967, "equipment": "carnes", "fail_prob": 0.10},
    {"id": 3, "name": "Supermercado 3", "address": "Av. Cabildo 2280", "barrio": "Belgrano",
     "lat": -34.5617, "lng": -58.4563, "equipment": "verduleria", "fail_prob": 0.12},
    {"id": 4, "name": "Supermercado 4", "address": "Av. Rivadavia 5100", "barrio": "Caballito",
     "lat": -34.6196, "lng": -58.4405, "equipment": "congelados", "fail_prob": 0.10},
    {"id": 5, "name": "Supermercado 5", "address": "Defensa 900", "barrio": "San Telmo",
     "lat": -34.6195, "lng": -58.3720, "equipment": "lacteos", "fail_prob": 0.60},  # equipo con fallas frecuentes
    {"id": 6, "name": "Supermercado 6", "address": "Av. Triunvirato 4300", "barrio": "Villa Urquiza",
     "lat": -34.5735, "lng": -58.4870, "equipment": "carnes", "fail_prob": 0.12},
    {"id": 7, "name": "Supermercado 7", "address": "Av. Rivadavia 6900", "barrio": "Flores",
     "lat": -34.6290, "lng": -58.4630, "equipment": "verduleria", "fail_prob": 0.10},
    {"id": 8, "name": "Supermercado 8", "address": "Av. Corrientes 3800", "barrio": "Almagro",
     "lat": -34.6031, "lng": -58.4203, "equipment": "congelados", "fail_prob": 0.15},
]
SENSORS_BY_ID = {s["id"]: s for s in SENSORS}
DEFAULT_SENSOR_ID = SENSORS[0]["id"]

FAIL_BLOCK_SECONDS = 6 * 3600

app = Flask(__name__, static_folder=None)
_db_lock = threading.Lock()


# ============================================================
# UTILIDADES
# ============================================================
def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_ts(dt):
    return dt.astimezone(timezone.utc).strftime(TS_FORMAT)


def from_ts(s):
    return datetime.strptime(s, TS_FORMAT).replace(tzinfo=timezone.utc)


def get_conn():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def equipment_of(sensor):
    return EQUIPMENT[sensor["equipment"]]


def compute_status(sensor, temperature, humidity):
    eq = equipment_of(sensor)
    t_min, t_max = eq["temp"]
    h_min, h_max = eq["hum"]
    if temperature > t_max:
        return "Temperatura Alta"
    if eq["alarm_low"] is not None and temperature < eq["alarm_low"]:
        return "Baja Temperatura"
    if not (h_min <= humidity <= h_max):
        return "Humedad fuera de rango"
    return "Normal"


def thresholds(sensor):
    eq = equipment_of(sensor)
    return {
        "temp_min": eq["temp"][0], "temp_max": eq["temp"][1],
        "hum_min": eq["hum"][0], "hum_max": eq["hum"][1],
        "alarm_low": eq["alarm_low"],
    }


def row_to_dict(r):
    return {
        "sensor_id": r["sensor_id"],
        "device_id": device_id(r["sensor_id"]),
        "temperature": r["temperature"],
        "humidity": r["humidity"],
        "timestamp": r["timestamp"],
        "status": r["status"],
        "source": r["source"],
    }


def device_id(sensor_id):
    return f"TH-CABA-{sensor_id:02d}"


def sensor_info(s):
    eq = equipment_of(s)
    info = {k: s[k] for k in ("id", "name", "address", "barrio", "lat", "lng", "equipment")}
    info["device_id"] = device_id(s["id"])
    info.update({"equipment_label": eq["label"], "icon": eq["icon"], "thresholds": thresholds(s)})
    return info


def requested_sensor():
    """Sensor pedido con ?sensor=<id> (por defecto el primero). None si no existe."""
    sid = request.args.get("sensor", DEFAULT_SENSOR_ID, type=int)
    return SENSORS_BY_ID.get(sid)


def not_found_sensor():
    return jsonify({"error": "Sensor inexistente",
                    "available": [s["id"] for s in SENSORS]}), 404


# ============================================================
# SIMULADOR DE SENSOR
# ============================================================
def _reflect(value, lo, hi):
    """Mantiene el valor dentro de [lo, hi] rebotando en los bordes."""
    width = hi - lo
    if width <= 0:
        return lo
    v = (value - lo) % (2 * width)
    return lo + (v if v <= width else 2 * width - v)


def failure_at(sensor, when):
    """Intensidad de falla (0 = sin falla, 1 = pico) del sensor en el instante 'when'.

    En cada bloque de 6 h se decide al azar (con semilla fija por sensor y bloque)
    si ocurre una falla, cuándo empieza, cuánto dura y qué tan grave es.
    Devuelve (intensidad 0..1, exceso máximo de temperatura en °C).
    """
    ts = when.timestamp()
    block = int(ts // FAIL_BLOCK_SECONDS)
    rng = random.Random(f"falla-{sensor['id']}-{block}")
    if rng.random() >= sensor["fail_prob"]:
        return 0.0, 0.0
    start = rng.randint(0, 28) * 600          # arranca en algún momento del bloque
    duration = rng.randint(4, 12) * 600       # 40 a 120 minutos
    peak = rng.uniform(3.0, 12.0)             # °C por encima del máximo del rango
    pos = ts - block * FAIL_BLOCK_SECONDS - start
    if pos < 0 or pos > duration:
        return 0.0, 0.0
    p = pos / duration
    # Sube rápido, se sostiene y vuelve cuando el equipo se recupera
    intensity = min(1.0, 0.25 + 2.5 * p) if p < 0.8 else max(0.25, (1 - p) / 0.2)
    return intensity, peak


def simulate_next(sensor, prev_temp, prev_hum, when):
    """Siguiente lectura: variación aleatoria dentro del rango normal del equipo,
    salvo que haya una falla en curso (la temperatura supera el rango)."""
    eq = equipment_of(sensor)
    t_min, t_max = eq["temp"]
    h_min, h_max = eq["hum"]

    # Partimos del último valor normal (si veníamos de una falla, se recorta al rango)
    base_t = min(max(prev_temp, t_min), t_max)
    base_h = min(max(prev_hum, h_min), h_max)

    temp = _reflect(base_t + random.gauss(0, 0.25 * (t_max - t_min)), t_min, t_max)
    hum = _reflect(base_h + random.gauss(0, 0.20 * (h_max - h_min)), h_min, h_max)

    intensity, peak = failure_at(sensor, when)
    if intensity > 0:
        temp = t_max + peak * intensity + random.gauss(0, 0.2)
        temp = max(temp, t_max + 0.3)
        hum = min(99.0, h_max + (2.0 + 6.0 * intensity) + random.gauss(0, 0.5))

    return round(temp, 1), round(hum, 1)


def ensure_up_to_date():
    """Completa las lecturas simuladas que faltan hasta 'ahora' y purga lo viejo."""
    if not SIM_ENABLED:
        return
    now = utcnow()
    earliest_allowed = now - timedelta(hours=24)

    with _db_lock:
        conn = get_conn()
        try:
            rows = []
            for s in SENSORS:
                last = conn.execute(
                    "SELECT temperature, humidity, timestamp FROM sensor_readings "
                    "WHERE sensor_id = ? AND source IN ('programada', 'a demanda') "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (s["id"],),
                ).fetchone()
                eq = equipment_of(s)
                if last is None:
                    t, h = sum(eq["temp"]) / 2, sum(eq["hum"]) / 2
                    start = earliest_allowed
                else:
                    t, h = last["temperature"], last["humidity"]
                    start = max(from_ts(last["timestamp"]), earliest_allowed)

                # Siguiente punto de la grilla de 10 minutos
                epoch = int(start.timestamp())
                cursor = datetime.fromtimestamp(
                    (epoch // SIM_INTERVAL + 1) * SIM_INTERVAL, tz=timezone.utc)
                while cursor <= now:
                    t, h = simulate_next(s, t, h, cursor)
                    rows.append((s["id"], t, h, to_ts(cursor), compute_status(s, t, h), "programada"))
                    cursor += timedelta(seconds=SIM_INTERVAL)

            if rows:
                conn.executemany(
                    "INSERT INTO sensor_readings "
                    "(sensor_id, temperature, humidity, timestamp, status, source) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
            conn.execute(
                "DELETE FROM sensor_readings WHERE timestamp < ?",
                (to_ts(now - timedelta(days=RETENTION_DAYS)),),
            )
            conn.commit()
        finally:
            conn.close()


# ============================================================
# INICIALIZACIÓN DE LA BASE DE DATOS
# ============================================================
def init_database():
    os.makedirs(os.path.dirname(os.path.abspath(DATABASE)), exist_ok=True)
    conn = get_conn()

    # Si la base es de una versión anterior, se regenera (los datos eran simulados)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.executescript("DROP TABLE IF EXISTS sensor_readings; DROP TABLE IF EXISTS sensors;")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensors (
            id        INTEGER PRIMARY KEY,
            name      TEXT NOT NULL,
            address   TEXT NOT NULL,
            barrio    TEXT NOT NULL,
            lat       REAL NOT NULL,
            lng       REAL NOT NULL,
            equipment TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id   INTEGER NOT NULL REFERENCES sensors(id),
            temperature REAL NOT NULL,
            humidity    REAL NOT NULL,
            timestamp   TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'Normal',
            source      TEXT NOT NULL DEFAULT 'programada'
        );
        CREATE INDEX IF NOT EXISTS idx_readings_sensor_ts
            ON sensor_readings(sensor_id, timestamp);
        """
    )
    conn.executemany(
        "INSERT OR REPLACE INTO sensors (id, name, address, barrio, lat, lng, equipment) "
        "VALUES (:id, :name, :address, :barrio, :lat, :lng, :equipment)",
        SENSORS,
    )
    conn.commit()
    conn.close()
    ensure_up_to_date()  # siembra las últimas 24 h si la base está vacía


@app.before_request
def _refresh_simulation():
    if request.path.startswith("/api/"):
        ensure_up_to_date()


# ============================================================
# RUTAS
# ============================================================
@app.route("/")
def serve_frontend():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": to_ts(utcnow())})


@app.route("/api/equipment")
def get_equipment():
    return jsonify(EQUIPMENT)


@app.route("/api/sensors")
def get_sensors():
    """Todos los sensores con su ubicación, tipo de equipo y última lectura (para el mapa)."""
    conn = get_conn()
    result = []
    for s in SENSORS:
        r = conn.execute(
            "SELECT * FROM sensor_readings WHERE sensor_id = ? "
            "ORDER BY timestamp DESC, id DESC LIMIT 1",
            (s["id"],),
        ).fetchone()
        item = sensor_info(s)
        item["last_reading"] = row_to_dict(r) if r else None
        result.append(item)
    conn.close()
    return jsonify(result)


@app.route("/api/current-reading")
def get_current_reading():
    sensor = requested_sensor()
    if sensor is None:
        return not_found_sensor()
    conn = get_conn()
    r = conn.execute(
        "SELECT * FROM sensor_readings WHERE sensor_id = ? "
        "ORDER BY timestamp DESC, id DESC LIMIT 1",
        (sensor["id"],),
    ).fetchone()
    conn.close()
    if r is None:
        return jsonify({"error": "No data available"}), 404
    data = row_to_dict(r)
    data["sensor"] = sensor_info(sensor)
    return jsonify(data)


@app.route("/api/history")
def get_history():
    sensor = requested_sensor()
    if sensor is None:
        return not_found_sensor()
    limit = max(1, min(request.args.get("limit", 10, type=int), 500))
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM sensor_readings WHERE sensor_id = ? "
        "ORDER BY timestamp DESC, id DESC LIMIT ?",
        (sensor["id"], limit),
    ).fetchall()
    conn.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/readings-range")
def get_readings_range():
    """Lecturas de las últimas N horas de un sensor (para las gráficas)."""
    sensor = requested_sensor()
    if sensor is None:
        return not_found_sensor()
    hours = max(1, min(request.args.get("hours", 24, type=int), 24 * RETENTION_DAYS))
    points = max(10, min(request.args.get("points", 144, type=int), 1000))
    start = utcnow() - timedelta(hours=hours)

    conn = get_conn()
    rows = conn.execute(
        "SELECT temperature, humidity, timestamp FROM sensor_readings "
        "WHERE sensor_id = ? AND timestamp >= ? ORDER BY timestamp ASC",
        (sensor["id"], to_ts(start)),
    ).fetchall()
    conn.close()

    # Si hay más lecturas que puntos pedidos, se promedian por tramos de tiempo
    if len(rows) > points:
        bucket_seconds = hours * 3600 / points
        buckets = {}
        for r in rows:
            idx = int((from_ts(r["timestamp"]) - start).total_seconds() // bucket_seconds)
            b = buckets.setdefault(idx, {"t": [], "h": [], "ts": r["timestamp"]})
            b["t"].append(r["temperature"])
            b["h"].append(r["humidity"])
            b["ts"] = r["timestamp"]
        series = [(round(sum(b["t"]) / len(b["t"]), 1), round(sum(b["h"]) / len(b["h"]), 1), b["ts"])
                  for _, b in sorted(buckets.items())]
    else:
        series = [(r["temperature"], r["humidity"], r["timestamp"]) for r in rows]

    return jsonify({
        "sensor_id": sensor["id"],
        "temperatures": [x[0] for x in series],
        "humidities": [x[1] for x in series],
        "timestamps": [x[2] for x in series],
        "thresholds": thresholds(sensor),
    })


@app.route("/api/export.csv")
def export_csv():
    """Descarga de lecturas en CSV.

    ?sensor=<id>|all  (por defecto el sensor 1)   ?hours=N (por defecto 24, máx. retención)
    Formato pensado para Excel en español: separador ';', coma decimal y BOM UTF-8.
    """
    sensor_param = request.args.get("sensor", str(DEFAULT_SENSOR_ID))
    if sensor_param == "all":
        selected = SENSORS
    else:
        s = SENSORS_BY_ID.get(int(sensor_param)) if sensor_param.isdigit() else None
        if s is None:
            return not_found_sensor()
        selected = [s]
    hours = max(1, min(request.args.get("hours", 24, type=int), 24 * RETENTION_DAYS))
    since = to_ts(utcnow() - timedelta(hours=hours))
    ids = [s["id"] for s in selected]

    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM sensor_readings WHERE timestamp >= ? "
        f"AND sensor_id IN ({','.join('?' * len(ids))}) ORDER BY sensor_id, timestamp",
        (since, *ids),
    ).fetchall()
    conn.close()

    def dec(v):
        return f"{v:.1f}".replace(".", ",")

    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["fecha_hora_argentina", "fecha_hora_utc", "sensor_id", "sensor", "direccion",
                "barrio", "equipo", "temperatura_c", "humedad_pct", "rango_temp_c",
                "rango_humedad_pct", "estado", "dispositivo", "tipo_lectura"])
    for r in rows:
        s = SENSORS_BY_ID[r["sensor_id"]]
        eq = equipment_of(s)
        local = from_ts(r["timestamp"]).astimezone(AR_TZ).strftime("%d/%m/%Y %H:%M:%S")
        w.writerow([local, r["timestamp"], s["id"], s["name"], s["address"], s["barrio"],
                    eq["label"], dec(r["temperature"]), dec(r["humidity"]),
                    f"{dec(eq['temp'][0])} a {dec(eq['temp'][1])}",
                    f"{eq['hum'][0]:.0f} a {eq['hum'][1]:.0f}",
                    r["status"], device_id(s["id"]), r["source"]])

    name = "todos" if sensor_param == "all" else f"supermercado_{selected[0]['id']}"
    filename = f"lecturas_{name}_{utcnow().astimezone(AR_TZ):%Y%m%d_%H%M}.csv"
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.route("/api/add-reading", methods=["POST"])
def add_reading():
    """Agrega una lectura.

    - Sin cuerpo JSON: lectura a demanda para ?sensor=<id> (botón "Leer sensor").
    - Con JSON {"sensor_id": .., "temperature": .., "humidity": ..}: lectura de un
      sensor real (ESP32, gateway LoRaWAN, etc.). Si INGEST_API_KEY está definida,
      se exige el header 'X-API-Key'.
    """
    data = request.get_json(silent=True)
    now = utcnow()

    if data:
        if INGEST_API_KEY and request.headers.get("X-API-Key") != INGEST_API_KEY:
            return jsonify({"error": "API key inválida"}), 401
        sensor = SENSORS_BY_ID.get(data.get("sensor_id", DEFAULT_SENSOR_ID))
        if sensor is None:
            return not_found_sensor()
        try:
            temperature = round(float(data["temperature"]), 1)
            humidity = round(float(data["humidity"]), 1)
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "Se requieren 'temperature' y 'humidity' numéricos"}), 400
        if not (-60 <= temperature <= 60 and 0 <= humidity <= 100):
            return jsonify({"error": "Valores fuera de rango"}), 400
        source = "dispositivo"
    else:
        sensor = requested_sensor()
        if sensor is None:
            return not_found_sensor()
        conn = get_conn()
        last = conn.execute(
            "SELECT temperature, humidity FROM sensor_readings WHERE sensor_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (sensor["id"],),
        ).fetchone()
        conn.close()
        eq = equipment_of(sensor)
        prev_t, prev_h = ((last["temperature"], last["humidity"]) if last
                          else (sum(eq["temp"]) / 2, sum(eq["hum"]) / 2))
        temperature, humidity = simulate_next(sensor, prev_t, prev_h, now)
        source = "a demanda"

    status = compute_status(sensor, temperature, humidity)
    with _db_lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO sensor_readings "
            "(sensor_id, temperature, humidity, timestamp, status, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sensor["id"], temperature, humidity, to_ts(now), status, source),
        )
        conn.commit()
        conn.close()

    return jsonify({
        "message": "Reading added successfully",
        "sensor_id": sensor["id"],
        "device_id": device_id(sensor["id"]),
        "temperature": temperature,
        "humidity": humidity,
        "timestamp": to_ts(now),
        "status": status,
        "source": source,
    }), 201


@app.route("/api/stats")
def get_stats():
    sensor = requested_sensor()
    if sensor is None:
        return not_found_sensor()
    since = to_ts(utcnow() - timedelta(hours=24))
    conn = get_conn()
    r = conn.execute(
        """
        SELECT AVG(temperature) AS t_avg, MIN(temperature) AS t_min, MAX(temperature) AS t_max,
               AVG(humidity) AS h_avg, MIN(humidity) AS h_min, MAX(humidity) AS h_max,
               COUNT(*) AS n,
               SUM(CASE WHEN status != 'Normal' THEN 1 ELSE 0 END) AS alerts,
               SUM(CASE WHEN status = 'Temperatura Alta' THEN 1 ELSE 0 END) AS high
        FROM sensor_readings WHERE sensor_id = ? AND timestamp >= ?
        """,
        (sensor["id"], since),
    ).fetchone()
    conn.close()

    def rnd(v):
        return round(v, 1) if v is not None else None

    return jsonify({
        "sensor_id": sensor["id"],
        "temperature": {
            "average": rnd(r["t_avg"]), "min": r["t_min"], "max": r["t_max"],
            "readings_count": r["n"],
        },
        "humidity": {"average": rnd(r["h_avg"]), "min": r["h_min"], "max": r["h_max"]},
        "alerts_count": r["alerts"] or 0,
        "minutes_above_range": (r["high"] or 0) * SIM_INTERVAL // 60,
        "thresholds": thresholds(sensor),
    })


# Se ejecuta al importar el módulo (también bajo gunicorn)
init_database()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")

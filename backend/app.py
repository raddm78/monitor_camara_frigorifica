"""
Monitor de Cámaras Frigoríficas - Backend (Flask + SQLite)

API REST que almacena y sirve lecturas de temperatura y humedad de sensores
instalados en cámaras frigoríficas de supermercados de CABA (Argentina).
Cada sensor tiene nombre, dirección y coordenadas para mostrarlo en un mapa.

Si no hay sensores reales conectados, el backend simula las lecturas
(caminata aleatoria alrededor del setpoint, aperturas de puerta y ciclos de
descongelamiento) para que el dashboard siempre muestre datos "vivos".

Diseñado para correr en un hosting gratuito (Render, Railway, Fly.io, etc.):
  - La simulación es "perezosa": en cada request se completan las lecturas que
    faltan desde la última guardada. Así, aunque el servicio se duerma por
    inactividad, al despertar el historial queda continuo.
  - Toda la hora se guarda en UTC (ISO 8601) y el navegador la convierte a hora local.
"""

import math
import os
import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_from_directory

# ============================================================
# CONFIGURACIÓN (todo sobreescribible por variables de entorno)
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

DATABASE = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "sensor_data.db"))
SIM_ENABLED = os.environ.get("SIMULATION_ENABLED", "true").lower() == "true"
SIM_INTERVAL = int(os.environ.get("SIM_INTERVAL_SECONDS", "60"))   # 1 lectura por minuto
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "3"))
INGEST_API_KEY = os.environ.get("INGEST_API_KEY")                  # para sensores reales

SETPOINT = -18.0          # °C objetivo de las cámaras
TEMP_HIGH = -15.0         # por encima: alerta "Temperatura Alta"
TEMP_LOW = -21.0          # por debajo: alerta "Baja Temperatura"

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# ============================================================
# SENSORES (direcciones ilustrativas dentro de CABA)
#   setpoint:  temperatura a la que tiende el equipo de frío
#   door_prob: probabilidad de apertura de puerta por lectura
# ============================================================
SENSORS = [
    {"id": 1, "name": "Supermercado 1", "address": "Av. Santa Fe 3253", "barrio": "Palermo",
     "lat": -34.5880, "lng": -58.4115, "setpoint": -18.0, "door_prob": 0.010},
    {"id": 2, "name": "Supermercado 2", "address": "Av. Las Heras 2100", "barrio": "Recoleta",
     "lat": -34.5903, "lng": -58.3967, "setpoint": -18.5, "door_prob": 0.008},
    {"id": 3, "name": "Supermercado 3", "address": "Av. Cabildo 2280", "barrio": "Belgrano",
     "lat": -34.5617, "lng": -58.4563, "setpoint": -18.0, "door_prob": 0.015},
    {"id": 4, "name": "Supermercado 4", "address": "Av. Rivadavia 5100", "barrio": "Caballito",
     "lat": -34.6196, "lng": -58.4405, "setpoint": -17.5, "door_prob": 0.012},
    {"id": 5, "name": "Supermercado 5", "address": "Defensa 900", "barrio": "San Telmo",
     "lat": -34.6195, "lng": -58.3720, "setpoint": -15.3, "door_prob": 0.020},  # compresor degradado
    {"id": 6, "name": "Supermercado 6", "address": "Av. Triunvirato 4300", "barrio": "Villa Urquiza",
     "lat": -34.5735, "lng": -58.4870, "setpoint": -18.0, "door_prob": 0.010},
    {"id": 7, "name": "Supermercado 7", "address": "Av. Rivadavia 6900", "barrio": "Flores",
     "lat": -34.6290, "lng": -58.4630, "setpoint": -19.0, "door_prob": 0.008},
    {"id": 8, "name": "Supermercado 8", "address": "Av. Corrientes 3800", "barrio": "Almagro",
     "lat": -34.6031, "lng": -58.4203, "setpoint": -18.0, "door_prob": 0.012},
]
SENSORS_BY_ID = {s["id"]: s for s in SENSORS}
DEFAULT_SENSOR_ID = SENSORS[0]["id"]

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


def compute_status(temperature):
    if temperature > TEMP_HIGH:
        return "Temperatura Alta"
    if temperature < TEMP_LOW:
        return "Baja Temperatura"
    return "Normal"


def row_to_dict(r):
    return {
        "sensor_id": r["sensor_id"],
        "temperature": r["temperature"],
        "humidity": r["humidity"],
        "timestamp": r["timestamp"],
        "status": r["status"],
        "source": r["source"],
    }


def sensor_info(s):
    return {k: s[k] for k in ("id", "name", "address", "barrio", "lat", "lng")}


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
def simulate_next(sensor, prev_temp, prev_hum, when):
    """Genera la siguiente lectura de un sensor a partir de la anterior.

    - Caminata aleatoria con reversión a la media (el equipo de frío corrige).
    - Ciclo de descongelamiento de 20 min cada 6 h (desfasado por sensor).
    - Aperturas de puerta aleatorias (picos de temperatura y humedad).
    """
    setpoint = sensor["setpoint"]
    target_t = setpoint
    target_h = 65.0

    offset = (sensor["id"] * 41) % 360          # cada sensor descongela a otra hora
    minutes_into_cycle = ((when.hour % 6) * 60 + when.minute + offset) % 360
    if minutes_into_cycle < 20:
        target_t = setpoint + 4.0 * math.sin(math.pi * minutes_into_cycle / 20)
        target_h = 75.0

    temp = prev_temp + 0.15 * (target_t - prev_temp) + random.gauss(0, 0.12)
    hum = prev_hum + 0.10 * (target_h - prev_hum) + random.gauss(0, 0.6)

    if random.random() < sensor["door_prob"]:
        temp += random.uniform(1.5, 3.5)
        hum += random.uniform(5, 12)

    temp = round(max(-30.0, min(5.0, temp)), 1)
    hum = round(max(30.0, min(100.0, hum)), 1)
    return temp, hum


def ensure_up_to_date():
    """Completa las lecturas simuladas que faltan hasta 'ahora' y purga lo viejo."""
    if not SIM_ENABLED:
        return
    now = utcnow()
    earliest_allowed = now - timedelta(hours=24)
    step = timedelta(seconds=SIM_INTERVAL)

    with _db_lock:
        conn = get_conn()
        try:
            rows = []
            for s in SENSORS:
                last = conn.execute(
                    "SELECT temperature, humidity, timestamp FROM sensor_readings "
                    "WHERE sensor_id = ? ORDER BY timestamp DESC LIMIT 1",
                    (s["id"],),
                ).fetchone()
                if last is None:
                    t, h, cursor_time = s["setpoint"], 65.0, earliest_allowed
                else:
                    t, h = last["temperature"], last["humidity"]
                    cursor_time = max(from_ts(last["timestamp"]), earliest_allowed)

                cursor_time += step
                while cursor_time <= now:
                    t, h = simulate_next(s, t, h, cursor_time)
                    rows.append((s["id"], t, h, to_ts(cursor_time), compute_status(t), "simulado"))
                    cursor_time += step

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

    # Migración desde la versión de un solo sensor: los datos eran simulados, se regeneran
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(sensor_readings)")]
    if cols and "sensor_id" not in cols:
        conn.execute("DROP TABLE sensor_readings")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensors (
            id      INTEGER PRIMARY KEY,
            name    TEXT NOT NULL,
            address TEXT NOT NULL,
            barrio  TEXT NOT NULL,
            lat     REAL NOT NULL,
            lng     REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id   INTEGER NOT NULL REFERENCES sensors(id),
            temperature REAL NOT NULL,
            humidity    REAL NOT NULL,
            timestamp   TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'Normal',
            source      TEXT NOT NULL DEFAULT 'simulado'
        );
        CREATE INDEX IF NOT EXISTS idx_readings_sensor_ts
            ON sensor_readings(sensor_id, timestamp);
        """
    )
    conn.executemany(
        "INSERT OR REPLACE INTO sensors (id, name, address, barrio, lat, lng) "
        "VALUES (:id, :name, :address, :barrio, :lat, :lng)",
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


@app.route("/api/sensors")
def get_sensors():
    """Todos los sensores con su ubicación y su última lectura (para el mapa)."""
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
    """Lecturas de las últimas N horas de un sensor, promediadas en 'points' tramos."""
    sensor = requested_sensor()
    if sensor is None:
        return not_found_sensor()
    hours = max(1, min(request.args.get("hours", 24, type=int), 24 * RETENTION_DAYS))
    points = max(10, min(request.args.get("points", 96, type=int), 1000))
    start = utcnow() - timedelta(hours=hours)

    conn = get_conn()
    rows = conn.execute(
        "SELECT temperature, humidity, timestamp FROM sensor_readings "
        "WHERE sensor_id = ? AND timestamp >= ? ORDER BY timestamp ASC",
        (sensor["id"], to_ts(start)),
    ).fetchall()
    conn.close()

    bucket_seconds = hours * 3600 / points
    buckets = {}
    for r in rows:
        idx = int((from_ts(r["timestamp"]) - start).total_seconds() // bucket_seconds)
        b = buckets.setdefault(idx, {"t": [], "h": [], "ts": r["timestamp"]})
        b["t"].append(r["temperature"])
        b["h"].append(r["humidity"])
        b["ts"] = r["timestamp"]

    ordered = [buckets[k] for k in sorted(buckets)]
    return jsonify({
        "sensor_id": sensor["id"],
        "temperatures": [round(sum(b["t"]) / len(b["t"]), 1) for b in ordered],
        "humidities": [round(sum(b["h"]) / len(b["h"]), 1) for b in ordered],
        "timestamps": [b["ts"] for b in ordered],
        "thresholds": {"high": TEMP_HIGH, "low": TEMP_LOW, "setpoint": SETPOINT},
    })


@app.route("/api/add-reading", methods=["POST"])
def add_reading():
    """Agrega una lectura.

    - Sin cuerpo JSON: lectura simulada para ?sensor=<id> (botón "Simular Lectura").
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
        source = "sensor"
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
        prev_t, prev_h = (last["temperature"], last["humidity"]) if last else (sensor["setpoint"], 65.0)
        temperature, humidity = simulate_next(sensor, prev_t, prev_h, now)
        source = "simulado"

    status = compute_status(temperature)
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
               SUM(CASE WHEN status != 'Normal' THEN 1 ELSE 0 END) AS alerts
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
        "thresholds": {"high": TEMP_HIGH, "low": TEMP_LOW, "setpoint": SETPOINT},
    })


# Se ejecuta al importar el módulo (también bajo gunicorn)
init_database()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")

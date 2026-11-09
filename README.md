# ❄️ Monitor de Cámaras Frigoríficas — Dashboard IoT

Dashboard web para monitorear en tiempo real la **temperatura y humedad** de las cámaras frigoríficas
de 8 supermercados de la Ciudad de Buenos Aires, con **mapa de sensores**.
Backend en **Python / Flask** con API REST y base **SQLite**; frontend en **HTML + CSS + JavaScript**
con **Chart.js** y **Leaflet / OpenStreetMap**.

> 🔗 **Demo en vivo:** https://monitor-camara-frigorifica.onrender.com
> *(plan gratuito: la primera carga tras un rato sin visitas puede tardar unos segundos)*

Por defecto el backend **simula los sensores** de forma realista (caminata aleatoria alrededor del setpoint de −18 °C,
ciclos de descongelamiento cada 6 h y aperturas de puerta aleatorias), así la demo siempre muestra datos vivos.
Un sensor real (ESP32, gateway LoRaWAN/Sigfox, etc.) puede enviar lecturas por HTTP al mismo endpoint.

## Funcionalidades

- Mapa de CABA con un marcador por supermercado, coloreado según su estado (normal / temperatura alta / baja).
  Al hacer clic se ve nombre, dirección, temperatura y humedad, y el dashboard pasa a mostrar ese sensor.
- Selector de sensor; el "Supermercado 5" simula un compresor degradado para mostrar alertas.
- Temperatura y humedad actuales, con alerta cuando la temperatura sale del rango (−21 °C a −15 °C).
- Gráficas de las últimas 24 h con líneas de umbral (datos promediados en 96 tramos).
- Panel de estadísticas de 24 h: promedio, mínima, máxima, cantidad de lecturas y lecturas en alerta.
- Historial de las últimas N lecturas, con origen (`simulado` / `sensor`).
- Botón "Simular Lectura" y refresco automático cada 30 s.
- Ingesta de sensores reales protegida con API key.

## Arquitectura

```
 Sensores (ESP32 / gateway) ─POST /api/add-reading──┐
                                                    ▼
 Navegador ◀──── HTML + JSON ──── Flask (gunicorn) ──── SQLite
                                     │
                                     └─ Simulador "perezoso": en cada request completa
                                        las lecturas faltantes (1/min) hasta ahora
```

La simulación no usa un hilo en segundo plano: en cada request se generan las lecturas que faltan desde la última
guardada. Así el historial queda continuo aunque el hosting gratuito duerma el servicio por inactividad.

## API

| Método | Endpoint | Descripción |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/api/health` | Health check |
| GET | `/api/sensors` | Sensores con ubicación (lat/lng, dirección) y última lectura |
| GET | `/api/current-reading?sensor=ID` | Última lectura del sensor |
| GET | `/api/history?sensor=ID&limit=N` | Últimas N lecturas (máx. 500) |
| GET | `/api/readings-range?sensor=ID&hours=24&points=96` | Serie para gráficas (promediada) |
| GET | `/api/stats?sensor=ID` | Estadísticas de las últimas 24 h |
| POST | `/api/add-reading?sensor=ID` | Sin body: lectura simulada · Con JSON: lectura de sensor real |

Si se omite `sensor`, se usa el sensor 1.

Enviar una lectura real:

```bash
curl -X POST https://<tu-servicio>.onrender.com/api/add-reading \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <INGEST_API_KEY>" \
  -d '{"sensor_id": 3, "temperature": -18.4, "humidity": 66}'
```

## Correr en local

```bash
cd backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
# abrir http://localhost:5000
```

Tests: `python -m unittest discover tests` (desde la raíz del proyecto).

## Deploy en Render (gratis)

1. Subir este proyecto a un repositorio de GitHub.
2. En [render.com](https://render.com): **New → Blueprint**, elegir el repo. Render lee `render.yaml` y crea el servicio.
3. Al terminar el build, la URL pública queda en el panel del servicio. La `INGEST_API_KEY` se genera sola
   (pestaña *Environment*).

Notas del plan gratuito: el servicio se duerme tras ~15 min sin visitas y la primera carga tarda unos segundos;
el disco es efímero, así que la base se reconstruye con 24 h simuladas en cada redeploy.

También incluye `Dockerfile` (Fly.io, Google Cloud Run, Railway) y `Procfile` (Railway / Heroku).

## Variables de entorno

| Variable | Default | Uso |
|---|---|---|
| `PORT` | `5000` | Puerto HTTP |
| `DATABASE_PATH` | `backend/sensor_data.db` | Ruta del archivo SQLite |
| `SIMULATION_ENABLED` | `true` | `false` para mostrar solo datos de sensores reales |
| `SIM_INTERVAL_SECONDS` | `60` | Intervalo entre lecturas simuladas |
| `RETENTION_DAYS` | `3` | Días de historial que se conservan |
| `INGEST_API_KEY` | *(vacío)* | Si está definida, se exige en `X-API-Key` para lecturas reales |

## Stack

Python 3 · Flask · gunicorn · SQLite · HTML5 / CSS3 / JavaScript · Chart.js · Leaflet · OpenStreetMap · Font Awesome · Render / Docker

> Las direcciones y coordenadas de los supermercados son ilustrativas.

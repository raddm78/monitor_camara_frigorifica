"""Tests de la API. Ejecutar desde la raíz:  python -m unittest discover tests"""
import importlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(cls.tmp.name, "test.db")
        os.environ["INGEST_API_KEY"] = "secreto"
        import app as app_module
        cls.mod = importlib.reload(app_module)
        cls.client = cls.mod.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_frontend_served(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Monitor de C", r.data)

    def test_sensors_in_caba(self):
        sensors = self.client.get("/api/sensors").get_json()
        self.assertEqual(len(sensors), 8)
        for s in sensors:
            self.assertTrue(s["name"].startswith("Supermercado"))
            # Caja aproximada de la Ciudad de Buenos Aires
            self.assertTrue(-34.71 < s["lat"] < -34.52, s)
            self.assertTrue(-58.54 < s["lng"] < -58.33, s)
            self.assertIsNotNone(s["last_reading"])

    def test_per_sensor_data(self):
        h3 = self.client.get("/api/history?sensor=3&limit=5").get_json()
        self.assertTrue(all(r["sensor_id"] == 3 for r in h3))
        cur = self.client.get("/api/current-reading?sensor=3").get_json()
        self.assertEqual(cur["sensor"]["name"], "Supermercado 3")
        self.assertEqual(self.client.get("/api/stats?sensor=99").status_code, 404)

    def test_seeded_last_24h(self):
        stats = self.client.get("/api/stats?sensor=2").get_json()
        # 24 h a 1 lectura cada 10 min = 144
        self.assertGreaterEqual(stats["temperature"]["readings_count"], 143)

    def test_current_reading(self):
        d = self.client.get("/api/current-reading").get_json()
        self.assertIn(d["status"], {"Normal", "Temperatura Alta", "Baja Temperatura"})
        self.assertTrue(d["timestamp"].endswith("Z"))

    def test_history_limit(self):
        self.assertEqual(len(self.client.get("/api/history?limit=20").get_json()), 20)

    def test_range_downsampled(self):
        d = self.client.get("/api/readings-range?hours=24&points=48").get_json()
        self.assertLessEqual(len(d["temperatures"]), 49)
        self.assertEqual(len(d["temperatures"]), len(d["timestamps"]))

    def test_readings_respect_equipment_ranges(self):
        for s in self.client.get("/api/sensors").get_json():
            th = s["thresholds"]
            hist = self.client.get(f"/api/history?sensor={s['id']}&limit=144").get_json()
            for r in hist:
                if r["status"] == "Normal":
                    self.assertTrue(th["temp_min"] <= r["temperature"] <= th["temp_max"], (s["name"], r))
                    self.assertTrue(th["hum_min"] <= r["humidity"] <= th["hum_max"], (s["name"], r))
                if r["status"] == "Temperatura Alta":
                    self.assertGreater(r["temperature"], th["temp_max"])

    def test_failures_happen(self):
        mod = self.mod
        s5 = mod.SENSORS_BY_ID[5]
        start = mod.utcnow()
        hits = sum(1 for i in range(6 * 24 * 7)  # una semana cada 10 min
                   if mod.failure_at(s5, start + mod.timedelta(minutes=10 * i))[0] > 0)
        self.assertGreater(hits, 0)
        t, _ = mod.simulate_next(s5, 3.0, 75.0, start)
        self.assertLessEqual(t, 30)

    def test_csv_export(self):
        r = self.client.get("/api/export.csv?sensor=2&hours=24")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        lines = r.data.decode("utf-8-sig").splitlines()
        self.assertTrue(lines[0].startswith("fecha_hora_argentina;"))
        self.assertGreaterEqual(len(lines), 144)
        self.assertTrue(all(";Supermercado 2;" in l for l in lines[1:]))
        all_lines = self.client.get("/api/export.csv?sensor=all").data.decode("utf-8-sig").splitlines()
        self.assertGreaterEqual(len(all_lines), 8 * 144)
        self.assertEqual(self.client.get("/api/export.csv?sensor=99").status_code, 404)

    def test_simulated_reading(self):
        r = self.client.post("/api/add-reading")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["source"], "a demanda")

    def test_real_sensor_requires_key(self):
        body = {"sensor_id": 4, "temperature": -12.0, "humidity": 70}
        self.assertEqual(self.client.post("/api/add-reading", json=body).status_code, 401)
        r = self.client.post("/api/add-reading", json=body, headers={"X-API-Key": "secreto"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["status"], "Temperatura Alta")
        cur = self.client.get("/api/current-reading?sensor=4").get_json()
        self.assertEqual(cur["source"], "dispositivo")
        self.assertEqual(cur["device_id"], "TH-CABA-04")

    def test_invalid_payload(self):
        r = self.client.post("/api/add-reading", json={"temperature": "x"},
                             headers={"X-API-Key": "secreto"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()

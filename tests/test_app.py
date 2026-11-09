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

    def test_seeded_last_24h(self):
        stats = self.client.get("/api/stats").get_json()
        # 24 h a 1 lectura/min ~ 1440
        self.assertGreaterEqual(stats["temperature"]["readings_count"], 1400)

    def test_current_reading(self):
        d = self.client.get("/api/current-reading").get_json()
        self.assertIn(d["status"], {"Normal", "Temperatura Alta", "Baja Temperatura"})
        self.assertTrue(d["timestamp"].endswith("Z"))

    def test_history_limit(self):
        self.assertEqual(len(self.client.get("/api/history?limit=20").get_json()), 20)

    def test_range_downsampled(self):
        d = self.client.get("/api/readings-range?hours=24&points=96").get_json()
        self.assertLessEqual(len(d["temperatures"]), 97)
        self.assertEqual(len(d["temperatures"]), len(d["timestamps"]))

    def test_simulated_reading(self):
        r = self.client.post("/api/add-reading")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["source"], "simulado")

    def test_real_sensor_requires_key(self):
        body = {"temperature": -12.0, "humidity": 70}
        self.assertEqual(self.client.post("/api/add-reading", json=body).status_code, 401)
        r = self.client.post("/api/add-reading", json=body, headers={"X-API-Key": "secreto"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["status"], "Temperatura Alta")
        cur = self.client.get("/api/current-reading").get_json()
        self.assertEqual(cur["source"], "sensor")

    def test_invalid_payload(self):
        r = self.client.post("/api/add-reading", json={"temperature": "x"},
                             headers={"X-API-Key": "secreto"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()

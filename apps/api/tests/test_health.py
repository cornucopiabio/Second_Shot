import unittest

from fastapi.testclient import TestClient

from app.main import app


class HealthEndpointTest(unittest.TestCase):
    def test_health_endpoint(self) -> None:
        client = TestClient(app)
        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "second-shot-api")

    def test_run_lifecycle(self) -> None:
        client = TestClient(app)

        resolved = client.post("/resolve-indication", json={"query": "lung fibrosis"})
        self.assertEqual(resolved.status_code, 200)
        mondo_id = "MONDO:0006374"
        for match in resolved.json()["matches"]:
            if match["mondo_id"] == "MONDO:0006374":
                mondo_id = match["mondo_id"]
                break

        created = client.post(
            "/runs", json={"mondo_id": mondo_id, "top_k": 10, "enable_docking": True}
        )
        self.assertEqual(created.status_code, 200)
        run = created.json()
        run_id = run["run_id"]
        self.assertEqual(run["status"], "partial")

        dock_payload = {"pairs": [{"drug": run["candidates"][0]["drug"], "target": run["candidates"][0]["target"]}]}
        docked = client.post(f"/runs/{run_id}/dock", json=dock_payload)
        self.assertEqual(docked.status_code, 200)
        self.assertEqual(docked.json()["status"], "completed")

        report = client.get(f"/runs/{run_id}/report")
        self.assertEqual(report.status_code, 200)
        self.assertIn("summary", report.json())

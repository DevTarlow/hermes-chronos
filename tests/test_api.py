"""Integration tests for the Desktop/dashboard API router."""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:  # FastAPI ships with the Hermes dashboard runtime, not every CLI install.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _FASTAPI_AVAILABLE = True
except Exception:  # pragma: no cover - exercised only in dependency-light installs
    FastAPI = None  # type: ignore[assignment]
    TestClient = None  # type: ignore[assignment]
    _FASTAPI_AVAILABLE = False


def _load_engine_module():
    """Load ``engine.py`` by path, the same way ``dashboard/plugin_api.py`` does."""
    name = "hermes_chronos_engine_for_api_tests"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "engine.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[name] = module
    return module


def _load_api_module():
    name = "hermes_chronos_api_for_tests"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[name] = module
    return module


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI/TestClient not installed")
class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chronos-api-")
        self.workspace = Path(self.tmp) / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.txt").write_text("v1\n", encoding="utf-8")

        self.engine_module = _load_engine_module()
        self.api_module = _load_api_module()
        self.engine_module.clear_engine_cache()

        def factory(workspace, max_checkpoints=50, ignored_patterns=None):
            return self.engine_module.ChronosEngine(
                workspace,
                max_checkpoints=max_checkpoints,
                ignored_patterns=ignored_patterns,
            )

        router = self.api_module.build_router(
            factory,
            settings_loader=lambda: {
                "auto_snapshot": True,
                "max_checkpoints": 50,
                "ignored_patterns": ["node_modules", ".git"],
                "clean_untracked_on_rollback": True,
            },
            default_cwd=lambda: str(self.workspace),
            version="1.0.0",
        )
        app = FastAPI()
        app.include_router(router, prefix="/api/plugins/hermes-chronos")
        self.client = TestClient(app)

    def tearDown(self):
        self.engine_module.clear_engine_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _timeline(self):
        response = self.client.post(
            "/api/plugins/hermes-chronos/timeline",
            json={"cwd": str(self.workspace), "limit": 10},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        return payload

    def test_timeline_health_and_diff(self):
        timeline = self._timeline()
        self.assertGreaterEqual(timeline["count"], 1)
        checkpoint = timeline["checkpoints"][0]

        health = self.client.get(
            "/api/plugins/hermes-chronos/health",
            params={"cwd": str(self.workspace)},
        )
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")

        diff = self.client.post(
            "/api/plugins/hermes-chronos/diff",
            json={"cwd": str(self.workspace), "id": checkpoint["id"]},
        )
        self.assertEqual(diff.status_code, 200)
        self.assertEqual(diff.json()["status"], "ok")

        one = self.client.get(
            f"/api/plugins/hermes-chronos/checkpoint/{checkpoint['id']}",
            params={"cwd": str(self.workspace)},
        )
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()["status"], "ok")

    def test_rollback_endpoint(self):
        first = self._timeline()["checkpoints"][0]
        (self.workspace / "app.txt").write_text("v2\n", encoding="utf-8")
        # A direct engine snapshot would return the baseline here; use the API's
        # own workspace so the second checkpoint is a real new commit.
        second = self._timeline()
        self.assertGreaterEqual(second["count"], 1)

        response = self.client.post(
            "/api/plugins/hermes-chronos/rollback",
            json={"cwd": str(self.workspace), "checkpoint_id": first["id"], "clean": True},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual((self.workspace / "app.txt").read_text(), "v1\n")
        self.assertTrue(payload.get("safety_checkpoint"))

    def test_invalid_workspace_is_a_clean_error(self):
        response = self.client.post(
            "/api/plugins/hermes-chronos/timeline",
            json={"cwd": "relative/path", "limit": 5},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("absolute", payload["error"])


if __name__ == "__main__":
    unittest.main()

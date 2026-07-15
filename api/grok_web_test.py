from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import grok_web


class GrokLegacyWebRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(grok_web.create_router())
        self.client = TestClient(app)

    def test_admin_pages_and_static_assets_are_served(self) -> None:
        response = self.client.get("/admin/account")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Grok2API", response.text)

        asset = self.client.get("/static/js/auth.js")
        self.assertEqual(asset.status_code, 200)
        self.assertIn("ADMIN_API", asset.text)

    def test_host_root_is_not_registered(self) -> None:
        paths = {getattr(route, "path", "") for route in self.client.app.routes}
        self.assertNotIn("/", paths)
        self.assertIn("/admin/account", paths)
        self.assertIn("/webui/api/models", paths)


if __name__ == "__main__":
    unittest.main()

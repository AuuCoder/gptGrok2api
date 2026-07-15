from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api import grok_admin
from app.platform.auth.middleware import verify_admin_key
from app.products.web.admin import router as upstream_admin_router


class GrokRuntimeAdminRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(grok_admin.create_router())
        self.client = TestClient(app)

    def test_verify_uses_host_admin_authorization(self) -> None:
        with patch.object(
            grok_admin,
            "require_admin",
            return_value={"id": "admin", "role": "admin"},
        ) as require_admin:
            response = self.client.get(
                "/api/grok/runtime/admin/verify",
                headers={"Authorization": "Bearer host-admin-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "success"})
        require_admin.assert_called_once_with("Bearer host-admin-token")

    def test_rejects_unauthenticated_request_before_runtime_access(self) -> None:
        with patch.object(
            grok_admin,
            "require_admin",
            side_effect=HTTPException(status_code=401, detail="host auth required"),
        ):
            response = self.client.get("/api/grok/runtime/admin/verify")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "host auth required")

    def test_legacy_grok_app_key_query_parameter_is_not_accepted(self) -> None:
        observed: list[str | None] = []

        def reject(authorization: str | None) -> dict[str, object]:
            observed.append(authorization)
            raise HTTPException(status_code=401, detail="host auth required")

        with patch.object(grok_admin, "require_admin", side_effect=reject):
            response = self.client.get(
                "/api/grok/runtime/admin/verify",
                params={"app_key": "grok-runtime-admin-key"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(observed, [None])

    def test_all_upstream_admin_routes_are_rebased_with_host_auth_only(self) -> None:
        expected = {
            (
                source_route.path.removeprefix("/admin/api") or "/",
                frozenset(source_route.methods or set()),
            )
            for source_route in upstream_admin_router.routes
            if isinstance(source_route, APIRoute)
        }
        expected = {
            (f"/api/grok/runtime/admin{path}", methods)
            for path, methods in expected
        }
        actual_routes = [
            route
            for route in self.client.app.routes
            if isinstance(route, APIRoute) and route.path.startswith("/api/grok/runtime/admin")
        ]
        actual = {(route.path, frozenset(route.methods or set())) for route in actual_routes}

        self.assertEqual(actual, expected)
        for route in actual_routes:
            dependencies = [getattr(dependency, "dependency", None) for dependency in route.dependencies]
            self.assertIn(grok_admin.require_grok_runtime_admin, dependencies)
            self.assertNotIn(verify_admin_key, dependencies)


if __name__ == "__main__":
    unittest.main()

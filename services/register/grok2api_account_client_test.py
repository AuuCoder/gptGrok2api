from __future__ import annotations

import unittest

from services.register.grok2api_account_client import (
    Grok2APIAccountClient,
    Grok2APIAccountError,
    normalize_sso,
)


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200, text: str = "") -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise RuntimeError("no fake response")
        return self.responses.pop(0)


class Grok2APIAccountClientTest(unittest.TestCase):
    def client(self, session: FakeSession) -> Grok2APIAccountClient:
        return Grok2APIAccountClient(
            {
                "grok2api_enabled": True,
                "grok2api_api_base": "http://runtime.test/",
                "grok2api_admin_key": "admin-secret",
                "grok2api_pool": "auto",
                "grok2api_auto_nsfw": True,
                "grok2api_verify_on_import": True,
                "grok2api_timeout": 45,
            },
            session=session,
        )

    def test_normalize_sso_accepts_bare_or_prefix_and_rejects_cookie_string(self) -> None:
        self.assertEqual(normalize_sso(" bare-token "), "bare-token")
        self.assertEqual(normalize_sso("sso=prefix-token"), "prefix-token")
        with self.assertRaisesRegex(ValueError, "完整 Cookie"):
            normalize_sso("sso=token; sso-rw=token")
        with self.assertRaisesRegex(ValueError, "完整 Cookie"):
            normalize_sso("Cookie: sso=token")

    def test_methods_use_admin_api_contract_and_bare_tokens(self) -> None:
        session = FakeSession(
            [
                FakeResponse({"tokens": []}),
                FakeResponse({"status": "success", "count": 1, "skipped": 0}),
                FakeResponse({"summary": {"total": 1, "ok": 1, "fail": 0}}),
                FakeResponse({"results": [{"token": "token-one", "status": "valid", "quota": {"remaining": 2, "total": 5}}]}),
                FakeResponse({"summary": {"total": 1, "ok": 1, "fail": 0}}),
                FakeResponse({"deleted": 1}),
            ]
        )
        client = self.client(session)

        client.list()
        client.add(["sso=token-one", "token-one"], tags=["nsfw", "nsfw"])
        client.refresh(["token-one"])
        client.verify(["token-one"])
        client.set_disabled(["token-one"], True)
        client.delete(["token-one"])

        self.assertEqual([call["url"] for call in session.calls], [
            "http://runtime.test/admin/api/tokens",
            "http://runtime.test/admin/api/tokens/add",
            "http://runtime.test/admin/api/batch/refresh",
            "http://runtime.test/admin/api/tokens/verify",
            "http://runtime.test/admin/api/tokens/disabled/batch",
            "http://runtime.test/admin/api/tokens",
        ])
        self.assertEqual(session.calls[1]["params"], {"auto_nsfw": "true"})
        self.assertEqual(session.calls[1]["json"], {"tokens": ["token-one"], "pool": "auto", "tags": ["nsfw"]})
        self.assertEqual(session.calls[3]["json"], {"tokens": ["token-one"]})
        self.assertEqual(session.calls[4]["json"], {"tokens": ["token-one"], "disabled": True})
        self.assertEqual(session.calls[5]["json"], ["token-one"])
        self.assertTrue(all(call["headers"]["Authorization"] == "Bearer admin-secret" for call in session.calls))

    def test_http_error_redacts_admin_key_and_token(self) -> None:
        secret_token = "very-secret-token"
        session = FakeSession(
            [FakeResponse({"detail": f"bad {secret_token} admin-secret"}, status_code=400)]
        )
        client = self.client(session)

        with self.assertRaises(Grok2APIAccountError) as raised:
            client.refresh([secret_token])

        message = str(raised.exception)
        self.assertIn("HTTP 400", message)
        self.assertNotIn(secret_token, message)
        self.assertNotIn("admin-secret", message)

    def test_disabled_or_incomplete_config_never_sends_request(self) -> None:
        session = FakeSession([FakeResponse({})])
        client = Grok2APIAccountClient({}, session=session)

        with self.assertRaisesRegex(Grok2APIAccountError, "disabled"):
            client.list()

        self.assertEqual(session.calls, [])

    def test_api_base_accepts_existing_admin_api_prefix(self) -> None:
        session = FakeSession([FakeResponse({"tokens": []})])
        client = Grok2APIAccountClient(
            {
                "grok2api_enabled": True,
                "grok2api_api_base": "http://grok2api:8000/admin/api/",
                "grok2api_admin_key": "key",
            },
            session=session,
        )

        client.list()

        self.assertEqual(session.calls[0]["url"], "http://grok2api:8000/admin/api/tokens")

    def test_default_session_disables_environment_proxy_and_unknown_pool_falls_back(self) -> None:
        client = Grok2APIAccountClient(
            {
                "grok2api_enabled": True,
                "grok2api_api_base": "http://runtime.test",
                "grok2api_admin_key": "key",
                "grok2api_pool": "unknown-pool",
            }
        )
        try:
            self.assertFalse(client._session.trust_env)
            self.assertEqual(client.pool, "auto")
        finally:
            client._session.close()


if __name__ == "__main__":
    unittest.main()

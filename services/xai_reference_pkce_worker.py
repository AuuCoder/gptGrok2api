"""Run the reference Grok PKCE implementation in its own Python environment."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


RESULT_PREFIX = "XAI_PKCE_RESULT="


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference-dir", required=True)
    args = parser.parse_args()

    reference_dir = Path(args.reference_dir).expanduser().resolve()
    if not (reference_dir / "xconsole_client" / "oauth_protocol.py").is_file():
        raise RuntimeError("reference directory does not contain xconsole_client/oauth_protocol.py")

    payload = json.load(sys.stdin)
    sys.path.insert(0, str(reference_dir))
    from curl_cffi.requests.impersonate import BrowserType
    from xconsole_client.oauth_protocol import ProtocolOAuthClient

    sso = str(payload.get("sso") or "").strip()
    supported = {item.value for item in BrowserType}
    impersonate = "chrome146" if "chrome146" in supported else "chrome136"
    client = ProtocolOAuthClient(
        proxy=str(payload.get("proxy") or "").strip(),
        impersonate=impersonate,
        debug=False,
    )
    with tempfile.TemporaryDirectory(prefix="xai-pkce-auth-") as auth_dir:
        result = client.login(
            str(payload.get("email") or "").strip(),
            str(payload.get("password") or "").strip(),
            proxy=str(payload.get("proxy") or "").strip(),
            cliproxyapi_auth_dir=auth_dir,
            output_dir=None,
            session_cookies={"sso": sso, "sso-rw": sso} if sso else None,
        )

    token = result.token if isinstance(result.token, dict) else {}
    output = {
        "access_token": str(token.get("access_token") or ""),
        "refresh_token": str(token.get("refresh_token") or ""),
        "id_token": str(token.get("id_token") or ""),
        "expires_in": int(token.get("expires_in") or 21_600),
        "token_type": str(token.get("token_type") or "Bearer"),
    }
    print(f"{RESULT_PREFIX}{json.dumps(output, ensure_ascii=True, separators=(',', ':'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

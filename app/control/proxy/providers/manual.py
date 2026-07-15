"""Manual clearance provider — uses operator-supplied cookies directly."""

from app.platform.config.snapshot import get_config
from ..config import resolve_clearance_config
from ..models import ClearanceBundle, ClearanceMode


class ManualClearanceProvider:
    """Build a ClearanceBundle from static config values."""

    def build_bundle(
        self,
        *,
        affinity_key: str,
        clearance_host: str = "grok.com",
    ) -> ClearanceBundle | None:
        cfg = get_config()
        mode = ClearanceMode.parse(cfg.get_str("proxy.clearance.mode", "none"))
        if mode != ClearanceMode.MANUAL:
            return None
        clearance = resolve_clearance_config(cfg)
        return ClearanceBundle(
            bundle_id=f"manual:{affinity_key}@{clearance_host}",
            cf_cookies=clearance.cf_cookies,
            user_agent=clearance.user_agent,
            affinity_key=affinity_key,
            clearance_host=clearance_host,
        )


__all__ = ["ManualClearanceProvider"]

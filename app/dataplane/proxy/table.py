"""Proxy dataplane table — runtime view of egress nodes and clearance bundles.

Thin wrapper around ProxyDirectory's internal state, formalizing the
dataplane/control-plane boundary.  The control-plane ProxyDirectory owns
mutation; this module provides a read-only snapshot for selector logic.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.control.proxy.models import (
    EgressNode, ClearanceBundle, EgressMode, ClearanceMode,
)

if TYPE_CHECKING:
    from app.control.proxy import ProxyDirectory

BundleKey = tuple[str, str]


@dataclass
class ProxyRuntimeTable:
    """Read-only snapshot of proxy state for dataplane selection."""

    egress_mode:    EgressMode    = EgressMode.DIRECT
    clearance_mode: ClearanceMode = ClearanceMode.NONE
    nodes:          list[EgressNode]                  = field(default_factory=list)
    bundles:        dict[BundleKey, ClearanceBundle]  = field(default_factory=dict)

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def has_nodes(self) -> bool:
        return len(self.nodes) > 0

    def healthy_nodes(self) -> list[EgressNode]:
        from app.control.proxy.models import EgressNodeState
        return [n for n in self.nodes if n.state == EgressNodeState.HEALTHY]


def snapshot_from_directory(directory: "ProxyDirectory") -> ProxyRuntimeTable:
    """Build a ProxyRuntimeTable from the current ProxyDirectory state.

    This is a point-in-time snapshot — not kept in sync automatically.
    """
    return ProxyRuntimeTable(
        egress_mode=directory.egress_mode,
        clearance_mode=directory.clearance_mode,
        nodes=directory.nodes,
        bundles=directory.bundles,
    )


__all__ = ["ProxyRuntimeTable", "snapshot_from_directory"]

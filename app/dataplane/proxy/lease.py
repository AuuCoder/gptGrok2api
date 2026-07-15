"""Proxy dataplane lease — re-export from control plane.

ProxyLease is defined in ``app.control.proxy.models`` and used throughout
both control and dataplane layers.  This module provides a canonical import
path within the dataplane package.
"""

from app.control.proxy.models import ProxyLease

__all__ = ["ProxyLease"]

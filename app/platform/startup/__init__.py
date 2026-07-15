"""Startup utilities — migration, seeding, first-boot checks."""
from .migration import run_startup_migrations

__all__ = ["run_startup_migrations"]

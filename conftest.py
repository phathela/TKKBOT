import os
import sys

# Ensure the project root is importable regardless of how pytest is invoked.
sys.path.insert(0, os.path.dirname(__file__))

from app.config import Settings


def make_settings(**overrides):
    """Build a Settings instance with known values, isolated from any .env file."""
    base = {
        "webhook_secret": "test-secret",
        "bybit_api_key": "test-key",
        "bybit_api_secret": "test-secret-key",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)

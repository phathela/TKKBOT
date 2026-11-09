import os
import sys

# Ensure the project root is importable regardless of how pytest is invoked.
sys.path.insert(0, os.path.dirname(__file__))

from app.config import Settings
from scanner.config import ScanSettings


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


def make_scan_settings(**overrides):
    """ScanSettings with every key present, isolated from any .env file."""
    base = {
        "deepseek_api_key": "test-deepseek",
        "coingecko_api_key": "test-coingecko",
        "cmc_api_key": "test-cmc",
        "twelvedata_api_key": "test-twelve",
        "opportunities_api_key": "test-api-key",
        "_env_file": None,
    }
    base.update(overrides)
    return ScanSettings(**base)

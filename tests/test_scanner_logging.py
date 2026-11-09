"""No credential may reach stdout through a library's request log."""
import logging

import pytest

from app.logger import NOISY_HTTP_LOGGERS, silence_http_clients
from app.logger import setup_logging as setup_app_logging
from scanner.run import _setup_logging as setup_scanner_logging


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """``setup_logging`` rebuilds the root handlers (force=True); put them back."""
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    levels = {name: logging.getLogger(name).level for name in NOISY_HTTP_LOGGERS}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, saved in levels.items():
        logging.getLogger(name).setLevel(saved)


@pytest.mark.parametrize("setup", [setup_app_logging, setup_scanner_logging])
def test_http_client_loggers_are_quieted(setup):
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

    setup("INFO")

    for name in NOISY_HTTP_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_silence_http_clients_is_idempotent():
    silence_http_clients()
    silence_http_clients()
    assert logging.getLogger("httpx").level == logging.WARNING

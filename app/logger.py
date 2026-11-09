"""Logging setup for TKKBOT."""
import logging
import sys

# httpx logs every request URL at INFO — and several market-data providers take
# their API key as a query parameter (Twelve Data's ``apikey``, NewsAPI's
# ``apiKey``, CryptoPanic's ``auth_token``, Finnhub's legacy ``token``). Left at
# INFO, those keys are written to stdout and stored by Railway. Keep these
# loggers at WARNING so a URL can never carry a credential into the logs.
NOISY_HTTP_LOGGERS = ("httpx", "httpcore")


def silence_http_clients() -> None:
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )
    silence_http_clients()

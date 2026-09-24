import io
import logging

from logsafe import RedactingFormatter, redact, secret_fragments

URL = "https://eth-mainnet.g.alchemy.com/v2/AbCdEf123456iXQi5v8KnVX_7qVoYZa"
WS = "wss://mainnet.infura.io/ws/v3/0123456789abcdef0123456789abcdef"
TG = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def test_fragments_and_redact():
    secrets = secret_fragments(URL, WS, TG, None)
    msg = (f"ClientResponseError: 400, url='{URL}' | ws {WS} | "
           f"https://api.telegram.org/bot{TG}/sendMessage | key alone AbCdEf123456iXQi5v8KnVX_7qVoYZa")
    out = redact(msg, secrets)
    assert "AbCdEf123456iXQi5v8KnVX_7qVoYZa" not in out
    assert "0123456789abcdef0123456789abcdef" not in out
    assert TG not in out and "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in out
    # безобидные части URL остаются, чтобы лог был понятен
    assert "eth-mainnet.g.alchemy.com" not in secrets and "v2" not in secrets


def test_formatter_redacts_tracebacks():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s", None, secret_fragments(URL)))
    logger = logging.getLogger("test_logsafe")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        raise ConnectionError(f"cannot reach {URL}")
    except ConnectionError:
        logger.exception("RPC %s упал", URL)
    text = stream.getvalue()
    assert "iXQi5v8KnVX" not in text and "***" in text and "Traceback" in text

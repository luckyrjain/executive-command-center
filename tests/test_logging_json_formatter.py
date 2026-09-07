"""``ecc.logging.JsonFormatter`` (round 3 architecture review): a
``logger.exception()``/``logger.warning(..., exc_info=True)`` call computes
a traceback onto the ``LogRecord``, but the formatter silently dropped it --
every other field still reached stdout, so a failure looked "logged" while
its actual cause was unrecoverable from logs alone. No test previously
covered this formatter at all.
"""

from __future__ import annotations

import json
import logging
import sys

from ecc.logging import JsonFormatter

_LOGGER = logging.getLogger("ecc.logging.tests")


def _make_record(*, exc_info: bool) -> logging.LogRecord:
    if exc_info:
        try:
            raise ValueError("boom")
        except ValueError:
            return _LOGGER.makeRecord(
                _LOGGER.name, logging.ERROR, __name__, 1, "failed", (), sys.exc_info()
            )
    return _LOGGER.makeRecord(_LOGGER.name, logging.INFO, __name__, 1, "ok", (), None)


def test_format_omits_exception_key_when_no_exc_info() -> None:
    payload = json.loads(JsonFormatter().format(_make_record(exc_info=False)))
    assert "exception" not in payload
    assert payload["message"] == "ok"


def test_format_includes_traceback_when_exc_info_present() -> None:
    payload = json.loads(JsonFormatter().format(_make_record(exc_info=True)))
    assert "exception" in payload
    assert "ValueError: boom" in payload["exception"]
    assert "Traceback" in payload["exception"]
    assert payload["message"] == "failed"


def test_format_is_valid_single_line_json_even_with_a_multiline_traceback() -> None:
    line = JsonFormatter().format(_make_record(exc_info=True))
    assert "\n" not in line
    payload = json.loads(line)
    assert "\\n" in json.dumps(payload["exception"])

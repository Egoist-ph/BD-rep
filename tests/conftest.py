from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bdrep.config import load_config
from bdrep.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 10, 10)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "leads.db")
    yield s
    s.close()


@pytest.fixture
def sam_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "sam_search.json").read_text())


@pytest.fixture
def news_xml() -> str:
    return (FIXTURES / "google_news.xml").read_text()


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = "", url: str = "https://example.test/x"):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Returns queued responses in order and records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        resp = self.responses.pop(0) if self.responses else FakeResponse(200, {"opportunitiesData": [], "totalRecords": 0})
        if isinstance(resp, Exception):
            raise resp
        query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        resp.url = f"{url}?{query}" if query else url
        return resp


class FakeAnthropic:
    """Minimal stand-in for anthropic.Anthropic().beta.messages.create."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, SimpleNamespace):
            return out
        return SimpleNamespace(
            stop_reason="end_turn",
            model=kwargs["model"],
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(out))],
        )

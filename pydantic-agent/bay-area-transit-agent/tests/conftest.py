"""Shared test fakes: an in-memory stand-in for ``uagents.Context``.

These let the payment/chat handlers run without a live agent, network, Stripe,
or Agentverse mailbox. HTTP/Stripe/LLM boundaries are monkeypatched per test.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeStorage:
    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        return self._d.get(key)

    def set(self, key: str, value: Any) -> None:
        self._d[key] = value

    def clear(self) -> None:
        self._d.clear()


class _FakeLogger:
    def info(self, *a: Any, **k: Any) -> None: ...
    def debug(self, *a: Any, **k: Any) -> None: ...
    def warning(self, *a: Any, **k: Any) -> None: ...
    def error(self, *a: Any, **k: Any) -> None: ...
    def exception(self, *a: Any, **k: Any) -> None: ...


class _FakeAgent:
    address = "agent1qtestselleraddressxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class FakeContext:
    """Captures everything sent via ``ctx.send`` in ``self.sent``."""

    def __init__(self, session: str = "window-1") -> None:
        self.storage = FakeStorage()
        self.agent = _FakeAgent()
        self.session = session
        self.logger = _FakeLogger()
        self.sent: list[tuple[str, Any]] = []

    async def send(self, destination: str, message: Any) -> None:
        self.sent.append((destination, message))

    def messages_of(self, cls: type) -> list[Any]:
        return [m for _, m in self.sent if isinstance(m, cls)]


@pytest.fixture
def ctx() -> FakeContext:
    return FakeContext()


@pytest.fixture
def sender() -> str:
    return "agent1qtestbuyeraddressyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"

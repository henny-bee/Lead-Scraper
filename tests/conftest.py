"""Shared pytest configuration. Async tests use the anyio plugin (no pytest-asyncio)."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"

"""Fixture-benchmark regression in the default suite: counts and precision only, no timing."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmark"))
import scenario as bench  # noqa: E402  (the benchmark harness, importlib import mode)

pytestmark = pytest.mark.anyio


async def test_s1_count_and_precision(tmp_path: Path, search_mock) -> None:
    run = await bench.run_once(bench.load_scenario("S1"), "default", tmp_path)
    assert run["count"] == 20, run
    assert run["email_precision"] >= 0.95 and run["website_precision"] == 1.0, run


async def test_s2_no_decoys(tmp_path: Path, search_mock) -> None:
    run = await bench.run_once(bench.load_scenario("S2"), "default", tmp_path)
    assert run["decoys_accepted"] == 0 and run["email_precision"] == 1.0, run
    assert run["duplicate_records"] == 0, run


async def test_s4_count(tmp_path: Path, search_mock) -> None:
    run = await bench.run_once(bench.load_scenario("S4"), "default", tmp_path)
    assert run["count"] == 100, run

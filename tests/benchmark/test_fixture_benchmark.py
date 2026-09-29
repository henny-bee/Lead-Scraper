"""Deterministic fixture benchmark."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenario as bench  # noqa: E402  (sibling helper module, importlib import mode)

pytestmark = [pytest.mark.benchmark, pytest.mark.anyio]

CASES = [(name, config) for name in ("S1", "S2", "S3", "S4") for config in ("default", "off")]
CASES.append(("S2", "searxng"))                    # the backend switch, S2 only
BASELINE = json.loads((bench.FIXTURES / "baseline.json").read_text(encoding="utf-8"))["fixture"]


def check_gates(name: str, config: str, out: dict) -> None:
    """Acceptance criteria (deterministic, fixture)."""
    base = BASELINE[name]["default" if config == "searxng" else config]
    if name == "S1" and config == "default":
        assert out["count"] == 20, out
        assert out["email_precision"] >= 0.95 and out["website_precision"] == 1.0, out
        assert out["wall_s"] <= 1.0 * base["wall_s"], (out["wall_s"], base["wall_s"])
        if base["time_to_10_s"] is not None:          # the baseline never reached 10 → recorded only
            assert out["time_to_10_s"] <= 0.7 * base["time_to_10_s"], (out["time_to_10_s"], base)
        assert out["time_to_target_s"] is not None, out
        assert out["email_yield"] >= base["email_yield"], (out["email_yield"], base["email_yield"])
    if name == "S4" and config == "default":
        assert out["count"] == 100 and base["count"] == 100, out
        assert out["wall_s"] <= 0.5 * base["wall_s"], (out["wall_s"], base["wall_s"])
        assert out["time_to_target_s"] <= 0.5 * base["time_to_target_s"], (out["time_to_target_s"], base)
    if name == "S2":
        assert out["correct"] >= 0.9 * out["recoverable"], out
        assert out["decoys_accepted"] == 0 and out["email_precision"] == 1.0, out
        assert out["duplicate_records"] == 0, out              # the discovery SERP's merge cases
        web = config != "off"
        for by_source in out["by_source_runs"]:
            assert (by_source.get("search", 0) > 0) is web, by_source
            assert (by_source.get("web_discovery", 0) > 0) is web, by_source
    if name == "S3":
        assert out["discovery_wall_s"] <= 0.6 * base["discovery_wall_s"], (out["discovery_wall_s"], base)
        assert out["max_overpass_in_flight"] <= 2, out        # one (custom) endpoint in the harness


@pytest.mark.parametrize("name,config", CASES)
async def test_fixture_benchmark(name: str, config: str, tmp_path: Path) -> None:
    sc = bench.load_scenario(name)
    runs = []
    for i in range(bench.RUNS):
        run = await bench.run_once(sc, config, tmp_path / f"run{i}")
        bench.assert_politeness(run, sc)
        runs.append(run)
    out = bench.summarize(runs)
    base = BASELINE[name]["default" if config == "searxng" else config]
    out["vs_baseline"] = {k: round(out[k] / base[k], 3) for k in ("wall_s", "time_to_target_s", "correct")
                          if out.get(k) and base.get(k)}          # ratio new / baseline
    print("\n" + bench.bench_line(out))
    check_gates(name, config, out)

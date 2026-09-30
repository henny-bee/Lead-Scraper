import ast
import random
from pathlib import Path

import pytest

from leadscraper.domain.tiling import BBox, sweep

pytestmark = pytest.mark.anyio

BAYERN = BBox(47.27, 8.97, 50.56, 13.84)    # area roughly the size of Bayern
CAP = 60


def make_points(seed: int = 7) -> list[tuple[float, float]]:
    """6 000 points: three dense city clusters + rural scatter."""
    rnd = random.Random(seed)
    points: set[tuple[float, float]] = set()
    for lat, lon, n in ((48.14, 11.58, 1800), (49.45, 11.08, 1500), (48.37, 10.90, 1200)):
        target = len(points) + n
        while len(points) < target:
            p = (round(rnd.gauss(lat, 0.08), 6), round(rnd.gauss(lon, 0.12), 6))
            if BAYERN.south <= p[0] <= BAYERN.north and BAYERN.west <= p[1] <= BAYERN.east:
                points.add(p)
    while len(points) < 6000:
        points.add((round(rnd.uniform(BAYERN.south, BAYERN.north), 6),
                    round(rnd.uniform(BAYERN.west, BAYERN.east), 6)))
    return sorted(points)


def capped_search(points, cap, log):
    async def search(cell: BBox) -> list:
        log.append(cell)
        hits = [p for p in points
                if cell.south <= p[0] <= cell.north and cell.west <= p[1] <= cell.east]
        return hits[:cap]                          # source truncates at `cap`
    return search


async def test_single_query_is_truncated() -> None:
    points = make_points()
    assert len(points) == 6000
    log: list[BBox] = []
    assert len(await capped_search(points, CAP, log)(BAYERN)) == CAP


async def test_sweep_finds_all_points() -> None:
    points = make_points()
    log: list[BBox] = []
    found = {p async for p in sweep(BAYERN, capped_search(points, CAP, log), cap=CAP)}
    assert found == set(points)                     # 100 % recall
    assert 1 < len(log) < 2000                      # far fewer queries than points


async def test_sweep_stops_at_min_km() -> None:
    same_spot = [(48.0, 11.0)] * 500                # impossible to separate spatially
    log: list[BBox] = []
    results = [p async for p in sweep(BAYERN, capped_search(same_spot, CAP, log), cap=CAP,
                                      min_km=2.0)]
    assert results and len(log) < 200              # terminates
    saturated = [c for c in log if c.south <= 48.0 <= c.north and c.west <= 11.0 <= c.east]
    assert min(c.size_km() for c in saturated) <= 2.0      # split down to min_km, then stop
    assert all(c.size_km() > 2.0 / 2 for c in log)  # never split below min_km


async def test_inside_filter_skips_cells() -> None:
    points = make_points()
    log: list[BBox] = []
    west_half = lambda b: b.west < 11.0             # noqa: E731
    found = {p async for p in sweep(BAYERN, capped_search(points, CAP, log), cap=CAP,
                                    inside=west_half)}
    assert all(c.west < 11.0 for c in log)
    assert found and all(p[1] <= BAYERN.east for p in found)


def test_bbox_split_and_size() -> None:
    parts = BBox(0, 0, 2, 2).split()
    assert parts == [BBox(0, 0, 1, 1), BBox(0, 1, 1, 2), BBox(1, 0, 2, 1), BBox(1, 1, 2, 2)]
    assert BBox(0, 0, 1, 1).size_km() == pytest.approx(111.32)
    assert BBox(60, 0, 60.1, 2).size_km() == pytest.approx(2 * 111.32 * 0.5, rel=0.01)


ALLOWED_DOMAIN_IMPORTS = {"__future__", "dataclasses", "enum", "math", "collections.abc", "typing"}


def test_domain_does_no_io_imports() -> None:
    """Domain/ does no I/O — only pure stdlib modules and other domain modules."""
    domain = Path(__file__).resolve().parents[2] / "src" / "leadscraper" / "domain"
    files = sorted(domain.glob("*.py"))
    assert {f.name for f in files} >= {"models.py", "quota.py", "tiling.py"}
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name in ALLOWED_DOMAIN_IMPORTS or name.startswith("leadscraper.domain"), \
                    f"{f.name} imports {name}"

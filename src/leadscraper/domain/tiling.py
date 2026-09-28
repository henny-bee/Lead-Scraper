# src/leadscraper/domain/tiling.py
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BBox:
    south: float
    west: float
    north: float
    east: float

    def split(self) -> list["BBox"]:
        lat, lon = (self.south + self.north) / 2, (self.west + self.east) / 2
        return [BBox(self.south, self.west, lat, lon), BBox(self.south, lon, lat, self.east),
                BBox(lat, self.west, self.north, lon), BBox(lat, lon, self.north, self.east)]

    def size_km(self) -> float:
        mid = math.radians((self.south + self.north) / 2)
        return max((self.north - self.south) * 111.32,
                   (self.east - self.west) * 111.32 * math.cos(mid))


async def sweep(root: BBox, search: Callable[[BBox], Awaitable[list]], *, cap: int,
                min_km: float = 2.0, inside: Callable[[BBox], bool] = lambda b: True) -> AsyncIterator:
    """Query one cell; if the result is full (== cap) it may be truncated -> split into 4."""
    stack = [root]
    while stack:
        cell = stack.pop()
        if not inside(cell):                      # cell outside the area polygon -> skip
            continue
        results = await search(cell)
        for r in results:
            yield r                               # duplicates between cells are dropped in the dedup stage
        if len(results) >= cap and cell.size_km() > min_km:
            stack.extend(cell.split())

# src/leadscraper/domain/quota.py
def allocate(total: int, capacity: dict[str, int]) -> dict[str, int]:
    """Water-filling: split `total` evenly across all slices; slices whose capacity runs out are
    'locked', and their remaining quota is redistributed to the slices that are still active."""
    alloc = dict.fromkeys(capacity, 0)
    active = {k for k, cap in capacity.items() if cap > 0}
    remaining = total
    while remaining > 0 and active:
        share = max(1, remaining // len(active))
        for key in sorted(active):
            give = min(share, capacity[key] - alloc[key], remaining)
            alloc[key] += give
            remaining -= give
            if alloc[key] >= capacity[key]:
                active.discard(key)
            if remaining == 0:
                break
    return alloc

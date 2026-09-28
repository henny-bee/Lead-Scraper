from leadscraper.domain.quota import allocate


def test_architecture_example() -> None:
    assert allocate(1000, {"BY:maschinenbau": 100, "BY:logistik": 5000,
                           "NW:logistik": 5000, "HE:grosshandel": 30}) == {
        "BY:maschinenbau": 100, "BY:logistik": 435, "NW:logistik": 435, "HE:grosshandel": 30}


def test_even_split_when_capacity_ample() -> None:
    alloc = allocate(12, {f"s{i}": 100 for i in range(12)})
    assert set(alloc.values()) == {1} and sum(alloc.values()) == 12


def test_total_exceeds_capacity() -> None:
    assert allocate(1000, {"a": 10, "b": 20}) == {"a": 10, "b": 20}


def test_zero_capacity_and_zero_total() -> None:
    assert allocate(10, {"a": 0, "b": 5}) == {"a": 0, "b": 5}
    assert allocate(0, {"a": 5}) == {"a": 0}
    assert allocate(5, {}) == {}


def test_remainder_distributed_and_sum_exact() -> None:
    alloc = allocate(10, {"a": 100, "b": 100, "c": 100})
    assert sum(alloc.values()) == 10 and max(alloc.values()) - min(alloc.values()) <= 1


def test_reallocation_after_slice_exhausted() -> None:
    """Planner re-calls allocate for the remaining quota when a slice runs dry (A§3.5)."""
    first = allocate(300, {"a": 1000, "b": 1000, "c": 1000})
    assert first == {"a": 100, "b": 100, "c": 100}
    # slice "a" produced only 40 -> remaining 60 goes to b and c
    second = allocate(60, {"b": 1000, "c": 1000})
    assert second == {"b": 30, "c": 30}

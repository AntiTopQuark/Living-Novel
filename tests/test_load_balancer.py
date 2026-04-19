from __future__ import annotations

import time

from common.llm.config import BaseURLConfig
from common.llm.load_balancer import WeightedEndpointPool


def test_weighted_round_robin_distribution() -> None:
    pool = WeightedEndpointPool(
        [
            BaseURLConfig(id="a", url="https://a", weight=3),
            BaseURLConfig(id="b", url="https://b", weight=1),
        ],
        failure_threshold=3,
        recovery_seconds=1.0,
    )

    counts = {"a": 0, "b": 0}
    for _ in range(80):
        endpoint = pool.select_endpoint()
        counts[endpoint.id] += 1
        pool.record_success(endpoint.id)

    ratio = counts["a"] / counts["b"]
    assert 2.5 <= ratio <= 3.5


def test_circuit_breaker_recovery() -> None:
    pool = WeightedEndpointPool(
        [
            BaseURLConfig(id="a", url="https://a", weight=1),
            BaseURLConfig(id="b", url="https://b", weight=1),
        ],
        failure_threshold=1,
        recovery_seconds=0.05,
    )

    first = pool.select_endpoint()
    pool.record_failure(first.id, "boom")

    second = pool.select_endpoint()
    assert second.id != first.id

    # Wait for half-open probing window.
    time.sleep(0.06)

    third = pool.select_endpoint()
    assert third.id == first.id
    pool.record_success(third.id)

    snapshot = pool.debug_snapshot()
    assert snapshot[first.id]["status"] == "closed"

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .config import BaseURLConfig
from .errors import EndpointSelectionError


@dataclass(slots=True)
class EndpointRuntimeState:
    id: str
    url: str
    weight: int
    status: str = "closed"  # closed | open | half-open
    consecutive_failures: int = 0
    open_until: float = 0.0
    half_open_trial_in_progress: bool = False
    last_error: str | None = None


class WeightedEndpointPool:
    def __init__(
        self,
        endpoints: list[BaseURLConfig],
        *,
        failure_threshold: int,
        recovery_seconds: float,
    ) -> None:
        if not endpoints:
            raise ValueError("At least one endpoint is required")

        self._failure_threshold = failure_threshold
        self._recovery_seconds = recovery_seconds
        self._states = {
            endpoint.id: EndpointRuntimeState(
                id=endpoint.id,
                url=endpoint.url,
                weight=endpoint.weight,
            )
            for endpoint in endpoints
        }
        self._weighted_ids = [
            endpoint.id for endpoint in endpoints for _ in range(endpoint.weight)
        ]
        self._cursor = 0
        self._lock = threading.Lock()

    def _refresh_open_states(self, now: float) -> None:
        for state in self._states.values():
            if state.status == "open" and now >= state.open_until:
                state.status = "half-open"
                state.half_open_trial_in_progress = False

    def select_endpoint(self, exclude_ids: set[str] | None = None) -> EndpointRuntimeState:
        exclude_ids = exclude_ids or set()
        with self._lock:
            now = time.monotonic()
            self._refresh_open_states(now)

            def _pick(excluded: set[str]) -> EndpointRuntimeState | None:
                total_slots = len(self._weighted_ids)
                for _ in range(total_slots):
                    endpoint_id = self._weighted_ids[self._cursor]
                    self._cursor = (self._cursor + 1) % total_slots

                    state = self._states[endpoint_id]
                    if state.id in excluded:
                        continue
                    if state.status == "closed":
                        return state
                    if state.status == "half-open" and not state.half_open_trial_in_progress:
                        state.half_open_trial_in_progress = True
                        return state
                return None

            selected = _pick(exclude_ids)
            if selected is not None:
                return selected
            if exclude_ids:
                selected = _pick(set())
                if selected is not None:
                    return selected

            raise EndpointSelectionError("No available endpoints. All circuit breakers are open.")

    def record_success(self, endpoint_id: str) -> None:
        with self._lock:
            state = self._states[endpoint_id]
            state.status = "closed"
            state.consecutive_failures = 0
            state.half_open_trial_in_progress = False
            state.last_error = None

    def record_failure(self, endpoint_id: str, error: str) -> None:
        with self._lock:
            now = time.monotonic()
            state = self._states[endpoint_id]
            state.last_error = error

            if state.status == "half-open":
                self._trip_open(state, now)
                return

            state.consecutive_failures += 1
            state.half_open_trial_in_progress = False
            if state.consecutive_failures >= self._failure_threshold:
                self._trip_open(state, now)

    def _trip_open(self, state: EndpointRuntimeState, now: float) -> None:
        state.status = "open"
        state.consecutive_failures = 0
        state.open_until = now + self._recovery_seconds
        state.half_open_trial_in_progress = False

    def debug_snapshot(self) -> dict[str, dict[str, str | int | float | bool | None]]:
        with self._lock:
            return {
                endpoint_id: {
                    "status": state.status,
                    "consecutive_failures": state.consecutive_failures,
                    "open_until": state.open_until,
                    "half_open_trial_in_progress": state.half_open_trial_in_progress,
                    "last_error": state.last_error,
                }
                for endpoint_id, state in self._states.items()
            }

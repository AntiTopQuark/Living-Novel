from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError


@dataclass(slots=True)
class BaseURLConfig:
    id: str
    url: str
    weight: int = 1


@dataclass(slots=True)
class CircuitBreakerConfig:
    failure_threshold: int = 3
    recovery_seconds: float = 30.0


@dataclass(slots=True)
class ProviderURLConfig:
    provider_id: str
    api_key: str
    base_urls: list[BaseURLConfig]
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.5
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class URLConfig:
    providers: dict[str, ProviderURLConfig]


@dataclass(slots=True)
class AgentRoute:
    provider: str
    model: str


@dataclass(slots=True)
class ProviderPricing:
    input_per_1k_tokens: float
    output_per_1k_tokens: float


@dataclass(slots=True)
class RuntimeConfig:
    sqlite_path: str
    default_provider: str | None = None
    default_model: str | None = None
    enable_streaming: bool = True
    agent_routes: dict[str, AgentRoute] = field(default_factory=dict)
    pricing: dict[str, ProviderPricing] = field(default_factory=dict)


def _load_yaml(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        payload = yaml.safe_load(f) or {}

    if not isinstance(payload, dict):
        raise ConfigError(f"Config must be a mapping: {config_path}")
    return payload


def load_url_config(path: str | Path) -> URLConfig:
    payload = _load_yaml(path)
    providers_payload = payload.get("providers")
    if not isinstance(providers_payload, dict) or not providers_payload:
        raise ConfigError("`providers` must be a non-empty mapping in llm_urls.yaml")

    providers: dict[str, ProviderURLConfig] = {}
    for provider_id, entry in providers_payload.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"Provider config must be a mapping: {provider_id}")

        api_key = str(entry.get("api_key", "")).strip()
        if not api_key:
            raise ConfigError(f"Missing api_key for provider: {provider_id}")

        base_urls_payload = entry.get("base_urls")
        if not isinstance(base_urls_payload, list) or not base_urls_payload:
            raise ConfigError(
                f"`base_urls` must be a non-empty list for provider: {provider_id}"
            )

        base_urls: list[BaseURLConfig] = []
        for index, base_url_entry in enumerate(base_urls_payload):
            if not isinstance(base_url_entry, dict):
                raise ConfigError(
                    f"base_urls[{index}] must be a mapping for provider: {provider_id}"
                )

            endpoint_id = str(base_url_entry.get("id", "")).strip()
            url = str(base_url_entry.get("url", "")).strip()
            weight = int(base_url_entry.get("weight", 1))

            if not endpoint_id:
                raise ConfigError(
                    f"base_urls[{index}].id is required for provider: {provider_id}"
                )
            if not url:
                raise ConfigError(
                    f"base_urls[{index}].url is required for provider: {provider_id}"
                )
            if weight <= 0:
                raise ConfigError(
                    f"base_urls[{index}].weight must be > 0 for provider: {provider_id}"
                )

            base_urls.append(BaseURLConfig(id=endpoint_id, url=url, weight=weight))

        cb_payload = entry.get("circuit_breaker", {})
        if cb_payload is None:
            cb_payload = {}
        if not isinstance(cb_payload, dict):
            raise ConfigError(f"circuit_breaker must be a mapping for provider: {provider_id}")

        failure_threshold = int(cb_payload.get("failure_threshold", 3))
        recovery_seconds = float(cb_payload.get("recovery_seconds", 30.0))
        if failure_threshold <= 0:
            raise ConfigError(
                f"circuit_breaker.failure_threshold must be > 0 for provider: {provider_id}"
            )
        if recovery_seconds <= 0:
            raise ConfigError(
                f"circuit_breaker.recovery_seconds must be > 0 for provider: {provider_id}"
            )

        headers_payload = entry.get("headers", {})
        if headers_payload is None:
            headers_payload = {}
        if not isinstance(headers_payload, dict):
            raise ConfigError(f"headers must be a mapping for provider: {provider_id}")
        headers = {str(k): str(v) for k, v in headers_payload.items()}

        providers[provider_id] = ProviderURLConfig(
            provider_id=provider_id,
            api_key=api_key,
            base_urls=base_urls,
            timeout_seconds=float(entry.get("timeout_seconds", 30.0)),
            max_retries=int(entry.get("max_retries", 2)),
            retry_backoff_seconds=float(entry.get("retry_backoff_seconds", 0.5)),
            circuit_breaker=CircuitBreakerConfig(
                failure_threshold=failure_threshold,
                recovery_seconds=recovery_seconds,
            ),
            headers=headers,
        )

    return URLConfig(providers=providers)


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    payload = _load_yaml(path)

    sqlite_path = str(payload.get("sqlite_path", "")).strip()
    if not sqlite_path:
        raise ConfigError("`sqlite_path` is required in llm_runtime.yaml")

    defaults_payload = payload.get("defaults", {})
    if defaults_payload is None:
        defaults_payload = {}
    if not isinstance(defaults_payload, dict):
        raise ConfigError("`defaults` must be a mapping in llm_runtime.yaml")

    default_provider = defaults_payload.get("provider")
    if default_provider is not None:
        default_provider = str(default_provider).strip() or None

    default_model = defaults_payload.get("model")
    if default_model is not None:
        default_model = str(default_model).strip() or None

    routes_payload = payload.get("agent_routes", {})
    if routes_payload is None:
        routes_payload = {}
    if not isinstance(routes_payload, dict):
        raise ConfigError("`agent_routes` must be a mapping in llm_runtime.yaml")

    agent_routes: dict[str, AgentRoute] = {}
    for agent_id, route in routes_payload.items():
        if not isinstance(route, dict):
            raise ConfigError(f"agent_routes.{agent_id} must be a mapping")
        provider = str(route.get("provider", "")).strip()
        model = str(route.get("model", "")).strip()
        if not provider:
            raise ConfigError(f"agent_routes.{agent_id}.provider is required")
        if not model:
            raise ConfigError(f"agent_routes.{agent_id}.model is required")
        agent_routes[str(agent_id)] = AgentRoute(provider=provider, model=model)

    pricing_payload = payload.get("pricing", {})
    if pricing_payload is None:
        pricing_payload = {}
    if not isinstance(pricing_payload, dict):
        raise ConfigError("`pricing` must be a mapping in llm_runtime.yaml")

    pricing: dict[str, ProviderPricing] = {}
    for provider_id, price_entry in pricing_payload.items():
        if not isinstance(price_entry, dict):
            raise ConfigError(f"pricing.{provider_id} must be a mapping")
        input_per_1k = float(price_entry.get("input_per_1k_tokens", 0.0))
        output_per_1k = float(price_entry.get("output_per_1k_tokens", 0.0))
        if input_per_1k < 0 or output_per_1k < 0:
            raise ConfigError(f"pricing for {provider_id} must be >= 0")
        pricing[str(provider_id)] = ProviderPricing(
            input_per_1k_tokens=input_per_1k,
            output_per_1k_tokens=output_per_1k,
        )

    return RuntimeConfig(
        sqlite_path=sqlite_path,
        default_provider=default_provider,
        default_model=default_model,
        enable_streaming=bool(payload.get("enable_streaming", True)),
        agent_routes=agent_routes,
        pricing=pricing,
    )

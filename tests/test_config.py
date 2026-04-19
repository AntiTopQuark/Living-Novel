from __future__ import annotations

from pathlib import Path

import pytest

from common.llm.config import load_runtime_config, load_url_config
from common.llm.errors import ConfigError


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_url_config_success(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "llm_urls.yaml",
        """
providers:
  p1:
    api_key: test
    base_urls:
      - id: primary
        url: https://api.example.com
        weight: 2
      - id: backup
        url: https://api2.example.com
        weight: 1
""",
    )

    cfg = load_url_config(config_path)
    assert "p1" in cfg.providers
    assert len(cfg.providers["p1"].base_urls) == 2
    assert cfg.providers["p1"].base_urls[0].weight == 2


def test_load_url_config_invalid_weight(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "llm_urls.yaml",
        """
providers:
  p1:
    api_key: test
    base_urls:
      - id: primary
        url: https://api.example.com
        weight: 0
""",
    )

    with pytest.raises(ConfigError):
        load_url_config(config_path)


def test_load_url_config_empty_url(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "llm_urls.yaml",
        """
providers:
  p1:
    api_key: test
    base_urls:
      - id: primary
        url: ""
""",
    )

    with pytest.raises(ConfigError):
        load_url_config(config_path)


def test_load_runtime_config_success(tmp_path: Path) -> None:
    config_path = _write(
        tmp_path / "llm_runtime.yaml",
        """
sqlite_path: data/usage.db
enable_streaming: true
defaults:
  provider: p1
  model: m1
agent_routes:
  narrator:
    provider: p1
    model: m2
pricing:
  p1:
    input_per_1k_tokens: 0.1
    output_per_1k_tokens: 0.2
""",
    )

    cfg = load_runtime_config(config_path)
    assert cfg.sqlite_path == "data/usage.db"
    assert cfg.default_provider == "p1"
    assert cfg.agent_routes["narrator"].model == "m2"
    assert cfg.pricing["p1"].output_per_1k_tokens == 0.2

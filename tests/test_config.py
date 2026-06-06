import pytest
from pathlib import Path
from microtrace.config import Config, AgentConfig, LLMConfig, ToolsConfig


def test_default_config():
    config = Config()
    assert config.agent.max_iterations == 8
    assert config.llm.model == "MiniMax-M3-highspeed"
    assert config.llm.base_url == "https://api.minimaxi.com/anthropic"
    assert "/var/log" in config.tools.log_dirs


def test_save_load_roundtrip(tmp_path: Path):
    config_path = tmp_path / "config.yaml"

    original = Config()
    original.agent.max_iterations = 12
    original.llm.api_key = "test-key"
    original.save(config_path)

    loaded = Config.load(config_path)
    assert loaded.agent.max_iterations == 12
    assert loaded.llm.api_key == "test-key"


def test_load_nonexistent_returns_default():
    """配置文件不存在时返回默认"""
    config = Config.load(Path("/nonexistent/path/config.yaml"))
    assert config.agent.max_iterations == 8

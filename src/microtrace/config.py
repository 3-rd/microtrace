"""配置管理（platformdirs + YAML）"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from platformdirs import user_config_dir, user_data_dir
from pydantic import BaseModel, Field

APP_NAME = "microtrace"


def get_config_path() -> Path:
    """跨平台 config 路径"""
    return Path(user_config_dir(APP_NAME)) / "config.yaml"


def get_data_dir() -> Path:
    """跨平台数据目录（SQLite DB 在这里）"""
    path = Path(user_data_dir(APP_NAME))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_db_path() -> Path:
    """SQLite DB 路径"""
    return get_data_dir() / "state.db"


def get_history_path() -> Path:
    """REPL history 文件路径"""
    return get_data_dir() / "repl_history"


class AgentConfig(BaseModel):
    """Agent 配置（config.yaml 的 agent 小节）"""
    max_iterations: int = Field(default=8, ge=1, le=100)
    compaction_buffer: int = Field(
        default=20_000,
        description="固定 20K buffer（与 OpenCode 一致）"
    )


class LLMConfig(BaseModel):
    """LLM 配置（config.yaml 的 llm 小节）"""
    provider: Literal["minimax"] = Field(default="minimax")
    model: str = Field(default="MiniMax-M3-highspeed")
    api_key: str | None = Field(default=None)
    base_url: str = Field(default="https://api.minimaxi.com/anthropic")
    timeout: float = Field(default=120.0, description="LLM 调用超时（秒）")


class ToolsConfig(BaseModel):
    """Tools 配置（config.yaml 的 tools 小节）"""
    log_dirs: list[str] = Field(
        default_factory=lambda: [
            "/var/log",
            "/var/log/vnfm",
            "C:/ProgramData/VNFM/logs",
            "C:/Windows/System32/winevt/Logs",
        ],
        description="search_logs 工具搜索的目录列表"
    )
    java_source_roots: list[str] = Field(
        default_factory=list,
        description="Java 源码根目录（find_class 工具搜索）"
    )
    max_file_size: int = Field(
        default=10_000_000,
        description="read_file 最大文件大小（字节）"
    )


class Config(BaseModel):
    """完整配置"""
    agent: AgentConfig = Field(default_factory=AgentConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """从 YAML 文件加载（不存在则返回默认）"""
        if path is None:
            path = get_config_path()
        if not path.exists():
            return cls()
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    def save(self, path: Path | None = None) -> None:
        """保存配置到 YAML 文件"""
        if path is None:
            path = get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump(
                self.model_dump(exclude_none=True),
                f,
                allow_unicode=True,
                sort_keys=False,
            )

    @classmethod
    def get_api_key(cls) -> str | None:
        """从 config 或 env 读 API key（env 优先）"""
        env_key = os.environ.get("MICROTRACE_API_KEY")
        if env_key:
            return env_key
        try:
            return cls.load().llm.api_key
        except Exception:
            return None


def get_compaction_buffer() -> int:
    """Compaction buffer 大小（默认 20K，与 OpenCode 一致；Q9）"""
    try:
        return Config.load().agent.compaction_buffer
    except Exception:
        return 20_000


def get_max_iterations() -> int:
    """Agent max_iterations（默认 8，可配置；Q4）"""
    try:
        return Config.load().agent.max_iterations
    except Exception:
        return 8

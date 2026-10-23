from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class ReplyLimits(BaseModel):
    max_pending_per_group: int = Field(default=5, ge=1, le=50)
    max_concurrent_requests: int = Field(default=3, ge=1, le=20)
    request_timeout_seconds: float = Field(default=180, ge=1, le=600)
    max_message_chars: int = Field(default=4000, ge=100, le=20000)
    max_context_chars: int = Field(default=16000, ge=100, le=100000)
    max_context_tokens: int = Field(default=6000, ge=500, le=32000)
    user_cooldown_seconds: float = Field(default=3, ge=0, le=3600)
    max_history: int = Field(default=20, ge=0, le=200)
    probability: float = Field(default=0, ge=0, le=1)
    cooldown_seconds: float = Field(default=300, ge=0)
    delay_min_seconds: float = Field(default=1, ge=0, le=60)
    delay_max_seconds: float = Field(default=4, ge=0, le=60)

    @model_validator(mode="after")
    def ordered_delays(self):
        if self.delay_min_seconds > self.delay_max_seconds:
            raise ValueError("最小回复延迟不能超过最大延迟")
        return self


@lru_cache(maxsize=1)
def load_config() -> dict:
    path = Path(__file__).resolve().parent.parent / "config.yaml"

    with path.open("r", encoding="utf-8-sig") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError(f"{path.name} 必须包含有效的 YAML 配置")
    limits = ReplyLimits.model_validate(config.get("reply", {}))
    config.setdefault("reply", {}).update(limits.model_dump())
    if "memory" in config:
        from src.memory import MemoryConfig
        config["memory"] = MemoryConfig.model_validate(config["memory"]).model_dump()

    if "persona_file" in config:
        config["persona"] = load_persona(config)
    return config


def load_persona(config: dict) -> str:
    """每轮重新读取文件；失败明确报错，不发送空人设或继续使用旧人设。"""
    path = Path(__file__).resolve().parent.parent / "config.yaml"
    # 相对路径以 config.yaml 所在目录为基准，不受终端工作目录影响。
    # 保留旧的内联 persona；配置 persona_file 时由文件内容优先覆盖。
    if "persona_file" in config:
        name = config["persona_file"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("persona_file 必须是非空的文件路径")
        persona_path = Path(name).expanduser()
        if not persona_path.is_absolute():
            persona_path = path.parent / persona_path
        try:
            persona = persona_path.read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeError) as error:
            raise ValueError(f"无法读取 UTF-8 人设文件：{persona_path}") from error
        if not persona:
            raise ValueError(f"人设文件不能为空：{persona_path}")
        return persona
    return config["persona"]

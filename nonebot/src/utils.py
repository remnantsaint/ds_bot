from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache(maxsize=1)
def load_config() -> dict:
    path = Path(__file__).resolve().parent.parent / "config.yaml"

    with path.open("r", encoding="utf-8-sig") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError(f"{path.name} 必须包含有效的 YAML 配置")

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
        config["persona"] = persona

    return config

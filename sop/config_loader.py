"""配置加载与访问模块。

支持 YAML / JSON 两种配置文件：
  - 若安装 PyYAML，可加载 .yaml
  - .json 走标准库 json，零依赖
提供点号路径访问 config.get("mineru.base_url")，以及缺省值回退。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

DEFAULT_CONFIG_NAME = "sop_config.json"


def _ensure_mapping(data, cfg_path: str) -> Dict[str, Any]:
    """配置顶层必须是 dict；写错成列表/字符串时明确报错，不静默回退默认值。"""
    if data is None:                # 空文件按空配置处理
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"配置文件顶层必须是键值对对象，当前是 {type(data).__name__}: {cfg_path}"
        )
    return data


def _default_config_path() -> str:
    """默认配置文件：优先 JSON（零依赖），其次 YAML。"""
    here = os.path.dirname(os.path.abspath(__file__))
    config_dir = os.path.join(os.path.dirname(here), "config")
    json_path = os.path.join(config_dir, "sop_config.json")
    yaml_path = os.path.join(config_dir, "sop_config.yaml")
    if os.path.exists(json_path):
        return json_path
    if os.path.exists(yaml_path):
        return yaml_path
    return json_path


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载配置为 dict。path 为空时自动定位默认配置。"""
    cfg_path = path or _default_config_path()
    cfg_path = os.path.abspath(os.path.expanduser(cfg_path))
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")

    ext = os.path.splitext(cfg_path)[1].lower()
    if ext == ".json":
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return _ensure_mapping(json.load(f), cfg_path)
        except json.JSONDecodeError as exc:
            raise ValueError(f"配置文件不是合法 JSON: {cfg_path}（{exc}）") from exc
    if ext in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "加载 YAML 配置需要 PyYAML，请 `pip install pyyaml`，"
                "或直接使用同目录的 sop_config.json"
            ) from exc
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return _ensure_mapping(yaml.safe_load(f), cfg_path)
        except yaml.YAMLError as exc:
            raise ValueError(f"配置文件不是合法 YAML: {cfg_path}（{exc}）") from exc
    raise ValueError(f"不支持的配置文件格式: {ext}（支持 .json/.yaml/.yml）")


class Config:
    """配置包装：点号路径访问 + 缺省回退 + 相对路径解析。"""

    def __init__(self, data: Dict[str, Any], base_dir: Optional[str] = None):
        self._data = _ensure_mapping(data, "<inline>") if data is not None else {}
        self._base_dir = base_dir or os.getcwd()
        self.path: str = ""  # 实际加载的配置文件绝对路径（Config.load 时填充）

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        cfg_path = path or _default_config_path()
        data = load_config(cfg_path)
        obj = cls(data, base_dir=os.path.dirname(os.path.abspath(cfg_path)))
        obj.path = os.path.abspath(cfg_path)
        return obj

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, dotted_key: str, default: Optional[dict] = None) -> Dict[str, Any]:
        value = self.get(dotted_key)
        return value if isinstance(value, dict) else dict(default or {})

    def resolve_path(self, value: str) -> str:
        """相对路径相对配置文件目录解析为绝对路径。"""
        if not value:
            return value
        if os.path.isabs(value):
            return value
        return os.path.normpath(os.path.join(self._base_dir, value))

    @property
    def raw(self) -> Dict[str, Any]:
        return self._data

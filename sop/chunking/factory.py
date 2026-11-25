"""分块策略工厂：按名称/配置创建分块器。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import BaseChunker
from .parent_child import ParentChildChunker
from .other_strategies import (
    RecursiveChunker, MarkdownHeaderChunker, FixedSizeChunker,
    SentenceChunker, SemanticChunker,
)

REGISTRY = {
    "parent_child": ParentChildChunker,
    "recursive": RecursiveChunker,
    "markdown_header": MarkdownHeaderChunker,
    "fixed_size": FixedSizeChunker,
    "sentence": SentenceChunker,
    "semantic": SemanticChunker,
}


class ChunkFactory:
    """根据策略名与配置创建 Chunker。默认 parent_child。"""

    @staticmethod
    def available() -> list:
        return sorted(REGISTRY.keys())

    @staticmethod
    def create(strategy: str | None = None, config: Optional[Dict[str, Any]] = None) -> BaseChunker:
        name = str(strategy or "parent_child").strip().lower()
        cls = REGISTRY.get(name)
        if cls is None:
            raise ValueError(
                f"未知分块策略: {name}，可用策略: {', '.join(ChunkFactory.available())}"
            )
        cfg = dict(config or {})
        chunker = cls(cfg)
        # 注入长度度量配置（token / char），各策略通过 self.measure() 使用
        unit = cfg.get("_length_unit", "char")
        encoding = cfg.get("_token_encoding", "cl100k_base")
        if isinstance(chunker, BaseChunker):
            chunker.configure_length(unit, encoding)
        return chunker

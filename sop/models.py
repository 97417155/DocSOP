"""统一数据模型：解析结果与分块结果。

设计目标：不管底层用哪个解析器 / 哪种分块策略，对外输出结构保持一致，
便于上层入库、检索或导出。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Element:
    """结构化元素（可选）：标题/文本/表格/图片/公式，带 bbox 溯源。"""

    type: str                       # heading | text | table | image | equation | code
    text: str
    level: int = 0                  # 标题层级
    bbox: List[float] = field(default_factory=list)
    page: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseResult:
    """单个文件解析后的统一结果。"""

    success: bool
    file_path: str
    file_name: str
    file_type: str                  # 归一化后的类型后缀，如 .pdf
    parser: str                     # 实际使用的解析器名
    content: str = ""               # 解析后的纯文本 / Markdown 全文
    elements: List[Element] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    warnings: List[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self, include_elements: bool = True) -> Dict[str, Any]:
        data = asdict(self)
        if not include_elements:
            data.pop("elements", None)
        return data

    def to_json(self, include_elements: bool = True, ensure_ascii: bool = False) -> str:
        return json.dumps(self.to_dict(include_elements), ensure_ascii=ensure_ascii, indent=2)


@dataclass
class Chunk:
    """单个分块。

    parent_child 策略下：parent 块 child_ids 非空、parent_id 为空；
    child 块 parent_id 指向父块。其余策略 parent_id 一般为空。
    """

    chunk_id: str
    text: str
    index: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    child_ids: List[str] = field(default_factory=list)
    char_count: int = 0
    # 溯源信息（来自 Element）：该块覆盖的页码集合、合并 bbox、命中的元素索引
    pages: List[int] = field(default_factory=list)
    bbox: List[float] = field(default_factory=list)
    source_element_ids: List[int] = field(default_factory=list)

    def __post_init__(self):
        if not self.char_count:
            self.char_count = len(self.text or "")

    @property
    def page(self) -> int:
        """主页码（取第一个），无溯源时返回 0。"""
        return self.pages[0] if self.pages else 0


@dataclass
class ChunkResult:
    """一次分块的输出。"""

    strategy: str
    chunks: List[Chunk] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    @property
    def parents(self) -> List[Chunk]:
        return [c for c in self.chunks if c.parent_id is None]

    @property
    def children(self) -> List[Chunk]:
        return [c for c in self.chunks if c.parent_id is not None]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "count": len(self.chunks),
            "metadata": self.metadata,
            "elapsed_ms": self.elapsed_ms,
            "chunks": [asdict(c) for c in self.chunks],
        }

    def to_json(self, ensure_ascii: bool = False) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=ensure_ascii, indent=2)


@dataclass
class ProcessResult:
    """一次完整 SOP 处理（解析 + 分块）的输出。"""

    parse: ParseResult
    chunk: Optional[ChunkResult] = None
    output_files: Dict[str, str] = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time()))
    error: str = ""                     # 解析成功但分块/编排阶段失败时的错误
    # 分支路由（document_parsing_method）— 兼容字段，默认不输出
    route: Optional[Dict[str, Any]] = field(default=None)
    kb_context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_elements: bool = True, include_route: bool = False) -> Dict[str, Any]:
        data = {
            "created_at": self.created_at,
            "ok": self.ok,
            "parse": self.parse.to_dict(include_elements=include_elements),
            "chunk": self.chunk.to_dict() if self.chunk else None,
            "output_files": self.output_files,
            "error": self.error,
        }
        if include_route and self.route is not None:
            data["route"] = self.route
        return data

    def to_json(self, ensure_ascii: bool = False, include_elements: bool = True,
                include_route: bool = False) -> str:
        return json.dumps(
            self.to_dict(include_elements=include_elements, include_route=include_route),
            ensure_ascii=ensure_ascii, indent=2,
        )

    @property
    def ok(self) -> bool:
        """只有解析与分块都成功才算整体成功。"""
        return self.parse.success and not self.error

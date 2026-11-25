"""知识库入库风格的输出结构：docs / doc_chunks / _engine_meta。

字段为通用的知识库入库风格，便于下游直接对照入库：
  - DocRecord      → docs 集合单条文档元信息
  - ChunkRecord    → doc_chunks 集合单条分块（box_json_str 为 JSON 字符串）
  - EngineMeta     → _engine_meta（execution_trace / _fallback / 实际解析方法等）
  - KBOutput       → 聚合 docs / doc_chunks / _engine_meta / _engine_data，
                     负责落盘为四个独立 JSON 文件

脱敏原则：file_path 为调用方传入的源标识（URL 或相对路径皆可，不写入本机绝对路径）；
kb_id / source_file_id 由调用方提供，默认占位符，不包含机器或项目信息。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


def _timestamp_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class DocRecord:
    """docs 集合：文档元信息。"""

    file_path: str
    whole_content: str
    chunk_count: int
    highlight_pdf_path: str = ""
    kb_id: str = "default"
    source_file_id: str = ""
    task_version: str = ""
    created_at: str = field(default_factory=_timestamp_str)
    updated_at: str = field(default_factory=_timestamp_str)
    keywords_pair: Dict[str, Any] = field(default_factory=dict)
    para_elements_count: int = 0
    para_headers_md: str = ""
    library_id: str = ""
    # 与 doc_chunks.doc_id 对应，保证文档与分块可通过该字段关联
    doc_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ChunkRecord:
    """doc_chunks 集合：单块。box_json_str 形状与入库一致：JSON 字符串。"""

    doc_id: str
    chunk_index: int
    text: str
    box_json_str: str = '""'
    chunk_unique_id: str = ""
    kb_id: str = "default"
    created_at: str = field(default_factory=_timestamp_str)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EngineMeta:
    """_engine_meta：执行轨迹与降级标记。"""

    execution_trace: List[Dict[str, Any]] = field(default_factory=list)
    _fallback: bool = False
    document_parsing_method: str = ""
    used_mineru: bool = False
    mineru_backend_type: str = ""
    mineru_version: str = ""
    runtime_version: str = "1.0"
    started_at: str = field(default_factory=_timestamp_str)
    ended_at: str = ""

    def add_trace(self, service_name: str, method: str) -> None:
        """追加一条执行轨迹。"""
        trace: Dict[str, Any] = {
            "runtime_version": self.runtime_version,
            "started_at": self.started_at,
            "ended_at": self.ended_at or _timestamp_str(),
            "document_parsing_method": method,
        }
        if self.used_mineru:
            trace["mineru_version"] = self.mineru_version
            trace["mineru_backend_type"] = self.mineru_backend_type
        self.execution_trace.append({service_name: trace})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class KBOutput:
    """聚合输出：四个集合的入库记录（docs / doc_chunks / _engine_meta / _engine_data）。"""

    docs: List[Dict[str, Any]] = field(default_factory=list)
    doc_chunks: List[Dict[str, Any]] = field(default_factory=list)
    engine_meta: Dict[str, Any] = field(default_factory=dict)
    engine_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "docs": self.docs,
            "doc_chunks": self.doc_chunks,
            "_engine_meta": self.engine_meta,
            "_engine_data": self.engine_data,
        }

    def to_json(self, ensure_ascii: bool = False, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=ensure_ascii, indent=indent)


def make_chunk_unique_id(source_file_id: str, index: int) -> str:
    """生成全局唯一 chunk id：{source_file_id}_{ts}_{i}_{rand}。"""
    return f"{source_file_id}_{int(time.time())}_{index}_{uuid.uuid4().hex[:4]}"


def box_to_json_str(box: Dict[int, List[List[float]]] | None) -> str:
    """box 字典 → box_json_str。无 box 时写 JSON 空字符串 "\\"\\""。"""
    if not box:
        return json.dumps("")
    return json.dumps(box, ensure_ascii=False)

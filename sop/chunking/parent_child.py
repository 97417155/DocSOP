"""父子分块策略（默认）。

parent_child chunking：
  1. 先按标题边界/段落切成较大的 parent 块（保上下文，parent_size）
  2. 再在每个 parent 内切成较小的 child 块（用于检索，child_size + overlap）
  3. 建立双向关联：parent.child_ids <-> child.parent_id
检索时命中 child，返回其 parent 作为上下文，兼顾召回精度与语义完整。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..models import Chunk, Element
from .base import BaseChunker


class ParentChildChunker(BaseChunker):
    name = "parent_child"

    def __init__(self, config: Dict[str, Any] | None = None):
        super().__init__(config)
        cfg = config or {}
        self.parent_size = self._positive_int(cfg, "parent_size", 1500)
        self.parent_overlap = self._bounded_overlap(cfg, "parent_overlap", 0, self.parent_size)
        self.child_size = self._positive_int(cfg, "child_size", 400)
        self.child_overlap = self._bounded_overlap(cfg, "child_overlap", 80, self.child_size)
        self.respect_headers = bool(cfg.get("respect_headers", True))

    def chunk(self, text: str, elements: List[Element] | None = None, metadata: dict | None = None) -> List[Chunk]:
        base_meta = dict(metadata or {})
        chunks: List[Chunk] = []

        # 1) 生成父块
        if self.respect_headers:
            parent_texts = self.split_by_headers(text, self.parent_size)
        else:
            parent_texts = self._sliding(text, self.parent_size, self.parent_overlap)

        # 2) 每个父块切子块并关联
        child_counter = 0
        for p_idx, parent_text in enumerate(parent_texts):
            if not parent_text.strip():
                continue
            parent_id = f"parent_{p_idx:04d}"
            parent_chunk = Chunk(
                chunk_id=parent_id,
                text=parent_text,
                index=p_idx,
                metadata={**base_meta, "level": "parent", "child_count": 0},
            )

            child_texts = self._sliding(parent_text, self.child_size, self.child_overlap)
            child_ids: List[str] = []
            for child_text in child_texts:
                if not child_text.strip():
                    continue
                cid = f"child_{child_counter:05d}"
                child_counter += 1
                child_ids.append(cid)
                chunks.append(Chunk(
                    chunk_id=cid,
                    text=child_text,
                    index=len(chunks),
                    metadata={**base_meta, "level": "child", "parent_index": p_idx},
                    parent_id=parent_id,
                ))
            parent_chunk.child_ids = child_ids
            parent_chunk.metadata["child_count"] = len(child_ids)
            chunks.append(parent_chunk)

        # 排序：父块在前，其后紧跟其子块，保证输出可读
        ordered: List[Chunk] = []
        by_id = {c.chunk_id: c for c in chunks}
        emitted = set()
        for c in chunks:
            if c.parent_id is None and c.chunk_id not in emitted:
                ordered.append(c); emitted.add(c.chunk_id)
                for cid in c.child_ids:
                    if cid in by_id and cid not in emitted:
                        ordered.append(by_id[cid]); emitted.add(cid)
        for c in chunks:
            if c.chunk_id not in emitted:
                ordered.append(c)
        # 重新编号 index
        for i, c in enumerate(ordered):
            c.index = i
        return ordered

"""其它通用分块策略：递归、标题层级、固定长度、语义、句子。"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ..models import Chunk
from .base import BaseChunker


class RecursiveChunker(BaseChunker):
    """LangChain 风格递归字符分块。"""

    name = "recursive"

    def __init__(self, config: Dict[str, Any] | None = None):
        super().__init__(config)
        cfg = config or {}
        self.size = self._positive_int(cfg, "chunk_size", 500)
        self.overlap = self._bounded_overlap(cfg, "chunk_overlap", 50, self.size)
        self.separators = cfg.get("separators") or ["\n\n", "\n", "。", "！", "？", "；", "，", ", ", " ", ""]

    def chunk(self, text, elements=None, metadata=None):
        parts = self.recursive_split(text, self.size, self.overlap, list(self.separators))
        return [Chunk(chunk_id=f"rec_{i:05d}", text=p, index=i, metadata=dict(metadata or {}))
                for i, p in enumerate(parts) if p.strip()]


class MarkdownHeaderChunker(BaseChunker):
    """按 Markdown 标题层级分块。"""

    name = "markdown_header"

    def __init__(self, config: Dict[str, Any] | None = None):
        super().__init__(config)
        cfg = config or {}
        self.max_size = self._positive_int(cfg, "max_size", 1500)
        try:
            self.max_level = max(1, min(6, int(cfg.get("header_level", 6))))
        except (TypeError, ValueError):
            self.max_level = 6

    def chunk(self, text, elements=None, metadata=None):
        parts = self.split_by_headers(text, self.max_size, max_level=self.max_level)
        return [Chunk(chunk_id=f"hdr_{i:05d}", text=p, index=i,
                      metadata={**(metadata or {}), "strategy": "markdown_header"})
                for i, p in enumerate(parts) if p.strip()]


class FixedSizeChunker(BaseChunker):
    """固定长度 + overlap 分块。"""

    name = "fixed_size"

    def __init__(self, config: Dict[str, Any] | None = None):
        super().__init__(config)
        cfg = config or {}
        self.size = self._positive_int(cfg, "chunk_size", 500)
        self.overlap = self._bounded_overlap(cfg, "chunk_overlap", 50, self.size)

    def chunk(self, text, elements=None, metadata=None):
        parts = self._sliding(text, self.size, self.overlap)
        return [Chunk(chunk_id=f"fix_{i:05d}", text=p, index=i, metadata=dict(metadata or {}))
                for i, p in enumerate(parts) if p.strip()]


class SentenceChunker(BaseChunker):
    """按中文/英文句子分块，带句子级 overlap。"""

    name = "sentence"

    def __init__(self, config: Dict[str, Any] | None = None):
        super().__init__(config)
        cfg = config or {}
        self.max_size = self._positive_int(cfg, "max_size", 500)
        self.overlap = self._non_negative_int(cfg, "overlap_sentences", 1)

    def chunk(self, text, elements=None, metadata=None):
        sents = [s.strip() for s in re.split(r"(?<=[。！？!?；;])\s*|\n+", text) if s.strip()]
        parts: List[str] = []
        buf_sents: List[str] = []
        buf_len = 0
        for s in sents:
            # 单句本身就超长：先冲刷已有缓冲，再把该句硬切为多片
            if self.measure(s) > self.max_size:
                if buf_sents:
                    parts.append("".join(buf_sents)); buf_sents, buf_len = [], 0
                parts.extend(self._sliding(s, self.max_size, 0))
                continue
            if buf_sents and buf_len + self.measure(s) > self.max_size:
                parts.append("".join(buf_sents))
                # overlap 取“当前块”的末尾几句，而不是全文末尾；
                # 且必须给新句留出空间，否则超大 overlap 会让块长持续膨胀、
                # 突破 max_size（例如 overlap_sentences=10**9 时每块越来越长）。
                s_len = self.measure(s)
                tail = list(buf_sents[-self.overlap:]) if self.overlap else []
                while tail and sum(self.measure(x) for x in tail) + s_len > self.max_size:
                    tail.pop(0)
                buf_sents = tail
                buf_len = sum(self.measure(x) for x in buf_sents)
            buf_sents.append(s)
            buf_len += self.measure(s)
        if buf_sents:
            parts.append("".join(buf_sents))
        return [Chunk(chunk_id=f"sent_{i:05d}", text=p, index=i, metadata=dict(metadata or {}))
                for i, p in enumerate(parts) if p.strip()]


class SemanticChunker(BaseChunker):
    """语义分块：相邻句子相似度低于阈值时切分。

    依赖 sentence-transformers（可选）。若不可用，自动降级为 RecursiveChunker。
    """

    name = "semantic"

    def __init__(self, config: Dict[str, Any] | None = None):
        super().__init__(config)
        cfg = config or {}
        self.max_size = self._positive_int(cfg, "max_size", 800)
        self.model_name = cfg.get("model", "sentence-transformers/all-MiniLM-L6-v2")
        self.threshold = float(cfg.get("similarity_threshold", 0.7))
        self._model = None
        self._fallback = RecursiveChunker({
            "chunk_size": self.max_size,
            "chunk_overlap": min(50, max(0, self.max_size - 1)),
        })

    def _load_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def chunk(self, text, elements=None, metadata=None):
        try:
            model = self._load_model()
            import numpy as np  # type: ignore
        except Exception as exc:
            # 模型未安装、离线导致权重下载失败、依赖缺失等任何加载问题都降级，
            # 不能让可选的语义分块把整条解析链路打断。
            self._fallback.configure_length(
                getattr(self, "length_unit", "char"),
                getattr(self, "config", {}).get("_token_encoding", "cl100k_base"),
            )
            chunks = self._fallback.chunk(text, elements, metadata)
            for c in chunks:
                c.metadata["semantic_fallback"] = f"语义模型不可用，已降级: {type(exc).__name__}: {exc}"
            return chunks

        sents = [s.strip() for s in re.split(r"(?<=[。！？!?；;])\s*|\n+", text) if s.strip()]
        if not sents:
            return []
        embs = model.encode(sents)
        groups, cur = [], [sents[0]]
        for i in range(1, len(sents)):
            sim = float(np.dot(embs[i], embs[i - 1]) / (np.linalg.norm(embs[i]) * np.linalg.norm(embs[i - 1]) + 1e-8))
            cur.append(sents[i])
            joined = "".join(cur)
            if sim < self.threshold or self.measure(joined) >= self.max_size:
                groups.append(joined); cur = []
        if cur:
            groups.append("".join(cur))
        # 兜底：任何超过 max_size 的组（可能含超长单句）再硬切一次
        final: List[str] = []
        for g in groups:
            if self.measure(g) > self.max_size:
                final.extend(self._sliding(g, self.max_size, 0))
            else:
                final.append(g)
        return [Chunk(chunk_id=f"sem_{i:05d}", text=g, index=i, metadata=dict(metadata or {}))
                for i, g in enumerate(final)]

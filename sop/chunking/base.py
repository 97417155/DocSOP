"""分块策略抽象基类与公共工具。

长度度量：默认按字符（char）；配置 chunking.length_unit=token 时按 token 切分
（优先 tiktoken，缺失时按 CJK 估算）。所有切分工具通过 self.measure / self._take
工作，因此各策略自动支持两种度量。
"""

from __future__ import annotations

import re
from typing import List

from ..models import Chunk, Element


class BaseChunker:
    """所有分块策略的基类。"""

    name = "base"

    def __init__(self, config: dict | None = None):
        self.config = dict(config or {})
        self.configure_length(
            self.config.get("_length_unit", "char"),
            self.config.get("_token_encoding", "cl100k_base"),
        )

    @staticmethod
    def _positive_int(config: dict, key: str, default: int) -> int:
        """读取必须为正整数的分块参数，非法配置直接报错而不是静默退化。"""
        try:
            value = int(config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置项 {key} 必须是正整数") from exc
        if value <= 0:
            raise ValueError(f"配置项 {key} 必须是正整数，当前值: {value}")
        return value

    @staticmethod
    def _non_negative_int(config: dict, key: str, default: int) -> int:
        try:
            value = int(config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置项 {key} 必须是非负整数") from exc
        if value < 0:
            raise ValueError(f"配置项 {key} 必须是非负整数，当前值: {value}")
        return value

    @staticmethod
    def _bounded_overlap(config: dict, key: str, default: int, size: int) -> int:
        """overlap 必须小于块大小，否则滑窗原地踏步，产出内容完全相同的重复块。"""
        value = BaseChunker._non_negative_int(config, key, default)
        if value >= size:
            raise ValueError(
                f"配置项 {key} 必须小于块大小 {size}，当前值: {value}；"
                "相等或更大将导致分块无限重复"
            )
        return value

    def configure_length(self, unit: str, encoding: str = "cl100k_base"):
        """由工厂注入长度度量配置（子类覆盖 __init__ 时不会调用基类构造）。"""
        self.config = getattr(self, "config", {})
        self.config["_length_unit"] = unit
        self.config["_token_encoding"] = encoding
        self.length_unit = str(unit).lower()
        self._token_encoder = None
        if self.length_unit == "token":
            self._init_token_encoder()

    def _init_token_encoder(self):
        try:
            import tiktoken  # type: ignore
            self._token_encoder = tiktoken.get_encoding(
                self.config.get("_token_encoding", "cl100k_base")
            )
        except Exception:
            self._token_encoder = None

    def _ensure_length_init(self):
        if not hasattr(self, "length_unit"):
            self.configure_length(
                getattr(self, "config", {}).get("_length_unit", "char"),
                getattr(self, "config", {}).get("_token_encoding", "cl100k_base"),
            )

    def measure(self, text: str) -> int:
        """按配置度量长度：char（默认）或 token。"""
        self._ensure_length_init()
        if self.length_unit != "token":
            return len(text or "")
        if self._token_encoder is not None:
            return len(self._token_encoder.encode(text or ""))
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        return int(cjk * 1.5 + other * 0.25)

    def _est_char_boundary(self, text: str, units: int) -> int:
        """估算模式下：累计 token 达到 units 所需的字符数。"""
        acc = 0.0
        for idx, ch in enumerate(text):
            acc += 1.5 if "\u4e00" <= ch <= "\u9fff" else 0.25
            if acc >= units:
                return idx + 1
        return len(text)

    def _take(self, text: str, n: int) -> str:
        """取前 n 个度量单位（字符或 token）对应的文本。"""
        self._ensure_length_init()
        if self.length_unit != "token":
            return text[:n]
        if self._token_encoder is not None:
            try:
                ids = self._token_encoder.encode(text)[:n]
                return self._token_encoder.decode(ids)
            except Exception:
                return text[:n]
        return text[:self._est_char_boundary(text, n)]

    def chunk(self, text: str, elements: List[Element] | None = None, metadata: dict | None = None) -> List[Chunk]:
        raise NotImplementedError

    # ---------- 通用工具（实例方法，用 self.measure 度量） ----------

    def split_by_headers(self, text: str, max_size: int, max_level: int = 6) -> List[str]:
        """按 markdown 标题切成若干带标题路径的块（超长块再按段落切）。

        max_level 控制识别的最深标题级别（1-6）：级别大于它的标题视为正文。
        """
        level_cap = max(1, min(6, int(max_level)))
        header_re = re.compile(r"^(#{1,%d})\s+(.*)$" % level_cap)
        lines = text.splitlines()
        blocks = []  # (level, title, [lines])
        i = 0
        while i < len(lines):
            m = header_re.match(lines[i])
            if m:
                level, title = len(m.group(1)), m.group(2).strip()
                body = []
                i += 1
                while i < len(lines) and not header_re.match(lines[i]):
                    body.append(lines[i]); i += 1
                blocks.append((level, title, body))
            else:
                body = []
                while i < len(lines) and not header_re.match(lines[i]):
                    body.append(lines[i]); i += 1
                blocks.append((0, "", body))

        results: List[str] = []
        # 保留标题的真实层级（level, title），不要按栈深度重编号，
        # 否则以 ### 开头的文档会被改写成 #，块内标题层级与原文不一致。
        stack: List[tuple] = []
        for level, title, body in blocks:
            if level:
                stack = [item for item in stack if item[0] < level] + [(level, title)]
            header_text = "\n".join(f"{'#' * lv} {h}" for lv, h in stack)
            content = "\n".join(body).strip()
            header_len = self.measure(header_text) if header_text else 0

            # 标题路径本身已达/超过上限时绝不能拼进正文块：否则正文会被切成
            # 1 字符一片并各自重复整段标题，既击穿 max_size 又让输出体积爆炸。
            # 此时标题单独硬切成块，正文按 max_size 独立切分。
            if header_len >= max_size:
                results.extend(self._sliding(header_text, max_size, 0))
                if content:
                    results.extend(self._split_paragraphs(content, max_size))
                continue

            if not content:
                if header_text:
                    results.append(header_text)
                continue
            combined = (header_text + "\n" + content).strip() if header_text else content
            if self.measure(combined) <= max_size:
                results.append(combined)
            else:
                budget = max_size - header_len - 1
                for piece in self._split_paragraphs(content, max(1, budget)):
                    results.append((header_text + "\n" + piece).strip())
        return results

    def _split_paragraphs(self, text: str, max_size: int) -> List[str]:
        paras = [p.strip() for p in text.split("\n\n") if p.strip()]
        out, buf = [], ""
        for p in paras:
            if self.measure(p) > max_size:
                if buf:
                    out.append(buf); buf = ""
                out.extend(self._sliding(p, max_size, 0))
            elif self.measure(buf) + self.measure(p) + 2 <= max_size:
                buf = (buf + "\n\n" + p).strip()
            else:
                if buf:
                    out.append(buf)
                buf = p
        if buf:
            out.append(buf)
        return out

    def _sliding(self, text: str, size: int, overlap: int) -> List[str]:
        """滑动窗口切分；size/overlap 按 self.measure 的度量解释。"""
        if size <= 0:
            return [text]
        if overlap >= size:
            overlap = 0
        step = size - overlap
        out: List[str] = []
        start_tok = 0
        total = self.measure(text)
        while start_tok < total:
            head = self._take_at(text, start_tok)
            piece = self._take(head, size)
            if not piece:
                break
            out.append(piece)
            if start_tok + size >= total:
                break
            start_tok += step
        return out or [""]

    def _take_at(self, text: str, offset: int) -> str:
        """跳过前 offset 个度量单位，返回剩余文本。"""
        self._ensure_length_init()
        if self.length_unit != "token":
            return text[offset:]
        if self._token_encoder is not None:
            try:
                ids = self._token_encoder.encode(text)[offset:]
                return self._token_encoder.decode(ids)
            except Exception:
                return text[offset:]
        return text[self._est_char_boundary(text, offset):]

    def recursive_split(self, text: str, size: int, overlap: int, separators: List[str]) -> List[str]:
        """LangChain 风格递归切分。"""
        if not text:
            return []
        if size <= 0:
            raise ValueError(f"递归分块的 size 必须为正整数，当前值: {size}")
        # 算法层防御：即使绕过配置校验直接调用，也不允许 overlap >= size
        overlap = max(0, min(int(overlap), size - 1))
        if self.measure(text) <= size:
            return [text]
        sep = ""
        for candidate in separators:
            if candidate and candidate in text:
                sep = candidate
                break
        if not sep:
            return self._sliding(text, size, overlap)

        parts = text.split(sep)
        out: List[str] = []
        buf = ""
        for part in parts:
            piece = part if not sep else part + sep
            if self.measure(buf) + self.measure(piece) <= size:
                buf += piece
            else:
                if buf:
                    out.append(buf.rstrip())
                    tail = self._take_at(buf, max(0, self.measure(buf) - overlap)) if overlap else ""
                    buf = tail + piece
                else:
                    out.extend(self.recursive_split(piece, size, overlap, separators[1:] or [""]))
                # 不设固定迭代上限：overlap 接近 size 时步长很小，固定上限会
                # 在超长文本上静默丢掉尾部。用“推进即继续、不推进即停”保证不丢内容。
                while self.measure(buf) > size:
                    head = self._take(buf, size)
                    if not head:
                        break
                    out.append(head)
                    nxt = self._take_at(buf, size - overlap) if overlap else ""
                    if nxt == buf:
                        break
                    buf = nxt
        if buf.strip():
            out.append(buf.rstrip())
        return [x for x in out if x.strip()]

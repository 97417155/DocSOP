"""解析器抽象基类与注册表。

约定：每个解析器实现 parse(ctx) -> ParseResult 的内容部分。
为降低耦合，parse 方法只返回 (content, elements, metadata, warnings)，
由 Dispatcher 统一封装为 ParseResult。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config_loader import Config
from ..models import Element

ParsePayload = Tuple[str, List[Element], Dict[str, Any], List[str]]


@dataclass
class ParseContext:
    """传给解析器的上下文。"""

    path: str
    file_name: str
    ext: str
    file_type: str
    config: Config
    url_mode: bool = False
    options: Dict[str, Any] = field(default_factory=dict)  # output_dir 等运行时上下文


class BaseParser:
    """所有解析器的基类。"""

    name = "base"
    supported_exts: Tuple[str, ...] = ()

    def parse(self, ctx: ParseContext) -> ParsePayload:
        raise NotImplementedError

    # ---- 通用工具 ----

    @staticmethod
    def detect_encoding(path: str, config: Config) -> str:
        mode = str(config.get("parsing.encoding", "auto") or "auto")
        if mode and mode.lower() != "auto":
            return mode
        with open(path, "rb") as fh:
            raw = fh.read(4096)
        # 优先 BOM
        if raw.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"
        for lib in ("charset_normalizer", "chardet"):
            try:
                mod = __import__(lib)
                if lib == "charset_normalizer":
                    guess = mod.from_bytes(raw).best()
                    enc = guess.encoding if guess else None
                else:
                    enc = mod.detect(raw).get("encoding")
                if enc:
                    return enc
            except Exception:
                continue
        return "utf-8"

    @staticmethod
    def read_text(path: str, config: Config) -> str:
        enc = BaseParser.detect_encoding(path, config)
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()

    @staticmethod
    def require(import_name: str, hint: str | None = None):
        """按需导入重依赖，缺失时给出可读提示。"""
        try:
            return __import__(import_name)
        except ImportError as exc:
            raise RuntimeError(
                hint or f"解析该格式需要依赖 `{import_name}`，请先安装：pip install {import_name}"
            ) from exc


class ParserRegistry:
    """按后缀注册 / 查找解析器。"""

    def __init__(self):
        self._parsers: List[BaseParser] = []

    def register(self, parser: BaseParser, ext: str) -> None:
        parser.supported_exts = tuple(set(parser.supported_exts) | {ext})
        self._parsers.append(parser)

    def find(self, ext: str) -> Optional[BaseParser]:
        for parser in reversed(self._parsers):
            if ext in parser.supported_exts:
                return parser
        return None

    def extensions(self) -> List[str]:
        seen = []
        for p in self._parsers:
            for e in p.supported_exts:
                if e not in seen:
                    seen.append(e)
        return sorted(seen)

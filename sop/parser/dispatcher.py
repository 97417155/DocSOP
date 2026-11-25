"""文档解析统一调度器。

流程：识别输入（本地文件 / URL）-> 归一化后缀 -> 选择解析器 ->
      解析 -> 封装 ParseResult。PDF / 图片走 MinerU（加载配置）。
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from typing import Optional
from urllib.parse import unquote, urlparse

# Windows / URL 中不能作为文件名的字符，统一替换为下划线
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_file_stem(source: str, is_url: bool = False) -> tuple:
    """从本地路径或 URL 安全提取 (文件名, 后缀)。

    - Windows 反斜杠路径必须先归一化，否则 basename 会返回整串绝对路径，
      进而让输出目录拼接时被当作绝对路径绕过。
    - URL 先解码再取路径末段，避免 ?query 与 ../ 穿越。
    """
    if is_url:
        try:
            path = unquote(urlparse(source).path)
        except Exception:
            path = source.split("?")[0]
    else:
        path = str(source).replace("\\", "/")
    name = os.path.basename(path.rstrip("/")) or "document"
    name = _UNSAFE_NAME.sub("_", name).strip().strip(".")
    if not name or name in (".", ".."):
        name = "document"
    return os.path.splitext(name)

from .base import ParserRegistry, ParseContext
from .simple_parsers import TextParser, HtmlParser, OfdParser
from .office_parsers import DocxParser, PptxParser, ExcelParser, LegacyOfficeParser
from .pdf_parser import PdfParser, ImageParser
from ..config_loader import Config
from ..models import ParseResult

URL_EXTS = (".html", ".htm")


def download_url_to_temp(source: str, ext: str, config: "Config") -> str:
    """把非 HTML 的 URL 下载到临时文件，返回本地路径。失败抛异常。

    供通用解析与分支路由共用：分支路由需要本地路径做 docx→PDF 转换，
    必须先落地再走四分支，否则 os.path.splitext(url) 后无法打开。
    调用方负责在使用完毕后 os.remove 清理。
    """
    import requests
    limit = int(config.get("parsing.max_url_download_bytes", 104857600) or 0)
    tmp_path = ""
    try:
        with requests.get(source, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            fd, tmp_path = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, "wb") as out:
                total = 0
                for chunk in resp.iter_content(65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if limit and total > limit:
                        raise RuntimeError(f"URL 下载超过限制 {limit} 字节")
                    out.write(chunk)
        return tmp_path
    except Exception:
        # 下载/写盘中途失败时清掉半成品，避免临时目录不断堆积
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


class DocumentParser:
    """文档解析器（对外主对象之一）。"""

    def __init__(self, config: Config):
        self.config = config
        self.registry = self._build_registry()

    @classmethod
    def from_config(cls, path: Optional[str] = None) -> "DocumentParser":
        return cls(Config.load(path))

    def _build_registry(self) -> ParserRegistry:
        reg = ParserRegistry()
        for parser in (
            TextParser(), HtmlParser(), OfdParser(),
            DocxParser(), PptxParser(), ExcelParser(), LegacyOfficeParser(),
            PdfParser(), ImageParser(),
        ):
            for ext in parser.supported_exts:
                reg.register(parser, ext)
        return reg

    def supported_extensions(self):
        return self.registry.extensions()

    def _fail(self, source: str, file_name: str, ext: str, parser: str,
              error: str, warnings: Optional[list] = None, started: float = None) -> ParseResult:
        """统一构造失败结果，保留 parser 名与耗时信息。"""
        return ParseResult(
            success=False, file_path=source, file_name=file_name, file_type=ext,
            parser=parser, error=error, warnings=list(warnings or []),
            elapsed_ms=int((time.time() - (started if started is not None else time.time())) * 1000),
        )

    def parse(self, source: str, output_dir: str = "") -> ParseResult:
        """解析一个本地文件或 URL。

        失败语义：文件不存在 / 零字节 / 解析抛错 / 解析结果为空（除非显式允许）
        一律返回 success=False + error，不再用 warnings 伪装成功。
        output_dir 用于内嵌图片落盘目录的基准（相对配置目录时）。
        """
        started = time.time()
        if not isinstance(source, str) or not source.strip():
            return self._fail(
                str(source), "", "", "none",
                f"输入源必须是非空字符串，当前类型: {type(source).__name__}", started=started,
            )
        is_url = source.lower().startswith(("http://", "https://"))
        base, ext = safe_file_stem(source, is_url=is_url)
        ext = ext.lower()

        # --- 本地文件前置校验：早失败，错误信息明确 ---
        if not is_url:
            if not source or not os.path.exists(source):
                return self._fail(source, base, ext, "none", f"文件不存在: {source}", started=started)
            if os.path.isdir(source):
                return self._fail(source, base, ext, "none", f"输入是目录而非文件: {source}", started=started)
            if not os.access(source, os.R_OK):
                return self._fail(source, base, ext, "none", f"文件不可读: {source}", started=started)
            if os.path.getsize(source) == 0:
                return self._fail(source, base, ext, "none", "文件为空（0 字节）", started=started)

        # 只有真正无后缀的 URL 才按 HTML 处理；带 .pdf/.png 等后缀的必须保留，
        # 否则二进制字节流会被当网页文本解析并误判为成功。
        if is_url and not ext:
            ext = ".html"
        if not ext:
            ext = ".txt"

        parser = self.registry.find(ext)
        if parser is None:
            return self._fail(source, base, ext, "none", f"不支持的文件类型: {ext}", started=started)

        tmp_path = None
        parse_path = source
        try:
            if is_url and ext not in URL_EXTS:
                # 非 HTML 的 URL 先下载到临时文件，再交给对应解析器；
                # 用 stream 分块落盘并限制总大小，避免大文件撑爆内存。
                try:
                    tmp_path = download_url_to_temp(source, ext, self.config)
                    parse_path = tmp_path
                except Exception as exc:
                    return self._fail(
                        source, base, ext, parser.name,
                        f"URL 下载失败: {type(exc).__name__}: {exc}", started=started,
                    )

            ctx = ParseContext(
                path=parse_path,
                file_name=base,
                ext=ext,
                file_type=ext,
                config=self.config,
                url_mode=is_url,
                options={"output_dir": output_dir},
            )

            try:
                content, elements, meta, warnings = parser.parse(ctx)
            except Exception as exc:
                return self._fail(
                    source, base, ext, parser.name,
                    f"{type(exc).__name__}: {exc}", started=started,
                )

            warnings = list(warnings or [])
            if not isinstance(content, str):
                return self._fail(
                    source, base, ext, parser.name,
                    f"解析器返回非法内容类型: {type(content).__name__}", warnings, started,
                )

            try:
                max_chars = int(self.config.get("parsing.max_content_chars", 0) or 0)
            except (TypeError, ValueError):
                return self._fail(
                    source, base, ext, parser.name,
                    "配置项 parsing.max_content_chars 必须是整数", warnings, started,
                )
            if max_chars < 0:
                return self._fail(
                    source, base, ext, parser.name,
                    "配置项 parsing.max_content_chars 不能为负数", warnings, started,
                )
            if max_chars and len(content) > max_chars:
                content = content[:max_chars]
                warnings.append(f"内容超过 max_content_chars={max_chars}，已截断")

            allow_empty = bool(self.config.get("parsing.allow_empty_content", False))
            if not content.strip() and not allow_empty:
                return self._fail(
                    source, base, ext, parser.name,
                    "解析结果为空（文件可能被损坏、无可提取文本，或所有解析路线均失败）",
                    warnings, started,
                )

            return ParseResult(
                success=True,
                file_path=source,
                file_name=base,
                file_type=ext,
                parser=parser.name,
                content=content,
                elements=elements,
                metadata=meta,
                warnings=warnings,
                elapsed_ms=int((time.time() - started) * 1000),
            )
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

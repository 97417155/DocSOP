"""零重依赖解析器：txt / md / code / html / ofd。"""

from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from typing import List

from .base import BaseParser, ParseContext, ParsePayload
from ..models import Element


class TextParser(BaseParser):
    """纯文本 / Markdown / 代码 / 日志。"""

    name = "text"
    supported_exts = (".txt", ".md", ".markdown", ".java", ".py", ".js", ".json", ".log", ".csv")

    def parse(self, ctx: ParseContext) -> ParsePayload:
        content = self.read_text(ctx.path, ctx.config)
        elements: List[Element] = []
        for line in content.splitlines():
            m = re.match(r"^(#{1,6})\s+(.*)", line)
            if m:
                elements.append(Element(type="heading", text=m.group(2).strip(), level=len(m.group(1))))
        return content, elements, {"encoding_hint": ctx.config.get("parsing.encoding", "auto")}, []


class HtmlParser(BaseParser):
    """HTML 文件 / 网页 URL -> Markdown。"""

    name = "html"
    supported_exts = (".html", ".htm")

    def parse(self, ctx: ParseContext) -> ParsePayload:
        warnings: List[str] = []
        if ctx.url_mode:
            try:
                import requests
                resp = requests.get(ctx.path, timeout=30)
                resp.raise_for_status()
                html = resp.text
            except Exception as exc:
                raise RuntimeError(f"网页抓取失败: {exc}") from exc
        else:
            html = self.read_text(ctx.path, ctx.config)

        # 优先 html2text，其次 bs4，最后标准库正则兜底
        content = None
        try:
            import html2text  # type: ignore
            h = html2text.HTML2Text()
            h.ignore_links = False
            content = h.handle(html)
        except ImportError:
            pass
        if content is None:
            try:
                from bs4 import BeautifulSoup  # type: ignore
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                content = soup.get_text(separator="\n")
            except ImportError:
                content = re.sub(r"<[^>]+>", "\n", html)
                content = re.sub(r"\n{3,}", "\n\n", content).strip()
        content = content or ""
        return content, [], {"url_mode": ctx.url_mode}, warnings


class OfdParser(BaseParser):
    """OFD 版式文件：解压后按页读取 Content.xml 文本。"""

    name = "ofd"
    supported_exts = (".ofd",)

    def parse(self, ctx: ParseContext) -> ParsePayload:
        warnings: List[str] = []
        texts: List[str] = []
        page_count = 0
        try:
            with zipfile.ZipFile(ctx.path, "r") as zf:
                page_files = sorted(
                    [n for n in zf.namelist() if re.search(r"Pages/Page_\d+/Content\.xml$", n)],
                    key=lambda n: int(re.search(r"Page_(\d+)", n).group(1)),
                )
                page_count = len(page_files)
                for pf in page_files:
                    root = ET.fromstring(zf.read(pf))
                    for elem in root.iter():
                        tag = elem.tag.split("}")[-1]
                        if tag in ("AbbreviatedData", "PhysicalBox"):
                            continue
                        if elem.text and elem.text.strip():
                            texts.append(" ".join(elem.text.strip().split()))
        except Exception as exc:
            raise RuntimeError(f"OFD 解析失败: {exc}") from exc
        return "\n".join(texts), [], {"page_count": page_count}, warnings

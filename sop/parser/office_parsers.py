"""Office 解析器：docx / pptx / xlsx / xls / 旧版 doc/ppt/wps。

重依赖（python-docx、python-pptx、pandas）按需加载，缺失时给出明确提示。
doc/ppt/wps 等旧格式通过 LibreOffice 转 PDF 后走 MinerU。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import List

from .base import BaseParser, ParseContext, ParsePayload
from .mineru_client import MineruClient
from ..models import Element


def md_table_cell(value) -> str:
    """把单元格内容规范成可安全放进 Markdown 表格的文本。

    - 换行会截断表格行，统一折叠为空格；
    - 未转义的 ``|`` 会被当成列分隔符，导致列错位，统一转义为 ``\\|``。
    """
    text = re.sub(r"\s*\r?\n\s*", " ", str(value if value is not None else ""))
    # 只转义尚未转义的竖线，避免把已有的 \| 二次转义
    return re.sub(r"(?<!\\)\|", r"\\|", text).strip()


class DocxParser(BaseParser):
    name = "docx"
    supported_exts = (".docx",)

    # 中英文标题样式名 → 层级；覆盖 Word/WPS 常见命名
    _CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
               "１": 1, "２": 2, "３": 3, "４": 4, "５": 5, "６": 6}

    @classmethod
    def _heading_level(cls, style_name: str) -> int:
        """从段落样式名推断标题层级；非标题返回 0。"""
        import re
        s = (style_name or "").strip()
        if not s:
            return 0
        low = s.lower()
        arabic = re.findall(r"\d+", s)
        if low.startswith("heading") or s.startswith("标题"):
            if arabic:
                return min(int(arabic[0]), 6)
            # 中文数字：标题一 / 标题 一
            for ch, lv in cls._CN_NUM.items():
                if ch in s:
                    return lv
            return 1
        return 0

    def parse(self, ctx: ParseContext) -> ParsePayload:
        strategy = str(ctx.config.get("parsing.docx_strategy", "lightweight") or "lightweight").lower()
        if strategy == "mineru":
            return self._via_mineru(ctx)
        return self._lightweight(ctx)

    def _lightweight(self, ctx: ParseContext) -> ParsePayload:
        """python-docx：段落 + 表格 -> Markdown；内嵌图片落盘并在原位插入链接。"""
        docx = self.require("docx", "解析 docx 需要 python-docx：pip install python-docx")
        try:
            doc = docx.Document(ctx.path)
        except Exception as exc:
            raise RuntimeError(
                f"docx 打开失败（可能是损坏的文件或旧版 .doc 伪装）: {exc}"
            ) from exc

        from docx.oxml.ns import qn

        BLIP_TAG = qn("a:blip")
        # VML 旧式图片；python-docx 未注册 v 前缀，直接用完整 URI
        VML_IMAGEDATA_TAG = "{urn:schemas-microsoft-com:vml}imagedata"
        R_EMBED = qn("r:embed")
        R_ID = qn("r:id")
        TEXTBOX_TAG = qn("w:txbxContent")
        W_P = qn("w:p")
        W_T = qn("w:t")

        from .image_store import ImageStore

        store = ImageStore(ctx.config, output_dir=str(ctx.options.get("output_dir") or ""))
        lines: List[str] = []
        elements: List[Element] = []
        images_meta: list = []
        warnings: list = []
        img_counter = [0]

        def scan_images(paragraph) -> list:
            """按文档顺序提取段落中的图片，返回 Markdown 链接列表。"""
            if not store.is_enabled():
                return []
            links: list = []
            seen_rids = set()
            for node in paragraph._p.iter():
                if node.tag == BLIP_TAG:
                    rid = node.get(R_EMBED)
                elif node.tag == VML_IMAGEDATA_TAG:
                    rid = node.get(R_ID)
                else:
                    continue
                if not rid or rid in seen_rids:
                    continue
                seen_rids.add(rid)
                part = doc.part.related_parts.get(rid)
                if part is None:
                    warnings.append(f"图片关系 {rid} 未找到，已跳过")
                    continue
                blob = getattr(part, "blob", b"") or b""
                info, err = store.save(
                    blob,
                    getattr(part, "content_type", ""),
                    str(getattr(part, "partname", "")),
                )
                if err:
                    warnings.append(err)
                    continue
                if not info:
                    continue
                images_meta.append(info)
                img_counter[0] += 1
                links.append(f"![图片{img_counter[0]}]({info['url']})")
            return links

        def textbox_text(paragraph) -> str:
            """提取段落中文本框的文字（python-docx 的 paragraph.text 不含文本框）。"""
            parts: list = []
            for box in paragraph._p.iter(TEXTBOX_TAG):
                for para in box.iter(W_P):
                    text = "".join(t.text or "" for t in para.iter(W_T)).strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts)

        def cell_text(cell) -> str:
            parts = [md_table_cell(cell.text)]
            cell_links: list = []
            for cp in cell.paragraphs:
                cell_links.extend(scan_images(cp))
                tb = textbox_text(cp)
                if tb:
                    parts.append(md_table_cell(tb))
            # 嵌套表格的文本不在 cell.text 中，扁平化后并入本单元格，避免内容丢失
            for nested in cell.tables:
                for row in nested.rows:
                    row_parts = []
                    for nc in row.cells:
                        row_parts.append(md_table_cell(nc.text))
                        for cp in nc.paragraphs:
                            cell_links.extend(scan_images(cp))
                    parts.append(" ".join(p for p in row_parts if p))
            txt = " ".join(p for p in parts if p)
            if cell_links:
                txt = (txt + " " if txt else "") + " ".join(cell_links)
            return txt

        # 按文档 body 的真实顺序交错处理段落与表格。
        # 不能用 doc.paragraphs + doc.tables 两个独立列表：那样会先输出全部段落、
        # 再把全部表格追加到末尾，导致表格位置错乱（例如表格本在第 4 块却被挪到文末）。
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph as DocxParagraph
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P

        for child in doc.element.body.iterchildren():
            if isinstance(child, CT_P):
                p = DocxParagraph(child, doc)
                text = p.text.strip()
                # 文本框内容不在 paragraph.text 中，按锚点段落原位补回
                tb_text = textbox_text(p)
                if tb_text:
                    text = (text + "\n" + tb_text).strip() if text else tb_text
                links = scan_images(p)
                if not text and not links:
                    continue
                style = (p.style.name if p.style else "") or ""
                level = self._heading_level(style)
                if text and level:
                    content = f"{'#' * level} {text}"
                else:
                    content = text
                if links:
                    content = (content + "\n" if content else "") + "\n".join(links)
                lines.append(content)
                elements.append(Element(
                    type="heading" if (text and level) else ("image" if (links and not text) else "text"),
                    text=content, level=level if text else 0,
                    metadata={"images": links} if links else {},
                ))
            elif isinstance(child, CT_Tbl):
                table = DocxTable(child, doc)
                grid = [[cell_text(c) for c in row.cells] for row in table.rows]
                if not grid:
                    continue
                width = max(len(r) for r in grid)
                grid = [r + [""] * (width - len(r)) for r in grid]
                md = ["| " + " | ".join(grid[0]) + " |", "|" + "|".join(["---"] * width) + "|"]
                md += ["| " + " | ".join(r) + " |" for r in grid[1:]]
                block = "\n".join(md)
                lines.append(block)
                elements.append(Element(type="table", text=block))

        meta = {
            "parser": "python-docx",
            "image_count": len(images_meta),
            "images": images_meta,
            "image_dir": store.dir,
            "image_dir_relative": store.dir_relative,
            "image_url_prefix": store.url_prefix,
        }
        return "\n\n".join(lines), elements, meta, warnings

    def _via_mineru(self, ctx: ParseContext) -> ParsePayload:
        """docx -> PDF(LibreOffice) -> MinerU，支持 bbox 溯源。"""
        pdf, tmpdir = convert_to_pdf(ctx, self)
        if not pdf:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise RuntimeError("docx 转 PDF 失败，无法走 MinerU，请检查 LibreOffice(soffice) 是否可用")
        try:
            from .mineru_result_adapter import adapt_middle_json
            result = MineruClient(ctx.config).parse(pdf)
            content = result.md_content or ""
            if not content:
                raise RuntimeError("MinerU 未返回 Markdown 内容")
            elements = adapt_middle_json(result.middle_json)
            return content, elements, {
                "parser": "mineru", "from": "docx",
                "traceable": any(e.bbox for e in elements),
                "element_count": len(elements),
            }, []
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class PptxParser(BaseParser):
    name = "pptx"
    supported_exts = (".pptx",)

    def parse(self, ctx: ParseContext) -> ParsePayload:
        pptx = self.require("pptx", "解析 pptx 需要 python-pptx：pip install python-pptx")
        try:
            prs = pptx.Presentation(ctx.path)
        except Exception as exc:
            raise RuntimeError(f"pptx 打开失败: {exc}") from exc

        from pptx.enum.shapes import MSO_SHAPE_TYPE
        from .image_store import ImageStore

        store = ImageStore(ctx.config, output_dir=str(ctx.options.get("output_dir") or ""))
        slides_blocks: List[str] = []
        elements: List[Element] = []
        images_meta: list = []
        warnings: list = []
        img_counter = [0]

        def save_picture(shape) -> str | None:
            """把图片 shape 落盘并返回 Markdown 链接；失败返回 None。"""
            if not store.is_enabled():
                return None
            try:
                blob = shape.image.blob
                content_type = getattr(shape.image, "content_type", "")
                partname = getattr(shape.image, "filename", "") or ""
            except Exception as exc:
                warnings.append(f"图片读取失败，已跳过: {exc}")
                return None
            info, err = store.save(blob, content_type, str(partname))
            if err:
                warnings.append(err)
                return None
            if not info:
                return None
            images_meta.append(info)
            img_counter[0] += 1
            return f"![图片{img_counter[0]}]({info['url']})"

        def iter_shapes(shapes):
            """递归展开组合形状，保证组合内的文字/表格/图片不被漏掉。"""
            for shape in shapes:
                try:
                    is_group = shape.shape_type == MSO_SHAPE_TYPE.GROUP
                except Exception:
                    is_group = False
                if is_group:
                    yield from iter_shapes(shape.shapes)
                else:
                    yield shape

        def table_to_md(table) -> str:
            grid = [[md_table_cell(cell.text) for cell in row.cells]
                    for row in table.rows]
            if not grid:
                return ""
            width = max(len(r) for r in grid)
            grid = [r + [""] * (width - len(r)) for r in grid]
            md = ["| " + " | ".join(grid[0]) + " |", "|" + "|".join(["---"] * width) + "|"]
            md += ["| " + " | ".join(r) + " |" for r in grid[1:]]
            return "\n".join(md)

        for sidx, slide in enumerate(prs.slides, 1):
            blocks: List[str] = []
            # slide.shapes 顺序即文档中的原始叠放顺序，直接按此顺序处理，
            # 保证文字/表格/图片按它们在幻灯片上的位置输出；组合形状递归展开，
            # 避免组合内的图片或表格被整体漏掉。
            for shape in iter_shapes(slide.shapes):
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    link = save_picture(shape)
                    if link:
                        blocks.append(link)
                        elements.append(Element(type="image", text=link,
                                                metadata={"images": [link], "slide": sidx}))
                    continue
                if getattr(shape, "has_table", False):
                    md = table_to_md(shape.table)
                    if md:
                        blocks.append(md)
                        elements.append(Element(type="table", text=md, metadata={"slide": sidx}))
                    continue
                if shape.has_text_frame:
                    text = shape.text_frame.text.strip()
                    if not text:
                        continue
                    # 保留文本框内多段落的原始行序
                    content = "\n".join(p.text.strip() for p in shape.text_frame.paragraphs
                                        if p.text.strip())
                    blocks.append(content)
                    elements.append(Element(type="text", text=content, metadata={"slide": sidx}))
            if blocks:
                slides_blocks.append(f"## 幻灯片 {sidx}\n" + "\n".join(blocks))

        meta = {
            "parser": "python-pptx",
            "slide_count": len(prs.slides),
            "image_count": len(images_meta),
            "images": images_meta,
            "image_dir": store.dir,
            "image_dir_relative": store.dir_relative,
            "image_url_prefix": store.url_prefix,
        }
        return "\n\n".join(slides_blocks), elements, meta, warnings


class ExcelParser(BaseParser):
    name = "excel"
    # .et 是 WPS 私有表格格式，pandas/openpyxl/xlrd 均无法直接读取；
    # 不注册该后缀，避免“声称支持但必然失败”，需要时先另存为 .xlsx。
    supported_exts = (".xlsx", ".xlsm", ".xls")

    def parse(self, ctx: ParseContext) -> ParsePayload:
        pd = self.require("pandas", "解析 Excel 需要 pandas + openpyxl/xlrd：pip install pandas openpyxl xlrd")
        try:
            try:
                book = pd.ExcelFile(ctx.path, engine="openpyxl")
            except Exception:
                book = pd.ExcelFile(ctx.path, engine="xlrd")
        except Exception as exc:
            raise RuntimeError(f"Excel 打开失败: {exc}") from exc

        blocks: List[str] = []
        elements: List[Element] = []
        try:
            sheet_names = list(book.sheet_names)
            for sheet in sheet_names:
                df = pd.read_excel(book, sheet_name=sheet)
                df = df.fillna("")
                header = "| " + " | ".join(md_table_cell(c) for c in df.columns) + " |"
                sep = "|" + "|".join(["---"] * len(df.columns)) + "|"
                rows = ["| " + " | ".join(md_table_cell(v) for v in row) + " |" for row in df.values]
                block = f"# {ctx.file_name}\n## {sheet}\n\n" + "\n".join([header, sep] + rows)
                blocks.append(block)
                elements.append(Element(type="table", text=block, metadata={"sheet": sheet}))
        finally:
            # pandas.ExcelFile 持有文件句柄，不关闭会导致 Windows 下文件被占用
            book.close()
        return "\n\n".join(blocks), elements, {"parser": "pandas", "sheet_count": len(sheet_names)}, []


class LegacyOfficeParser(BaseParser):
    """doc / ppt / wps / dps：LibreOffice -> PDF -> MinerU。"""

    name = "legacy_office"
    supported_exts = (".doc", ".ppt", ".wps", ".dps")

    def parse(self, ctx: ParseContext) -> ParsePayload:
        pdf, tmpdir = convert_to_pdf(ctx, self)
        if not pdf:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise RuntimeError("旧版 Office 转 PDF 失败，请确认已安装 LibreOffice(soffice)")
        try:
            from .mineru_result_adapter import adapt_middle_json
            result = MineruClient(ctx.config).parse(pdf)
            content = result.md_content or ""
            if not content:
                raise RuntimeError("MinerU 未返回 Markdown 内容")
            elements = adapt_middle_json(result.middle_json)
            return content, elements, {
                "parser": "mineru", "from": ctx.ext,
                "traceable": any(e.bbox for e in elements),
                "element_count": len(elements),
            }, []
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


def convert_to_pdf(ctx: ParseContext, parser: BaseParser):
    """调用 LibreOffice 将文件转 PDF。

    返回 ``(pdf路径或None, 临时目录)``；临时目录由调用方在解析结束后清理。
    """
    bin_name = ctx.config.get("converter.soffice_bin", "soffice")
    timeout = int(ctx.config.get("converter.timeout_seconds", 120))
    outdir = tempfile.mkdtemp(prefix="sop_convert_")
    try:
        done = subprocess.run(
            [bin_name, "--headless", "--convert-to", "pdf", ctx.path, "--outdir", outdir],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, outdir
    if done.returncode != 0:
        return None, outdir
    base = os.path.splitext(os.path.basename(ctx.path))[0]
    pdf = os.path.join(outdir, base + ".pdf")
    return (pdf if os.path.exists(pdf) else None), outdir

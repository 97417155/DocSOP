#!/usr/bin/env python3
"""测试用 mock soffice：把 docx 渲染成同内容的 PDF，用于验证高亮链路。

用法与真实 soffice 一致：
    mock_soffice.py --headless --convert-to pdf <input.docx> --outdir <dir>

与真实 LibreOffice 一样按文档 body 顺序渲染段落 / 表格 / 内嵌图片，
并保留逐字符坐标供 bbox 对齐使用。仅用于测试，不替代真实转换。
"""
from __future__ import annotations

import os
import sys

MARGIN_X = 50
PAGE_W, PAGE_H = 595, 842
BOTTOM = 800
BODY_FONT = 11
LINE_H = 20
TABLE_FONT = 9
TABLE_ROW_H = 26


def main() -> int:
    args = sys.argv[1:]
    try:
        src = args[args.index("--convert-to") + 2]
        outdir = args[args.index("--outdir") + 1]
    except (ValueError, IndexError):
        return 2

    try:
        import pymupdf as fitz
        import docx
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph as DocxParagraph
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.oxml.ns import qn
    except ImportError:
        return 3

    try:
        doc = docx.Document(src)
    except Exception:
        return 4

    BLIP = qn("a:blip")
    VML = "{urn:schemas-microsoft-com:vml}imagedata"
    R_EMBED = qn("r:embed")
    R_ID = qn("r:id")

    out = fitz.open()
    st = {"page": out.new_page(width=PAGE_W, height=PAGE_H), "y": 40.0}

    def new_page_if_needed(need: float):
        if st["y"] + need > BOTTOM:
            st["page"] = out.new_page(width=PAGE_W, height=PAGE_H)
            st["y"] = 40.0

    def put_text(value: str, size: int = BODY_FONT, indent: float = 0.0):
        value = (value or "").strip()
        if not value:
            return
        # 长段落按可显示宽度折行，避免超出页面（不截断内容）
        max_chars = max(8, int((PAGE_W - 2 * MARGIN_X - indent) / (size * 0.9)))
        chunks = [value[i:i + max_chars] for i in range(0, len(value), max_chars)] or [value]
        for line in chunks:
            new_page_if_needed(LINE_H)
            try:
                st["page"].insert_text((MARGIN_X + indent, st["y"]), line,
                                       fontsize=size, fontname="china-s")
            except Exception:
                st["page"].insert_text((MARGIN_X + indent, st["y"]), line, fontsize=size)
            st["y"] += LINE_H

    def put_image(blob: bytes):
        if not blob:
            return
        try:
            pix = fitz.Pixmap(blob)
            iw, ih = pix.width, pix.height
            max_w = PAGE_W - 2 * MARGIN_X
            if iw > max_w:
                ih = ih * max_w / iw
                iw = max_w
            need = ih + 12
            new_page_if_needed(need)
            rect = fitz.Rect(MARGIN_X, st["y"], MARGIN_X + iw, st["y"] + ih)
            st["page"].insert_image(rect, stream=blob)
            st["y"] += need
        except Exception:
            pass

    def paragraph_images(paragraph) -> list:
        blobs = []
        seen = set()
        for node in paragraph._p.iter():
            if node.tag == BLIP:
                rid = node.get(R_EMBED)
            elif node.tag == VML:
                rid = node.get(R_ID)
            else:
                continue
            if not rid or rid in seen:
                continue
            seen.add(rid)
            part = doc.part.related_parts.get(rid)
            if part is None:
                continue
            blob = getattr(part, "blob", b"") or b""
            if blob:
                blobs.append(blob)
        return blobs

    def put_table(table):
        grid = [[c.text.strip().replace("\n", " ") for c in row.cells]
                for row in table.rows]
        if not grid:
            return
        cols = max(len(r) for r in grid)
        grid = [r + [""] * (cols - len(r)) for r in grid]
        col_w = (PAGE_W - 2 * MARGIN_X) / cols
        for row in grid:
            new_page_if_needed(TABLE_ROW_H)
            y0, y1 = st["y"], st["y"] + TABLE_ROW_H
            for c_idx, val in enumerate(row):
                x0 = MARGIN_X + c_idx * col_w
                rect = fitz.Rect(x0, y0, x0 + col_w, y1)
                st["page"].draw_rect(rect, color=(0.6, 0.6, 0.6), width=0.5)
                if val:
                    try:
                        st["page"].insert_textbox(
                            fitz.Rect(x0 + 3, y0 + 6, x0 + col_w - 3, y1 - 3),
                            val, fontsize=TABLE_FONT, fontname="china-s",
                        )
                    except Exception:
                        pass
            st["y"] = y1
        st["y"] += 6

    # 按 body 真实顺序交错渲染段落 / 表格 / 图片
    for child in doc.element.body.iterchildren():
        if isinstance(child, CT_P):
            p = DocxParagraph(child, doc)
            text = p.text.strip()
            if text:
                put_text(text)
            for blob in paragraph_images(p):
                put_image(blob)
        elif isinstance(child, CT_Tbl):
            put_table(DocxTable(child, doc))

    os.makedirs(outdir, exist_ok=True)
    pdf_path = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
    out.save(pdf_path)
    out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

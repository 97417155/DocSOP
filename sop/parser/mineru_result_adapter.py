"""MinerU middle_json → 带 bbox/页码的 Element 列表适配器。

MinerU 各版本 middle_json 结构不完全一致，这里做兼容解析：
  - 1.x / 2.5：pdf_info 为 list，每页含 page_info + para_blocks
  - 3.x：results 内 body 的 middle_json 同样是 pdf_info 结构
  - loop 递归兜底：只要节点带 bbox + text 就提取，尽量多捞元素
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..models import Element

TYPE_MAP = {
    "text": "text", "paragraph": "text", "plain_text": "text",
    "title": "heading", "header": "heading", "interline_equation": "equation",
    "equation": "equation", "image": "image", "figure": "image",
    "table": "table", "table_caption": "text", "footnote": "text",
    "code": "code", "list": "text",
}


def adapt_middle_json(middle: Any) -> List[Element]:
    """把 MinerU middle_json 转为带溯源信息的 Element 列表。"""
    if not isinstance(middle, dict):
        return []
    pages = middle.get("pdf_info")
    if not isinstance(pages, list):
        # 兜底：整个 middle 当作一个 block
        return _walk(middle, 0)

    elements: List[Element] = []
    for page_idx, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        page_no = _page_no(page, page_idx)
        blocks = page.get("para_blocks") or page.get("blocks") or []
        for block in blocks if isinstance(blocks, list) else []:
            elements.extend(_block_to_elements(block, page_no))
    return elements


def _page_no(page: Dict[str, Any], fallback: int) -> int:
    info = page.get("page_info") or {}
    if isinstance(info, dict):
        for key in ("page_no", "page_number", "index"):
            v = info.get(key)
            if isinstance(v, int):
                # MinerU page_no 为 0-based，统一转为 1-based 页码
                if key in ("page_no", "index") and v < 10000:
                    return v + 1
                return v
    # 无 page_info 时用页序号（fallback 为 0-based）转 1-based
    return fallback + 1


def _block_to_elements(block: Any, page_no: int) -> List[Element]:
    if not isinstance(block, dict):
        return []
    bbox = _to_bbox(block.get("bbox"))
    btype = str(block.get("type", "")).lower()
    mapped = TYPE_MAP.get(btype, "text")

    # 直接从 spans 抽文本（MinerU 标准结构）
    texts = _spans_text(block)
    if texts:
        out: List[Element] = []
        for t in texts:
            if not t.strip():
                continue
            out.append(Element(
                type=mapped, text=t.strip(), bbox=list(bbox), page=page_no,
                metadata={"block_type": btype},
            ))
        return out
    # 递归兜底
    return _walk(block, page_no, default_type=mapped, default_bbox=bbox)


def _spans_text(block: Dict[str, Any]) -> List[str]:
    texts: List[str] = []
    for line in block.get("lines", []) or []:
        if not isinstance(line, dict):
            continue
        for span in line.get("spans", []) or []:
            if isinstance(span, dict) and isinstance(span.get("text"), str):
                texts.append(span["text"])
    if texts:
        return ["".join(texts)]
    return []


def _walk(node: Any, page_no: int, default_type: str = "text",
          default_bbox: List[float] | None = None) -> List[Element]:
    out: List[Element] = []
    if isinstance(node, dict):
        text = node.get("text")
        bbox = _to_bbox(node.get("bbox")) or default_bbox or []
        if isinstance(text, str) and text.strip():
            btype = str(node.get("type", default_type)).lower()
            out.append(Element(
                type=TYPE_MAP.get(btype, default_type), text=text.strip(),
                bbox=list(bbox), page=page_no,
            ))
        for v in node.values():
            out.extend(_walk(v, page_no, default_type, default_bbox))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk(v, page_no, default_type, default_bbox))
    return out


def _to_bbox(value: Any) -> List[float]:
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return [float(x) for x in value[:4]]
        except (TypeError, ValueError):
            return []
    return []

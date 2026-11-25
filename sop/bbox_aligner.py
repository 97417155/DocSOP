"""DOCX ↔ PDF 字符级 bbox 对齐。

链路：PDF 逐字符坐标提取 → 每页采样 → 全局 LCS 定页范围 → 局部 LCS 写回字符坐标
     → 按元素/块聚合包络矩形 {page: [[x0, y0, x1, y1]]}。

设计要点：
  - 依赖 PyMuPDF；缺失时由调用方降级，不抛到 CLI。
  - 全文超阈值直接跳过对齐（返回空），解析照常成功，仅无高亮坐标。
  - 字符采样 N=20 / M=10，降低 LCS 规模；超长文本再分段，避免 O(n*m) 爆内存。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# 页码 → 字符条目
CharMap = Dict[int, List[Dict[str, Any]]]


def _fitz():
    """优先 pymupdf，回退 fitz（两者 API 相同）。"""
    try:
        import pymupdf as fitz  # type: ignore
    except ImportError:  # pragma: no cover - 环境相关
        import fitz  # type: ignore
    return fitz


def extract_pdf_chars(pdf_path: str) -> CharMap:
    """提取 PDF 每页字符及其 bbox，页码 1-based。

    返回 {page_num: [{"page": int, "char": str, "bbox": [x0,y0,x1,y1]}, ...]}。
    """
    fitz = _fitz()
    doc = fitz.open(pdf_path)
    results: CharMap = {}
    try:
        for page_num, page in enumerate(doc, start=1):
            chars: List[Dict[str, Any]] = []
            for block in page.get_text("rawdict")["blocks"]:
                if "lines" not in block:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        for c in span["chars"]:
                            chars.append({
                                "page": page_num,
                                "char": c["c"],
                                "bbox": list(c["bbox"]),
                            })
            results[page_num] = chars
    finally:
        doc.close()
    return results


def sample_pdf_chars(pdf_chars_by_page: CharMap, N: int = 20, M: int = 10):
    """每页采样：前 N + 中段抽 M + 后 N，返回 (采样列表, 页起始索引映射)。"""
    sampled: List[Dict[str, Any]] = []
    page_index_map: Dict[int, int] = {}
    for page_num in sorted(pdf_chars_by_page.keys()):
        chars = pdf_chars_by_page[page_num]
        total = len(chars)
        if total <= 2 * N:
            # 短页全量采样：只取前 N 个会丢掉页尾字符，导致页范围判断偏窄、
            # 页尾内容无法命中坐标。
            first, last, mid = chars, [], []
        else:
            first, last, mid = chars[:N], chars[-N:], chars[N:-N]
        sampled_mid = []
        if mid:
            if len(mid) <= M:
                sampled_mid = mid
            else:
                step = max(1, len(mid) // M)
                sampled_mid = [mid[i] for i in range(0, len(mid), step)][:M]
        page_index_map[page_num] = len(sampled)
        sampled.extend(first)
        sampled.extend(sampled_mid)
        sampled.extend(last)
    return sampled, page_index_map


def lcs_alignment(s1: List[str], s2: List[str], chunk_limit: int = 4000) -> List[Tuple[int, int]]:
    """返回 s1/s2 最长公共子序列的索引对（升序）。

    对超长 s1 自动分段（每段 chunk_limit），分段对齐后按 s2 偏移拼接，
    避免 O(len(s1)*len(s2)) 的 DP 表爆内存；语义与整段 LCS 等价（采样对齐本身即近似）。
    """
    n, m = len(s1), len(s2)
    if not n or not m:
        return []
    if n <= chunk_limit:
        return _lcs_single(s1, s2)
    out: List[Tuple[int, int]] = []
    for start in range(0, n, chunk_limit):
        piece = s1[start:start + chunk_limit]
        out.extend((i + start, j) for i, j in _lcs_single(piece, s2))
    return out


def _lcs_single(s1: List[str], s2: List[str]) -> List[Tuple[int, int]]:
    """单段标准 LCS（DP + 回溯），调用方保证规模可控。"""
    n, m = len(s1), len(s2)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n):
        row, prev = dp[i + 1], dp[i]
        ci = s1[i]
        for j in range(m):
            row[j + 1] = prev[j] + 1 if ci == s2[j] else max(prev[j + 1], row[j])
    i, j = n, m
    alignment: List[Tuple[int, int]] = []
    while i > 0 and j > 0:
        if s1[i - 1] == s2[j - 1]:
            alignment.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    alignment.reverse()
    return alignment


def merge_bboxes(bboxes: List[List[float]]) -> List[float]:
    """把一组 bbox 合并为包络矩形。"""
    valid = [b for b in bboxes if b and any(b)]
    if not valid:
        return [0, 0, 0, 0]
    return [
        min(b[0] for b in valid), min(b[1] for b in valid),
        max(b[2] for b in valid), max(b[3] for b in valid),
    ]


def bbox_dict_from_chars(chars: List[Dict[str, Any]]) -> Dict[int, List[List[float]]]:
    """字符条目 → {page: [[包络 bbox]]}，页码 1-based，剔除 page=0。"""
    by_page: Dict[int, List[List[float]]] = {}
    for c in chars:
        page = int(c.get("page") or 0)
        if page <= 0:
            continue
        by_page.setdefault(page, []).append(list(c.get("bbox") or [0, 0, 0, 0]))
    return {p: [merge_bboxes(bs)] for p, bs in by_page.items() if bs}


def align_elements_to_pdf(
    element_texts: List[str],
    pdf_path: str,
    words_limit: int = 100000,
    N: int = 20,
    M: int = 10,
    per_element: bool = False,
):
    """把一段段文本（按原文顺序）对齐到 PDF 坐标。

    element_texts: 每个元素/块的原始文本，顺序即文档顺序。
    返回 (bbox, 失败原因)；失败时 bbox 为空但 reason 说明原因，不抛异常。

    默认 bbox 形状为 {page: [[x0,y0,x1,y1]]}（整体包络）。
    per_element=True 时改为返回 List[Dict[int, List[List[float]]]]，每个元素一份，
    便于把坐标精确写回各 Element / Chunk。
    """
    total = sum(len(t) for t in element_texts)
    if total <= 0:
        return {}, "无文本可对齐"
    if total >= words_limit:
        return {}, f"Document is too long for trace-back alignment: word_count={total} >= {words_limit}"

    try:
        pdf_chars = extract_pdf_chars(pdf_path)
    except Exception as exc:
        return {}, f"PDF 字符提取失败: {type(exc).__name__}: {exc}"

    sampled, page_index_map = sample_pdf_chars(pdf_chars, N=N, M=M)
    # s1: DOCX 字符序列；char_owner 记录每个字符属于哪个 element
    s1: List[str] = []
    char_owner: List[int] = []
    for idx, text in enumerate(element_texts):
        for ch in text:
            s1.append(ch)
            char_owner.append(idx)
    if not s1:
        return {}, "无字符可对齐"

    sampled_chars = [c["char"] for c in sampled]
    global_pairs = lcs_alignment(s1, sampled_chars)

    sorted_pages = sorted(page_index_map.keys())
    page_ranges: Dict[int, Tuple[int, int]] = {}
    for i, page in enumerate(sorted_pages):
        lo = page_index_map[page]
        hi = page_index_map[sorted_pages[i + 1]] if i + 1 < len(sorted_pages) else len(sampled_chars)
        positions = [a for a, b in global_pairs if lo <= b < hi]
        if positions:
            page_ranges[page] = (min(positions), max(positions) + 1)

    # 局部精细对齐：把 PDF 字符 bbox 写回命中的 s1 字符
    # matched[i] = (s1字符索引, 页码, bbox)，供逐元素/整体聚合
    matched: List[Tuple[int, int, List[float]]] = []
    cursor = 0
    for page in sorted(pdf_chars.keys()):
        if page not in page_ranges:
            continue
        page_chars = pdf_chars[page]
        _, end = page_ranges[page]
        end = max(end, cursor)
        local = lcs_alignment(s1[cursor:end], [c["char"] for c in page_chars])
        for a, b in local:
            ci = cursor + a
            if ci < len(s1):
                matched.append((ci, page, page_chars[b]["bbox"]))
        cursor = end

    if not matched:
        return ([] if per_element else {}), "字符对齐未命中任何坐标"

    if per_element:
        # 按字符归属元素聚合：每个元素 → {page: [[包络]]}
        per_elem_chars: List[List[Dict[str, Any]]] = [[] for _ in element_texts]
        for ci, page, bbox in matched:
            owner = char_owner[ci]
            if 0 <= owner < len(per_elem_chars):
                per_elem_chars[owner].append({"page": page, "bbox": bbox})
        return [bbox_dict_from_chars(chs) for chs in per_elem_chars], ""

    matched_chars = [{"page": page, "bbox": bbox} for _, page, bbox in matched]
    bbox = bbox_dict_from_chars(matched_chars)
    if not bbox:
        return {}, "字符对齐未命中任何坐标"
    return bbox, ""

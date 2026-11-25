"""把 Element 的 bbox/页码溯源信息关联到 Chunk。

策略：按文本子串匹配。对每个元素，找到其文本首次出现的分块，
把元素的 page / bbox / 元素索引挂到该块及其所有祖先块上，
这样检索命中子块时可回溯到父块的完整语境与原文坐标。
"""

from __future__ import annotations

import re
from typing import Dict, List

from ..models import Chunk, Element


def _norm(text: str) -> str:
    """归一化用于匹配的文本：去 markdown 标记与空白。"""
    s = re.sub(r"[#*`>|\-_\[\]()]", "", text or "")
    return re.sub(r"\s+", "", s)


def attach_provenance(chunks: List[Chunk], elements: List[Element]) -> int:
    """就地为每个 Chunk 填充 pages / bbox / source_element_ids。

    返回成功关联的元素数量（无 bbox 的元素也会被关联页码）。
    """
    if not elements:
        return 0
    norm_chunks = [_norm(c.text) for c in chunks]
    # 建立 元素文本 → 首个命中块索引，避免重复扫描
    matched = 0
    # 父块 → 子块映射（用于向上回溯）
    parent_of: Dict[str, str] = {c.chunk_id: c.parent_id for c in chunks if c.parent_id}
    index_of: Dict[str, int] = {c.chunk_id: i for i, c in enumerate(chunks)}
    # 优先在子块里找：父块包含全部正文，先匹配父块会让子块永远拿不到坐标。
    child_indices = [i for i, c in enumerate(chunks) if c.parent_id is not None]
    primary_indices = child_indices or list(range(len(chunks)))

    for eid, el in enumerate(elements):
        full_key = _norm(el.text)
        hit = -1
        # 长段落/表格可能被切到多个子块，完整前缀不一定落在任何单块内；
        # 逐级缩短前缀再匹配，保证长元素也能拿到最具体的一块坐标。
        for indices in (primary_indices, list(range(len(chunks)))):
            for key_len in (60, 40, 24, 12):
                key = full_key[:key_len]
                if len(key) < 4:
                    continue
                # 同级候选中取“包含该文本的最短块”，即最具体的一块。
                hit_len = 0
                for i in indices:
                    ctext = norm_chunks[i]
                    if key in ctext and (hit < 0 or len(ctext) < hit_len):
                        hit, hit_len = i, len(ctext)
                if hit >= 0:
                    break
            if hit >= 0:
                break
        if hit < 0:
            continue
        matched += 1
        # 命中块 + 沿 parent 链向上标注（父块覆盖子块内容）
        seen = {hit}
        cur = chunks[hit].chunk_id
        while cur in parent_of:
            pid = parent_of[cur]
            if pid in index_of:
                seen.add(index_of[pid])
                cur = pid
            else:
                break
        for idx in seen:
            c = chunks[idx]
            if el.page and el.page not in c.pages:
                c.pages.append(el.page)
            if el.bbox and not c.bbox:
                c.bbox = list(el.bbox)
            if eid not in c.source_element_ids:
                c.source_element_ids.append(eid)
    # 页码排序保证稳定
    for c in chunks:
        c.pages.sort()
    return matched

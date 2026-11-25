#!/usr/bin/env python3
"""统一 CLI 入口：给一个文件，输出解析内容 + 分块结果。

用法：
    python run.py <文件路径|URL> [--strategy parent_child] [--config 配置文件] [--out 输出目录]
    python run.py --list-strategies
    python run.py --list-exts
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sop import DocumentSOP, ChunkFactory  # noqa: E402
from sop.config_loader import Config  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="文档解析 SOP：解析文件并按策略分块")
    ap.add_argument("source", nargs="*", help="本地文件路径或 http(s) URL（支持多个）")
    ap.add_argument("--config", help="配置文件路径（.json/.yaml），默认自动加载")
    ap.add_argument("--strategy", help="分块策略，默认读配置（parent_child）")
    ap.add_argument("--out", help="输出目录，覆盖配置文件中的 output.dir")
    ap.add_argument("--preview", type=int, default=2, help="预览前 N 个分块，默认 2")
    ap.add_argument("--list-strategies", action="store_true", help="列出所有分块策略")
    ap.add_argument("--list-exts", action="store_true", help="列出支持的文件类型")
    ap.add_argument("--quiet", action="store_true", help="静默模式：不打印摘要且不保存结果文件（配合 --stdout 做管道）")
    ap.add_argument("--stdout", choices=["content", "chunks"], help="将解析全文或分块JSON直接输出到stdout")
    ap.add_argument("--workers", type=int, default=4, help="批量处理并发数，默认 4")
    ap.add_argument("--no-cache", action="store_true", help="禁用结果缓存，强制重新解析")
    # 分支路由 + 高亮链路
    ap.add_argument("--method", help="document_parsing_method：general/vision/no_hierarchical_structure_parse/_parse_pdf_with_bbox_highlight；传入即启用路由")
    ap.add_argument("--kb-id", default="default", help="知识库 ID（KB 输出用，默认 default）")
    ap.add_argument("--source-file-id", default="", help="源文件 ID（KB 输出用，默认文件名）")
    ap.add_argument("--library-id", default="", help="库 ID（KB 输出用）")
    args = ap.parse_args(argv)

    if args.list_strategies:
        print("可用分块策略: " + ", ".join(ChunkFactory.available()))
        return 0

    try:
        cfg = Config.load(args.config)
        sop = DocumentSOP(cfg)
    except Exception as exc:
        # 配置加载/构造阶段的错误也必须和解析失败一样可读，不能漏出裸 traceback
        sys.stderr.write(f"配置加载失败: {type(exc).__name__}: {exc}\n")
        return 1
    if args.list_exts:
        print("支持的文件类型: " + ", ".join(sop.parser.supported_extensions()))
        return 0

    if not args.source:
        ap.error("请提供文件路径或 URL")

    use_cache = not args.no_cache

    # 批量处理
    if len(args.source) > 1:
        results = sop.process_batch(
            args.source, strategy=args.strategy, save=not args.quiet,
            output_dir=args.out, workers=args.workers, use_cache=use_cache,
            method=args.method, kb_id=args.kb_id,
            source_file_id=args.source_file_id, library_id=args.library_id,
        )
        ok_count = sum(1 for r in results if r.ok)
        if args.quiet:
            return 0 if ok_count == len(results) else 1
        print(f"批量处理完成: {ok_count}/{len(results)} 成功, 并发={args.workers}")
        for r in results:
            p = r.parse
            status = "OK" if r.ok else "FAIL"
            chunks = len(r.chunk.chunks) if r.chunk else 0
            print(f"  [{status}] {p.file_name} | {len(p.content)}字 | {chunks}块"
                  + (f" | {p.error or r.error}" if (p.error or r.error) else ""))
        return 0 if ok_count == len(results) else 1

    result = sop.process(
        args.source[0], strategy=args.strategy, save=not args.quiet,
        output_dir=args.out, use_cache=use_cache,
        method=args.method, kb_id=args.kb_id,
        source_file_id=args.source_file_id, library_id=args.library_id,
    )
    p = result.parse

    if args.stdout in ("content", "chunks"):
        if not result.ok:
            sys.stderr.write(f"处理失败: {p.error or result.error}\n")
    if args.stdout == "content":
        sys.stdout.write(p.content)
        return 0 if result.ok else 1
    if args.stdout == "chunks":
        sys.stdout.write(result.chunk.to_json(ensure_ascii=False) if result.chunk else "{}")
        return 0 if result.ok else 1
    if args.quiet:
        return 0 if result.ok else 1

    print("=" * 60)
    print(f"配置文件: {getattr(cfg, 'path', '') or '内置默认'}")
    print(f"文件      : {p.file_name}")
    print(f"类型      : {p.file_type}   解析器: {p.parser}")
    print(f"解析成功  : {p.success}   耗时: {p.elapsed_ms} ms")
    if p.error:
        print(f"错误      : {p.error}")
    if result.error:
        print(f"错误      : {result.error}")
    for w in p.warnings:
        print(f"警告      : {w}")
    print(f"解析字符数: {len(p.content)}")
    if getattr(result, "route", None):
        rt = result.route
        print("-" * 60)
        print(f"解析路径  : {rt['document_parsing_method']}"
              + ("  [降级]" if rt.get("fallback") else ""))
        print(f"MinerU    : {'是' if rt.get('used_mineru') else '否'}"
              + (f" / {rt['mineru_backend_type']}" if rt.get("mineru_backend_type") else ""))
        print(f"高亮PDF   : {rt.get('highlight_pdf_path') or '无（未转换或未对齐）'}")
        if getattr(result, "kb_output", None):
            print(f"KB输出    : docs={len(result.kb_output.docs)} "
                  f"doc_chunks={len(result.kb_output.doc_chunks)}")
    print("=" * 60)

    if result.chunk:
        c = result.chunk
        print(f"分块策略  : {c.strategy}   总块数: {len(c.chunks)}")
        if c.metadata.get("parent_count"):
            print(f"父块/子块 : {c.metadata['parent_count']} / {c.metadata['child_count']}")
        for chunk in c.chunks[: args.preview]:
            head = chunk.text[:160].replace("\n", " ")
            extra = ""
            if chunk.parent_id:
                extra = f" [parent={chunk.parent_id}]"
            elif chunk.child_ids:
                extra = f" [children={len(chunk.child_ids)}]"
            print(f"  - {chunk.chunk_id} ({chunk.char_count}字){extra}: {head}...")
    print("=" * 60)
    for name, path in result.output_files.items():
        print(f"输出[{name}]: {path}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

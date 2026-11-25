"""顶层 SOP 编排：解析 -> 分块 -> 输出。

这就是"给一个文件，输出解析内容 + 按策略分块"的统一入口对象。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from .config_loader import Config
from .models import ChunkResult, ProcessResult
from .parser import DocumentParser
from .parser.dispatcher import safe_file_stem
from .chunking import ChunkFactory, attach_provenance
from .cache import ResultCache


class DocumentSOP:
    """文档解析 + 分块 一条龙。"""

    def __init__(self, config: Config):
        self.config = config
        self.parser = DocumentParser(config)
        self.cache = ResultCache(config)

    @classmethod
    def from_config(cls, path: Optional[str] = None) -> "DocumentSOP":
        return cls(Config.load(path))

    def _strategy_config(self, strategy: str) -> dict:
        cfg = dict(self.config.section(f"chunking.strategies.{strategy}"))
        # 注入长度度量配置（char/token），工厂会读取 _length_unit
        cfg["_length_unit"] = self.config.get("chunking.length_unit", "char")
        cfg["_token_encoding"] = self.config.get("chunking.token_encoding", "cl100k_base")
        return cfg

    def process(
        self,
        source: str,
        strategy: Optional[str] = None,
        save: bool = True,
        output_dir: Optional[str] = None,
        use_cache: bool = True,
        method: Optional[str] = None,
        kb_id: str = "default",
        source_file_id: str = "",
        library_id: str = "",
        task_version: str = "",
    ) -> ProcessResult:
        """完整处理一个文件：返回解析结果 + 分块结果。

        method：分支路由与高亮链路；
        仅在显式传 method（或配置 routing.enabled）时启用，默认保持原有行为。
        """
        # 配置值可能是非字符串（如误写数字），统一转字符串后再校验，避免裸 AttributeError
        raw_strategy = strategy if strategy is not None else self.config.get(
            "chunking.default_strategy", "parent_child"
        )
        strategy = str(raw_strategy).strip().lower()
        # 未知策略在解析前就拦下，返回可读错误，不让裸 traceback 冒到 CLI
        if strategy not in ChunkFactory.available():
            from .models import ParseResult
            error = f"未知分块策略: {raw_strategy}，可用策略: {', '.join(ChunkFactory.available())}"
            return ProcessResult(parse=ParseResult(
                success=False, file_path=str(source),
                file_name=os.path.basename(str(source)), file_type="",
                parser="none", error=error,
            ))

        # 是否启用分支路由
        routing_on = method is not None or bool(self.config.get("routing.enabled", False))
        if method is None:
            method = str(self.config.get("routing.default_method", "general"))

        # 路由模式禁用缓存：bbox/highlight 与输出目录、转换环境绑定，缓存会串目录
        use_cache = bool(use_cache and self.cache.enabled and not routing_on)
        cache_key = None
        if use_cache:
            cache_key = ResultCache.fingerprint(source, strategy, self.config)
            cached = self.cache.get(cache_key)
            if cached is not None:
                if save:
                    cached.output_files = self._save(cached, output_dir)
                return cached

        # 解析前的最终输出目录（绝对路径），用于内嵌图片落盘基准；
        # 与 _save 使用同一套解析规则，保证图片与文本落在同一目录树下。
        resolved_out = self._resolve_output_dir(output_dir)

        if routing_on:
            from .routing import ParsingRouter
            router = ParsingRouter(self.config)
            result = router.route(source, str(method), resolved_out)
            parse_result = result.parse
        else:
            result = None
            parse_result = self.parser.parse(source, output_dir=resolved_out)
        chunk_result: Optional[ChunkResult] = None
        result_error = ""

        if parse_result.success:
            started = time.time()
            try:
                chunker = ChunkFactory.create(strategy, self._strategy_config(strategy))
                chunks = chunker.chunk(parse_result.content, parse_result.elements, {
                    "source_file": parse_result.file_name,
                    "file_type": parse_result.file_type,
                })
                # 溯源关联：把 Element 的 bbox/页码挂到命中的块及其父块
                traced = attach_provenance(chunks, parse_result.elements) if parse_result.elements else 0
                chunk_result = ChunkResult(
                    strategy=strategy,
                    chunks=chunks,
                    metadata={
                        "total_chunks": len(chunks),
                        "parent_count": sum(1 for c in chunks if c.parent_id is None) if strategy == "parent_child" else 0,
                        "child_count": sum(1 for c in chunks if c.parent_id is not None) if strategy == "parent_child" else 0,
                        "traceable_elements": traced,
                    },
                    elapsed_ms=int((time.time() - started) * 1000),
                )
            except Exception as exc:
                # 分块器配置错误 / 内部异常也要作为可读失败结果返回，不向 CLI 抛裸 traceback
                result_error = f"分块失败（策略 {strategy}）: {type(exc).__name__}: {exc}"

        if result is None:
            result = ProcessResult(parse=parse_result, chunk=chunk_result, error=result_error)
        else:
            # 路由模式：复用已构造的 result（含 route/kb_context），补上分块
            result.chunk = chunk_result
            result.error = result_error

        # 路由模式生成 KB 结构
        if routing_on and result.parse.success:
            try:
                from .routing import ParsingRouter
                result.kb_output = ParsingRouter(self.config).build_kb(
                    result, chunk_result, kb_id=kb_id, source_file_id=source_file_id,
                    library_id=library_id, task_version=task_version,
                )
            except Exception as exc:
                # KB 是附加产物：生成失败降级为告警，不翻转已成功的解析结果
                result.parse.warnings.append(f"KB 结构生成失败: {type(exc).__name__}: {exc}")

        if cache_key:
            self.cache.put(cache_key, result)
        if save:
            result.output_files = self._save(result, output_dir, kb_mode=routing_on)
        return result

    def process_batch(
        self,
        sources: List[str],
        strategy: Optional[str] = None,
        save: bool = True,
        output_dir: Optional[str] = None,
        workers: int = 4,
        use_cache: bool = True,
        method: Optional[str] = None,
        kb_id: str = "default",
        source_file_id: str = "",
        library_id: str = "",
    ) -> List[ProcessResult]:
        """并发批量处理多个文件，返回结果与输入顺序一致。"""
        try:
            workers = max(1, int(workers))
        except (TypeError, ValueError):
            workers = 4
        results: List[Optional[ProcessResult]] = [None] * len(sources)
        # 同名文件（不同目录）批量处理时隔离到编号子目录，避免后者覆盖前者
        stems = [safe_file_stem(str(src))[0] for src in sources]
        counts: dict = {}
        for stem in stems:
            counts[stem] = counts.get(stem, 0) + 1

        def submit(src, i):
            sub_out = output_dir
            if counts.get(stems[i], 0) > 1:
                # 没有显式 --out 时同样要隔离，否则默认 output.dir 下后者会静默覆盖前者
                base_out = output_dir or self.config.get("output.dir", "sop_output")
                sub_out = os.path.join(base_out, f"{i:04d}")
            return self.process(
                src, strategy=strategy, save=save, output_dir=sub_out,
                use_cache=use_cache, method=method, kb_id=kb_id,
                source_file_id=source_file_id, library_id=library_id,
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(submit, src, i): i for i, src in enumerate(sources)}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:
                    # 单文件失败不影响整体，封装为失败结果
                    from .models import ParseResult
                    results[i] = ProcessResult(parse=ParseResult(
                        success=False, file_path=str(sources[i]),
                        file_name=os.path.basename(str(sources[i])), file_type="",
                        parser="none", error=f"{type(exc).__name__}: {exc}",
                    ))
        return [r for r in results if r is not None]

    # 便捷别名
    def parse_and_chunk(self, source: str, strategy: Optional[str] = None, **kw) -> ProcessResult:
        return self.process(source, strategy=strategy, **kw)

    def _resolve_output_dir(self, output_dir: Optional[str] = None) -> str:
        out = output_dir or self.config.get("output.dir", "sop_output")
        out = str(out)
        if not os.path.isabs(out):
            out = os.path.join(os.getcwd(), out)
        return os.path.normpath(out)

    def _save(self, result: ProcessResult, output_dir: Optional[str] = None,
              kb_mode: bool = False) -> dict:
        out = self._resolve_output_dir(output_dir)
        # 文件名做二次清洗：即使调用方直接构造了带路径的 file_name，也不会写出目标目录
        base = safe_file_stem(result.parse.file_name or "result")[0]
        try:
            os.makedirs(out, exist_ok=True)
        except Exception as exc:
            self._append_error(result, f"输出目录创建失败: {type(exc).__name__}: {exc}")
            return {}

        saved: dict = {}
        encoding = self.config.get("output.encoding", "utf-8") or "utf-8"

        # 缓存命中换输出目录时，文本会重写到新目录，但图片在图文中已存在磁盘上。
        # 依据 parse.metadata.images 的绝对路径，把图片同步复制到新目录的 images/ 下，
        # 保证 md 里的相对链接仍可达（按内容哈希命名，重名不覆盖冲突）。
        self._sync_images(result, out)

        def write(kind: str, filename: str, payload: str):
            path = os.path.join(out, filename)
            try:
                with open(path, "w", encoding=encoding) as f:
                    f.write(payload)
                saved[kind] = path
            except Exception as exc:
                self._append_error(result, f"{filename} 写入失败: {type(exc).__name__}: {exc}")

        if self.config.get("output.save_parsed_md", True) and result.parse.success:
            write("parsed_md", f"{base}.parsed.md", result.parse.content)
        if self.config.get("output.save_chunks_json", True) and result.chunk:
            write("chunks_json", f"{base}.chunks.json", result.chunk.to_json(ensure_ascii=False))

        include_elements = self.config.get("output.include_elements", False)

        # KB 模式：输出四个独立的集合文件（含 _engine_data）
        if kb_mode and getattr(result, "kb_output", None) is not None:
            kb = result.kb_output
            write("docs_json", f"{base}.docs.json",
                  json.dumps(kb.docs, ensure_ascii=False, indent=2))
            write("chunks_json_kb", f"{base}.doc_chunks.json",
                  json.dumps(kb.doc_chunks, ensure_ascii=False, indent=2))
            write("engine_meta_json", f"{base}._engine_meta.json",
                  json.dumps(kb.engine_meta, ensure_ascii=False, indent=2))
            write("engine_data_json", f"{base}._engine_data.json",
                  json.dumps(kb.engine_data, ensure_ascii=False, indent=2))

        # result.json 最后写：先登记自身路径，让文件内 output_files 与实际落盘产物一致
        result_json_name = f"{base}.result.json"
        result.output_files = {**saved, "result_json": os.path.join(out, result_json_name)}
        write("result_json", result_json_name,
              result.to_json(ensure_ascii=False, include_elements=include_elements,
                             include_route=kb_mode))
        result.output_files = saved
        return saved

    @staticmethod
    def _sync_images(result: ProcessResult, out: str) -> None:
        """把 parse.metadata.images 中记录的图片复制到新输出目录。

        仅在图片尚未存在于目标目录时复制；源缺失则跳过（不阻断文本输出）。
        """
        import shutil
        images = (result.parse.metadata or {}).get("images") or []
        if not images:
            return
        raw_dir = str(result.parse.metadata.get("image_dir_relative") or "images")
        target_dir = raw_dir if os.path.isabs(raw_dir) else os.path.join(out, raw_dir)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError:
            return
        for info in images:
            if not isinstance(info, dict):
                continue
            src = info.get("path") or ""
            name = info.get("file_name") or ""
            if not src or not name or not os.path.isfile(src):
                continue
            dst = os.path.join(target_dir, name)
            if os.path.abspath(src) == os.path.abspath(dst) or os.path.exists(dst):
                continue
            try:
                shutil.copyfile(src, dst)
            except OSError:
                continue

    @staticmethod
    def _append_error(result: ProcessResult, message: str) -> None:
        result.error = f"{result.error} | {message}" if result.error else message

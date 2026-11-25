"""document_parsing_method 分支路由 + 前端高亮链路。

docx 四分支（字符串包含匹配，顺序即优先级）：
  1. 含 no_hierarchical_structure_parse → headers_recovering（条件转换，阈值可配）
     失败 → 降级 _parse_pdf_with_bbox_highlight
  2. 含 _parse_pdf_with_bbox_highlight → 转 PDF → MinerU，必须转换
     失败 → 降级 _parse_docx_fallback
  3. 含 general / 未传（默认）→ bbox 溯源（条件转换，含字数/LibreOffice/超时三重门槛）
     失败 → 降级 _parse_docx_fallback
  4. 其他任意值（含 vision）→ _parse_docx_fallback（docx2md + 规则切片，无 PDF 无 box）

关键语义：
  - 降级不改变解析成功：解析照常出内容，只是 highlight_pdf_path 为空、box 为空。
  - _engine_data.document_parsing_method 会被改写为实际走的方法；_engine_meta._fallback=True。
  - execution_trace 记录 used_mineru / mineru_backend_type / 实际方法。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config_loader import Config
from .kb_models import EngineMeta, KBOutput, box_to_json_str, make_chunk_unique_id
from .models import Element, Chunk, ChunkResult, ParseResult, ProcessResult


# 分支常量
METHOD_RECOVERING = "no_hierarchical_structure_parse"
METHOD_PDF_BBOX = "_parse_pdf_with_bbox_highlight"
METHOD_GENERAL = "general"
METHOD_DOCX_FALLBACK = "_parse_docx_fallback"
METHOD_RECOVERING_ACTUAL = "_parse_docx_with_recovering_headers"
METHOD_BBOX_ACTUAL = "_parse_docx_with_bbox_highlight"


@dataclass
class RouteOutcome:
    """单条解析路线的产物。"""

    content: str
    elements: List[Element] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    error: str = ""                     # 致命错误：非空表示该路线彻底失败
    highlight_pdf_path: str = ""
    used_mineru: bool = False
    mineru_backend_type: str = ""
    traceable: bool = False


class ParsingRouter:
    """根据 document_parsing_method 分派解析，并生成知识库风格输出。"""

    def __init__(self, config: Config):
        self.config = config

    # ---------------- 公共 API ----------------

    def route(self, source: str, method: str, output_dir: str) -> ProcessResult:
        """执行分支路由，返回解析+分块结果；KB 输出挂在 result.kb_output。"""
        # method 可能由 API 调用方传非字符串（CLI 已保证字符串），统一转字符串，
        # 避免后面 .lower() 抛裸 AttributeError。
        method = str(method) if method is not None else METHOD_GENERAL
        # 与通用解析保持一致：非字符串/空 source 返回可读失败，不让裸 TypeError 穿出
        if not isinstance(source, str) or not source.strip():
            err = f"无效的源文件路径: {source!r}"
            parse = ParseResult(success=False, file_path=str(source), file_name="",
                                file_type="", parser="none", error=err)
            result = ProcessResult(parse=parse)
            result.route = {"document_parsing_method": method or METHOD_GENERAL,
                            "fallback": False, "used_mineru": False,
                            "mineru_backend_type": "", "highlight_pdf_path": ""}
            return result
        # URL（尤其带 .docx/.pdf 后缀的）必须先下载到本地临时文件，
        # 否则四分支会拿 URL 字符串去开 docx/转 PDF，必然失败。
        local_path = source
        tmp_path = None
        is_url = source.lower().startswith(("http://", "https://"))
        if is_url:
            from .parser.dispatcher import URL_EXTS, download_url_to_temp, safe_file_stem
            _, url_ext = safe_file_stem(source, is_url=True)
            url_ext = url_ext.lower() or ".html"
            if url_ext not in URL_EXTS:
                try:
                    local_path = download_url_to_temp(source, url_ext, self.config)
                    tmp_path = local_path
                except Exception as exc:
                    parse = ParseResult(success=False, file_path=source,
                                        file_name=os.path.basename(source), file_type=url_ext,
                                        parser="none", error=f"URL 下载失败: {type(exc).__name__}: {exc}")
                    result = ProcessResult(parse=parse)
                    result.route = {"document_parsing_method": method or METHOD_GENERAL,
                                    "fallback": False, "used_mineru": False,
                                    "mineru_backend_type": "", "highlight_pdf_path": ""}
                    return result

        try:
            ext = os.path.splitext(local_path)[1].lower()
            if ext == ".docx":
                outcome, actual_method, fallback = self._route_docx(local_path, method, output_dir)
            else:
                # 非 docx 走原有统一解析（PDF/image 等仍走其自身策略）
                outcome, actual_method, fallback = self._route_generic(local_path, method, output_dir)
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        # 构造 ParseResult：任何路线都没拿到内容（或带致命错误）时必须判失败，
        # 与通用解析的失败语义保持一致，不能因路由包装而把失败误判为成功。
        has_content = bool(outcome.content and outcome.content.strip())
        if not has_content and not outcome.error:
            # 无内容且无显式错误：把告警中的原因合并为 error，便于上层与 CLI 定位
            causes = [w for w in outcome.warnings if w]
            fatal_error = "；".join(causes) if causes else "所有解析路线均未产生内容"
        else:
            fatal_error = outcome.error
        parse = ParseResult(
            success=has_content and not outcome.error, file_path=source,
            file_name=os.path.basename(source), file_type=ext,
            parser=outcome.metadata.get("parser", actual_method),
            content=outcome.content, elements=outcome.elements,
            metadata=outcome.metadata, warnings=outcome.warnings,
            error=fatal_error,
        )
        # 溯源信息写入 engine_data
        engine_data = {
            "document_parsing_method": actual_method,
            "used_mineru": outcome.used_mineru,
            "mineru_backend_type": outcome.mineru_backend_type,
            "traceable": outcome.traceable,
        }
        result = ProcessResult(parse=parse)
        result.route = {
            "document_parsing_method": actual_method,
            "fallback": fallback,
            "used_mineru": outcome.used_mineru,
            "mineru_backend_type": outcome.mineru_backend_type,
            "highlight_pdf_path": outcome.highlight_pdf_path,
        }
        result.kb_context = {
            "source": source,
            "method": actual_method,
            "fallback": fallback,
            "outcome": outcome,
            "engine_data": engine_data,
        }
        return result

    def build_kb(
        self,
        result: ProcessResult,
        chunk_result: Optional[ChunkResult],
        kb_id: str = "default",
        source_file_id: str = "",
        library_id: str = "",
        task_version: str = "",
    ) -> KBOutput:
        """基于解析结果与分块结果构造 docs / doc_chunks / _engine_meta。"""
        ctx = getattr(result, "kb_context", {}) or {}
        outcome: RouteOutcome = ctx.get("outcome") or RouteOutcome(content="")
        method = ctx.get("method", "")
        fallback = bool(ctx.get("fallback", False))
        source = ctx.get("source", "")

        # chunks：优先用实际分块结果；无分块时按原样整篇一块
        if chunk_result and chunk_result.chunks:
            chunks: List[Chunk] = chunk_result.chunks
        else:
            chunks = [Chunk(
                chunk_id="chunk_00000", text=result.parse.content,
                index=0, metadata={"source_file": result.parse.file_name},
            )]

        is_url = str(source).lower().startswith(("http://", "https://"))
        if is_url:
            from urllib.parse import urlsplit, urlunsplit
            parts = urlsplit(str(source))
            # 去掉 query/fragment：签名 token 等不应进入入库记录
            file_label = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            stem = os.path.basename(parts.path.rstrip("/")) or "document"
        else:
            file_label = os.path.basename(str(source).replace("\\", "/"))
            stem = file_label
        source_file_id = source_file_id or stem

        # doc_id 稳定：基于脱敏后的源标识 + 内容，不掺入本机绝对路径，
        # 同一文件换目录/换机器重复入库仍得到同一个 doc_id。
        import hashlib
        doc_id = hashlib.md5(f"{file_label}|{result.parse.content}".encode("utf-8")).hexdigest()[:24]

        from .kb_models import _timestamp_str
        now = _timestamp_str()

        doc = DocRecordLike(
            doc_id=doc_id,
            file_path=file_label,
            whole_content=result.parse.content,
            chunk_count=len(chunks),
            highlight_pdf_path=outcome.highlight_pdf_path,
            kb_id=kb_id, source_file_id=source_file_id,
            task_version=task_version, created_at=now, updated_at=now,
            para_elements_count=len(outcome.elements),
            para_headers_md=self._headers_markdown(outcome.elements),
            library_id=library_id,
        ).record

        chunk_records = []
        for i, c in enumerate(chunks):
            box = self._chunk_box(c, outcome)
            chunk_records.append({
                "doc_id": doc_id,
                "chunk_index": i,
                "text": c.text,
                "box_json_str": box_to_json_str(box),
                "chunk_unique_id": make_chunk_unique_id(source_file_id, i),
                "kb_id": kb_id,
                "created_at": now,
            })

        meta = EngineMeta(
            document_parsing_method=method, _fallback=fallback,
            used_mineru=outcome.used_mineru,
            mineru_backend_type=outcome.mineru_backend_type,
            mineru_version=str(self.config.get("mineru.version", "")),
            started_at=now, ended_at=now,
        )
        meta.add_trace("document_parser", method)

        return KBOutput(
            docs=[doc.to_dict()],
            doc_chunks=chunk_records,
            engine_meta=meta.to_dict(),
            engine_data=ctx.get("engine_data", {}),
        )

    # ---------------- docx 分支 ----------------

    def _route_docx(self, source: str, method: str, output_dir: str) -> Tuple[RouteOutcome, str, bool]:
        method = method or METHOD_GENERAL
        low = method.lower()

        if METHOD_RECOVERING in low:
            outcome, ok = self._parse_recovering(source, output_dir)
            if ok:
                return outcome, METHOD_RECOVERING_ACTUAL, False
            # 失败 → 降级 _parse_pdf_with_bbox_highlight
            fb, _ = self._parse_pdf_bbox(source, output_dir)
            return fb, METHOD_PDF_BBOX, True

        if METHOD_PDF_BBOX in low:
            outcome, ok = self._parse_pdf_bbox(source, output_dir)
            if ok:
                return outcome, METHOD_PDF_BBOX, False
            fb = self._parse_docx_fallback(source, output_dir)
            return fb, METHOD_DOCX_FALLBACK, True

        if METHOD_GENERAL in low:
            outcome, ok = self._parse_docx_bbox(source, output_dir)
            if ok:
                return outcome, METHOD_BBOX_ACTUAL, False
            fb = self._parse_docx_fallback(source, output_dir)
            return fb, METHOD_DOCX_FALLBACK, True

        # else：含 vision 等任意值 → 最朴素 fallback
        return self._parse_docx_fallback(source, output_dir), METHOD_DOCX_FALLBACK, False

    # ---------------- 各路线实现 ----------------

    def _parse_docx_fallback(self, source: str, output_dir: str) -> RouteOutcome:
        """docx2md 风格：轻量解析，无 PDF、无 box。"""
        from .parser.office_parsers import DocxParser
        from .parser.base import ParseContext

        try:
            ctx = ParseContext(path=source, file_name=os.path.basename(source),
                               ext=".docx", file_type=".docx", config=self.config,
                               options={"output_dir": output_dir})
            content, elements, meta, warnings = DocxParser()._lightweight(ctx)
            return RouteOutcome(content=content, elements=elements,
                                metadata={**meta, "parser": "docx2md_fallback"},
                                warnings=warnings)
        except Exception as exc:
            return RouteOutcome(content="", warnings=[f"docx fallback 失败: {exc}"])

    def _convert_docx_to_pdf(self, source: str, output_dir: str) -> Tuple[str, str]:
        """LibreOffice 转 PDF。返回 (pdf_path, 失败原因)，成功时 reason 为空。

        三重门槛：LibreOffice 可用 / 转换成功 / 超时控制。
        """
        bin_name = str(self.config.get("converter.soffice_bin", "soffice") or "soffice")
        # 同时兼容 soffice 与 libreoffice 两种可执行文件命名
        exe = shutil.which(bin_name) or shutil.which("libreoffice") or shutil.which("soffice")
        if not exe:
            return "", "LibreOffice (soffice) is not installed or not in PATH"
        try:
            timeout = int(self.config.get("converter.timeout_seconds", 120))
        except (TypeError, ValueError):
            timeout = 120
        outdir = os.path.join(output_dir, "_highlight")
        try:
            os.makedirs(outdir, exist_ok=True)
            done = subprocess.run(
                [exe, "--headless", "--convert-to", "pdf", source, "--outdir", outdir],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return "", f"PDF 转换超时（{timeout}s）"
        except Exception as exc:
            return "", f"PDF 转换失败: {type(exc).__name__}: {exc}"
        if done.returncode != 0:
            return "", f"PDF 转换失败: {(done.stderr or '').strip()[:200]}"
        pdf = os.path.join(outdir, os.path.splitext(os.path.basename(source))[0] + ".pdf")
        if not os.path.exists(pdf):
            return "", "PDF 转换未生成输出文件"
        return pdf, ""

    def _parse_docx_bbox(self, source: str, output_dir: str) -> Tuple[RouteOutcome, bool]:
        """general 默认路线：轻量解析 + 条件转 PDF + 字符级对齐。

        三重门槛：字数 < words_limit / LibreOffice 存在 / 转换成功。
        任一不满足 → 解析仍成功，只是 box 为空、highlight_pdf_path 为空。
        """
        from .parser.office_parsers import DocxParser
        from .parser.base import ParseContext
        from .bbox_aligner import align_elements_to_pdf

        try:
            ctx = ParseContext(path=source, file_name=os.path.basename(source),
                               ext=".docx", file_type=".docx", config=self.config,
                               options={"output_dir": output_dir})
            content, elements, meta, warnings = DocxParser()._lightweight(ctx)
        except Exception as exc:
            return RouteOutcome(content="", warnings=[f"docx 解析失败: {exc}"]), False

        words_limit = self._words_limit("general")
        # 字数门槛前置：超限直接返回元素，不转换 PDF、不设 highlight，
        # 与文档声明一致，也避免对超长文档白跑一次可能长达 120s 的转换。
        if len(content) >= words_limit:
            warnings.append(
                f"Document is too long for trace-back alignment: word_count={len(content)} >= {words_limit}"
            )
            return RouteOutcome(
                content=content, elements=elements,
                metadata={**meta, "parser": "docx_with_bbox", "traceable": False},
                warnings=warnings,
            ), True

        pdf, why = self._convert_docx_to_pdf(source, output_dir)
        highlight = ""
        if pdf:
            try:
                per_bbox, reason = align_elements_to_pdf(
                    [e.text for e in elements], pdf, words_limit=words_limit,
                    per_element=True,
                )
                hit = 0
                if not reason and per_bbox:
                    for e, eb in zip(elements, per_bbox):
                        if eb:
                            first_page = sorted(eb.keys())[0]
                            e.bbox = list(eb[first_page][0])
                            if not e.page:
                                e.page = first_page
                            hit += 1
                if reason:
                    warnings.append(reason)
                elif hit:
                    warnings.append(f"bbox 对齐命中 {hit}/{len(elements)} 个元素")
                # 只有真正命中坐标才声明可溯源 / 暴露 highlight PDF；
                # 对齐失败时 PDF 虽已转换，但没有任何框可画，上报 highlight 会误导下游。
                if hit:
                    highlight = pdf
                else:
                    warnings.append("bbox 未命中任何坐标，本次不提供高亮 PDF")
            except Exception as exc:
                warnings.append(f"bbox 对齐失败: {type(exc).__name__}: {exc}")
        else:
            warnings.append(why)

        return RouteOutcome(
            content=content, elements=elements,
            metadata={**meta, "parser": "docx_with_bbox", "traceable": bool(highlight)},
            warnings=warnings, highlight_pdf_path=highlight,
            traceable=bool(highlight),
        ), True

    def _parse_recovering(self, source: str, output_dir: str) -> Tuple[RouteOutcome, bool]:
        """no_hierarchical_structure_parse：docx 元素 + MinerU 恢复标题层级。

        条件转换阈值 15 万字（配置 routing.recovering_words_limit）。
        本 SOP 用轻量解析 + MinerU（若启用）恢复层级，失败则降级。
        """
        from .parser.office_parsers import DocxParser
        from .parser.base import ParseContext

        try:
            ctx = ParseContext(path=source, file_name=os.path.basename(source),
                               ext=".docx", file_type=".docx", config=self.config,
                               options={"output_dir": output_dir})
            content, elements, meta, warnings = DocxParser()._lightweight(ctx)
        except Exception as exc:
            return RouteOutcome(content="", warnings=[f"headers recovering 失败: {exc}"]), False

        # 阈值判断：超过阈值跳过 MinerU 对齐，直接用解析结果（降级语义保留）
        limit = self._words_limit("recovering")
        if len(content) >= limit:
            warnings.append(f"Document is too long for trace-back alignment: word_count={len(content)} >= {limit}")
            return RouteOutcome(content=content, elements=elements, metadata=meta, warnings=warnings), True

        used_mineru = False
        backend = ""
        mineru_on = bool(self.config.get("mineru.enabled", False))
        if mineru_on and self.config.get("mineru.base_url", ""):
            try:
                from .parser.mineru_client import MineruClient
                result = MineruClient(self.config).parse(source)
                if result.md_content and result.md_content.strip():
                    content = result.md_content
                    used_mineru = True
                    backend = result.backend or "pipeline"
                    elements = []
                    warnings.append("已用 MinerU 恢复标题层级")
            except Exception as exc:
                warnings.append(f"MinerU 恢复层级失败，使用 docx 元素: {exc}")

        # traceable 取决于是否真有元素坐标可回溯，而不是是否用了 MinerU：
        # MinerU 恢复层级后 elements 被清空，没有元素就没有溯源信息。
        return RouteOutcome(
            content=content, elements=elements,
            metadata={**meta, "parser": "docx_headers_recovering"},
            warnings=warnings, used_mineru=used_mineru, mineru_backend_type=backend,
            traceable=any(getattr(e, "bbox", None) and any(e.bbox) for e in elements),
        ), True

    def _parse_pdf_bbox(self, source: str, output_dir: str) -> Tuple[RouteOutcome, bool]:
        """docx → PDF → MinerU，必须转换；失败时返回失败 outcome 供上层降级。"""
        pdf, why = self._convert_docx_to_pdf(source, output_dir)
        if not pdf:
            return RouteOutcome(content="", warnings=[why]), False
        try:
            from .parser.mineru_client import MineruClient
            from .parser.mineru_result_adapter import adapt_middle_json
            result = MineruClient(self.config).parse(pdf)
            content = result.md_content or ""
            if not content.strip():
                return RouteOutcome(content="", warnings=["MinerU 未返回内容"]), False
            elements = adapt_middle_json(result.middle_json)
            traceable = any(getattr(e, "bbox", None) and any(e.bbox) for e in elements)
            return RouteOutcome(
                content=content, elements=elements,
                metadata={"parser": "mineru_pdf", "from": "docx"},
                used_mineru=True, mineru_backend_type=result.backend or "pipeline",
                # 没有可画框的元素就不把无框 PDF 当作高亮产物上报
                highlight_pdf_path=pdf if traceable else "",
                traceable=traceable,
            ), True
        except Exception as exc:
            return RouteOutcome(content="", warnings=[f"MinerU PDF 解析失败: {exc}"]), False

    def _route_generic(self, source: str, method: str, output_dir: str) -> Tuple[RouteOutcome, str, bool]:
        """非 docx：走原有统一解析，PDF 按 enable_vlm 选择后端。"""
        from .parser.dispatcher import DocumentParser

        dp = DocumentParser(self.config)
        parse = dp.parse(source, output_dir=output_dir)
        if not parse.success:
            return RouteOutcome(content="", warnings=[parse.error]), parse.parser or "none", False
        # 以实际使用的解析器为准，而不是配置开关：MinerU 不可达时会降级到 pypdf，
        # 此时不能因为 mineru.enabled=true 就谎报 used_mineru / 不标记 fallback。
        parser_name = parse.parser or ""
        used_mineru = parser_name in ("mineru", "mineru_ocr")
        backend = (parse.metadata or {}).get("mineru_backend", "") if used_mineru else ""
        # 配置了 MinerU 却没用上，说明走了内部降级
        mineru_exts = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
        wanted_mineru = parse.file_type in mineru_exts \
            and bool(self.config.get("mineru.enabled", False))
        fell_back = bool(wanted_mineru and not used_mineru)
        warnings = list(parse.warnings)
        if fell_back:
            warnings.append("MinerU 不可用，已降级到本地解析器")
        # 有元素不等于可溯源：文本/标题元素没有坐标，仍应标记 traceable=false
        traceable = any(
            getattr(e, "bbox", None) and any(e.bbox) for e in (parse.elements or [])
        )
        return RouteOutcome(
            content=parse.content, elements=parse.elements,
            metadata=parse.metadata, warnings=warnings,
            used_mineru=used_mineru, mineru_backend_type=backend,
            traceable=traceable, error="",
        ), parser_name or "generic", fell_back

    # ---------------- 工具 ----------------

    def _words_limit(self, kind: str) -> int:
        key = "routing.general_words_limit" if kind == "general" else "routing.recovering_words_limit"
        try:
            return int(self.config.get(key, 100000 if kind == "general" else 150000))
        except (TypeError, ValueError):
            return 100000 if kind == "general" else 150000

    @staticmethod
    def _headers_markdown(elements: List[Element]) -> str:
        """生成标题层级 Markdown。"""
        lines = []
        for el in elements:
            if el.type == "heading" and el.text.strip():
                level = max(1, min(6, el.level or 1))
                lines.append(f"{'#' * level} {el.text.strip().lstrip('#').strip()}")
        return "\n".join(lines)

    @staticmethod
    def _chunk_box(chunk: Chunk, outcome: RouteOutcome) -> Dict[int, List[List[float]]]:
        """按 chunk 的溯源元素聚合 bbox，形状为 {page: [[x0, y0, x1, y1]]}：
        每页一个并集包络矩形。
        """
        if not outcome.elements or not chunk.source_element_ids:
            # 没有命中任何元素时不要拿全文元素兜底：那会把“整篇包络”错标成该块的
            # 坐标，前端高亮会覆盖整页。宁可不给框，也不给错误框。
            return {}
        ids = chunk.source_element_ids
        picked = [outcome.elements[i] for i in ids if 0 <= i < len(outcome.elements)]
        from .bbox_aligner import merge_bboxes
        by_page: Dict[int, List[List[float]]] = {}
        for el in picked:
            if getattr(el, "bbox", None) and any(el.bbox):
                page = el.page or 1
                by_page.setdefault(page, []).append(list(el.bbox))
        return {p: [merge_bboxes(bs)] for p, bs in by_page.items() if bs}


class DocRecordLike:
    """轻量构造 DocRecord，避免在路由模块重复导入 dataclass 细节。"""

    def __init__(self, **kwargs):
        from .kb_models import DocRecord
        self.record = DocRecord(**kwargs)

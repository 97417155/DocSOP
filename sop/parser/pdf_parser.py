"""PDF 解析器：默认走 MinerU，本地 pypdf 作为降级。"""

from __future__ import annotations

from typing import List

from .base import BaseParser, ParseContext, ParsePayload
from .mineru_client import MineruClient
from .mineru_result_adapter import adapt_middle_json


def _fail_with_history(message: str, warnings: List[str]) -> None:
    """降级链最终失败时，把此前各路失败原因并入 error，避免诊断信息丢失。"""
    if warnings:
        message = f"{message}（前置尝试: {' | '.join(warnings)}）"
    raise RuntimeError(message)


class PdfParser(BaseParser):
    name = "pdf"
    supported_exts = (".pdf",)

    def parse(self, ctx: ParseContext) -> ParsePayload:
        strategy = str(ctx.config.get("parsing.pdf_strategy", "mineru") or "mineru").lower()
        warnings: List[str] = []
        mineru_on = bool(ctx.config.get("mineru.enabled", True))

        # 只有显式启用 MinerU 时才尝试，避免 disabled 时产生误导性失败警告
        if strategy == "mineru" and mineru_on:
            try:
                client = MineruClient(ctx.config)
                result = client.parse(ctx.path)
                content = result.md_content or ""
                if not content.strip():
                    # 空结果视为本次路线失败，继续走本地降级
                    warnings.append("MinerU 未返回 Markdown 内容，尝试本地降级")
                else:
                    elements = adapt_middle_json(result.middle_json)
                    meta = {
                        "mineru_version": result.version,
                        "mineru_backend": result.backend,
                        "mineru_work_dir": result.work_dir,
                        "parser": "mineru",
                        "traceable": bool(elements),
                        "element_count": len(elements),
                    }
                    return content, elements, meta, warnings
            except Exception as exc:
                warnings.append(f"MinerU 解析失败，尝试本地降级: {exc}")
        elif strategy == "mineru" and not mineru_on:
            warnings.append("配置 mineru.enabled=false，已跳过 MinerU，使用本地解析")
        elif strategy not in ("mineru", "local"):
            warnings.append(f"未知 pdf_strategy={strategy}，使用本地 pypdf 解析")

        # 本地降级：pypdf
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as exc:
            _fail_with_history(
                "本地 PDF 降级需要 pypdf：pip install pypdf；或配置并启动 MinerU 服务",
                warnings,
            )
            raise  # pragma: no cover - _fail_with_history 必抛
        try:
            reader = PdfReader(ctx.path)
            if reader.is_encrypted:
                try:
                    reader.decrypt("")
                except Exception:
                    pass
            parts = [p.extract_text() or "" for p in reader.pages]
            return "\n\n".join(parts), [], {"parser": "pypdf", "page_count": len(parts)}, warnings
        except Exception as exc:
            _fail_with_history(f"本地 PDF 解析失败: {exc}", warnings)
            raise  # pragma: no cover


class ImageParser(BaseParser):
    """图片：MinerU OCR（默认）或可选视觉模型。"""

    name = "image"
    supported_exts = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")

    def parse(self, ctx: ParseContext) -> ParsePayload:
        import base64
        warnings: List[str] = []

        vision = ctx.config.section("parsing.vision")
        if vision.get("enabled") and vision.get("base_url"):
            try:
                import requests
                with open(ctx.path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                # 按真实后缀给出 MIME，不能把 PNG/WebP 一律标成 image/jpeg
                ext = (ctx.ext or "").lower().lstrip(".")
                mime = {
                    "png": "image/png", "gif": "image/gif", "bmp": "image/bmp",
                    "webp": "image/webp", "tif": "image/tiff", "tiff": "image/tiff",
                }.get(ext, "image/jpeg")
                headers: dict = dict(vision.get("headers") or {})
                api_key = vision.get("api_key") or ""
                if api_key:
                    headers.setdefault("Authorization", f"Bearer {api_key}")
                resp = requests.post(
                    vision["base_url"],
                    json={
                        "model": vision.get("model_name", "qwen2-vl"),
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": "请识别并输出图片中的全部文字内容"},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ]}],
                    },
                    headers=headers,
                    timeout=int(vision.get("timeout_seconds", 120)),
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("视觉模型未返回文字内容")
                return content, [], {"parser": "vision", "model": vision.get("model_name")}, warnings
            except Exception as exc:
                warnings.append(f"视觉模型识别失败，转 MinerU OCR: {exc}")

        mineru_on = bool(ctx.config.get("mineru.enabled", True))
        if not mineru_on:
            detail = " | ".join(warnings) if warnings else "视觉模型未配置（parsing.vision.base_url 为空）"
            raise RuntimeError(f"图片识别不可用：MinerU 未启用，且 {detail}")
        try:
            client = MineruClient(ctx.config)
            result = client.parse(ctx.path)
            content = result.md_content or ""
            if not content.strip():
                raise RuntimeError("MinerU OCR 未返回文字内容")
            return content, [], {"parser": "mineru_ocr", "backend": result.backend}, warnings
        except Exception as exc:
            _fail_with_history(f"图片 OCR 失败: {exc}", warnings)
            raise  # pragma: no cover

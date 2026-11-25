"""基于文件指纹的解析/分块结果缓存。

缓存键 = 文件绝对路径 + 内容指纹（本地 size/mtime_ns；URL 的 ETag/Last-Modified 或内容哈希）
         + 分块策略 + 影响结果的完整配置段签名；
命中时直接反序列化 ProcessResult，跳过解析与分块。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional

from .models import ProcessResult, ParseResult, ChunkResult, Chunk, Element


class ResultCache:
    def __init__(self, config):
        self.enabled = bool(config.get("cache.enabled", False))
        try:
            self.ttl = int(config.get("cache.ttl_seconds", 86400))
        except (TypeError, ValueError) as exc:
            raise ValueError("配置项 cache.ttl_seconds 必须是整数") from exc
        try:
            self.dir = config.resolve_path(config.get("cache.dir", "_work/cache"))
            if self.enabled:
                os.makedirs(self.dir, exist_ok=True)
        except Exception as exc:
            raise ValueError(f"配置项 cache.dir 非法: {exc}") from exc

    @staticmethod
    def url_signature(url: str, config) -> str:
        """获取 URL 内容签名：优先响应头，缺失时下载内容计算哈希。

        这样网页更新（即使 URL 不变）也会让缓存键变化，避免一直返回旧解析结果。
        """
        timeout = max(1, int(config.get("cache.url_check_timeout_seconds", 10) or 10))
        try:
            import requests  # type: ignore
        except ImportError:
            return "requests-unavailable"

        header_sig = ""
        try:
            resp = requests.head(url, allow_redirects=True, timeout=timeout)
            h = resp.headers
            header_sig = "|".join(str(h.get(k) or "") for k in (
                "ETag", "Last-Modified", "Content-Length",
            ))
        except Exception:
            header_sig = ""
        if header_sig.strip("|"):
            return header_sig

        try:
            digest = hashlib.md5()
            with requests.get(url, stream=True, allow_redirects=True, timeout=timeout) as resp:
                resp.raise_for_status()
                for block in resp.iter_content(65536):
                    if block:
                        digest.update(block)
            return digest.hexdigest()
        except Exception as exc:
            # 探测失败时给出稳定但不匹配任何正常签名的标记，避免误命中旧缓存
            return f"unavailable:{type(exc).__name__}"

    @staticmethod
    def fingerprint(source: str, strategy: str, config, url_signature: str | None = None) -> str:
        """计算缓存键。本地文件按内容指纹；URL 按远端内容签名。"""
        is_url = str(source).lower().startswith(("http://", "https://"))
        if is_url:
            ident = str(source)
            stat = url_signature if url_signature is not None else ResultCache.url_signature(str(source), config)
        else:
            ident = os.path.abspath(str(source))
            if os.path.isfile(ident):
                st = os.stat(ident)
                stat = f"{st.st_size}|{st.st_mtime_ns}"
            else:
                stat = "missing"

        # 影响解析/分块结果的完整配置段：chunking 全量参数（含各策略）、
        # parsing、mineru、converter、images。任何一项变化都会自动失效缓存。
        impact = {
            "chunking": config.section("chunking"),
            "parsing": config.section("parsing"),
            "mineru": config.section("mineru"),
            "converter": config.section("converter"),
            "images": config.section("images"),
        }
        sig = json.dumps(impact, sort_keys=True, ensure_ascii=False, default=str)
        raw = f"{ident}|{stat}|{strategy}|{sig}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> str:
        return os.path.join(self.dir, f"{key}.json")

    def get(self, key: str) -> Optional[ProcessResult]:
        if not self.enabled:
            return None
        p = self._path(key)
        if not os.path.exists(p):
            return None
        if self.ttl > 0 and time.time() - os.path.getmtime(p) > self.ttl:
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return self._deserialize(json.load(f))
        except Exception:
            return None

    def put(self, key: str, result: ProcessResult):
        if not self.enabled or not result.ok:
            return
        p = self._path(key)
        tmp = f"{p}.{os.getpid()}.tmp"
        try:
            # 先写临时文件再原子替换：并发批量处理同一文件时不会留下半截 JSON
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._serialize(result), f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    @staticmethod
    def _serialize(result: ProcessResult) -> dict:
        return {
            "created_at": result.created_at,
            "parse": result.parse.to_dict(include_elements=True),
            "chunk": result.chunk.to_dict() if result.chunk else None,
            "output_files": result.output_files,
            "error": result.error,
        }

    @staticmethod
    def _deserialize(data: dict) -> ProcessResult:
        pd = data.get("parse", {})
        elements = [Element(**e) for e in pd.get("elements", [])]
        pd["elements"] = elements
        parse = ParseResult(**{k: v for k, v in pd.items() if k in ParseResult.__dataclass_fields__})
        chunk = None
        cd = data.get("chunk")
        if cd:
            chunks = []
            for c in cd.get("chunks", []):
                c = dict(c)
                c.pop("page", None)  # property，非字段
                chunks.append(Chunk(**{k: v for k, v in c.items() if k in Chunk.__dataclass_fields__}))
            chunk = ChunkResult(
                strategy=cd.get("strategy", ""), chunks=chunks,
                metadata=cd.get("metadata", {}), elapsed_ms=cd.get("elapsed_ms", 0),
            )
        return ProcessResult(
            parse=parse, chunk=chunk, output_files=data.get("output_files", {}),
            error=data.get("error", ""),
            created_at=int(data.get("created_at") or time.time()),
        )

"""内嵌图片存储：把解析器提取到的图片字节落盘，并生成可访问的链接。

设计要点：
- 配置驱动：目录与链接前缀都来自 ``images.*``，与输出目录解耦；
  默认目录放在默认输出目录下（sop_output/images），配合默认 url_prefix
  （images/）即可开箱即用；对接静态服务时把 url_prefix 改为完整 URL。
- 内容寻址：文件名取字节 MD5 前 12 位，天然去重且内容变化自动换名。
- 失败不影响解析：任何写盘/超限问题都返回 None + 原因，由解析器收集为 warning。
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Optional, Tuple

_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/x-icon": ".ico",
    "image/x-emf": ".emf",
    "image/x-wmf": ".wmf",
}


class ImageStore:
    """按内容哈希保存图片，返回统一的文件信息与链接。"""

    def __init__(self, config, output_dir: str = ""):
        self.config = config
        self.enabled = bool(config.get("images.enabled", True))
        try:
            self.max_bytes = int(config.get("images.max_bytes", 104857600) or 0)
        except (TypeError, ValueError):
            self.max_bytes = 104857600
        self.url_prefix = str(config.get("images.url_prefix", "images/") or "")
        raw_dir = str(config.get("images.dir", "images") or "images")
        self.dir_relative = raw_dir  # 原始配置值（相对子目录名或绝对路径）
        # 相对目录相对于实际输出目录解析，保证 md 里的相对链接可达；
        # 绝对路径视为用户显式固定的图片仓库，直接采用。
        if os.path.isabs(raw_dir):
            self.dir = os.path.normpath(raw_dir)
        elif output_dir:
            self.dir = os.path.normpath(os.path.join(output_dir, raw_dir))
        else:
            self.dir = config.resolve_path(raw_dir)
        self._seen: Dict[str, Dict[str, Any]] = {}
        self._init_error = ""
        if self.enabled:
            try:
                os.makedirs(self.dir, exist_ok=True)
            except (OSError, ValueError) as exc:
                # 目录不可创建不应打断文本解析：后续 save() 返回可读原因，
                # 由解析器收集为 warning，正文照常输出。
                self._init_error = f"图片目录创建失败: {type(exc).__name__}: {exc}"

    def is_enabled(self) -> bool:
        return self.enabled

    def info(self) -> Dict[str, Any]:
        """供 ParseResult.metadata 使用的目录级信息。"""
        return {
            "enabled": self.enabled,
            "dir": self.dir,
            "dir_relative": self.dir_relative,
            "url_prefix": self.url_prefix,
        }

    def save(self, blob: bytes, content_type: str = "", source_hint: str = "") -> Tuple[Optional[Dict[str, Any]], str]:
        """保存一张图片。

        返回 ``(info, error)``：成功时 error 为空；跳过/失败时 info 为 None。
        相同字节只写一次，重复引用返回同一条 info（dedup=True）。
        """
        if not self.enabled:
            return None, ""
        if self._init_error:
            return None, self._init_error
        if not blob:
            return None, "图片字节为空"
        if self.max_bytes and len(blob) > self.max_bytes:
            return None, f"图片超过 images.max_bytes={self.max_bytes}（实际 {len(blob)}），已跳过"

        digest = hashlib.md5(blob).hexdigest()
        if digest in self._seen:
            info = dict(self._seen[digest])
            info["dedup"] = True
            return info, ""

        ext = _MIME_EXT.get((content_type or "").lower().split(";")[0].strip(), "")
        if not ext:
            # 从来源文件名兜底推断后缀
            hint = (source_hint or "").lower()
            for candidate in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
                if hint.endswith(candidate):
                    ext = ".jpg" if candidate == ".jpeg" else candidate
                    break
        if not ext:
            ext = ".img"

        file_name = f"img_{digest[:12]}{ext}"
        path = os.path.join(self.dir, file_name)
        url = (self.url_prefix.rstrip("/") + "/" + file_name) if self.url_prefix else file_name
        try:
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(blob)
        except Exception as exc:
            return None, f"图片写入失败: {type(exc).__name__}: {exc}"

        info: Dict[str, Any] = {
            "file_name": file_name,
            "path": path,
            "url": url,
            "bytes": len(blob),
            "content_type": content_type,
            "md5": digest,
            "source": source_hint,
            "dedup": False,
        }
        self._seen[digest] = info
        return dict(info), ""

    def saved(self) -> list:
        """已成功保存的图片信息（去重后）。"""
        return [dict(v) for v in self._seen.values()]

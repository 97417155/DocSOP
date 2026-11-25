"""MinerU HTTP 客户端（从配置加载服务器地址）。

支持 1.x / 2.5 / 3.x 三种协议，为独立实现，仅依赖 requests + 标准库。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Optional

from ..config_loader import Config


@dataclass
class MineruResult:
    md_content: Optional[str]
    middle_json: Optional[dict]
    backend: Optional[str]
    file_name: str
    version: str
    work_dir: str
    raw: Any = None


class MineruClient:
    """从配置读取 MinerU 服务器信息并发起解析。"""

    def __init__(self, config: Config):
        def pos_int(key: str, default: int) -> int:
            try:
                value = int(config.get(f"mineru.{key}", default))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"配置项 mineru.{key} 必须是正整数") from exc
            if value <= 0:
                raise ValueError(f"配置项 mineru.{key} 必须是正整数，当前值: {value}")
            return value

        def non_neg_int(key: str, default: int) -> int:
            try:
                value = int(config.get(f"mineru.{key}", default))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"配置项 mineru.{key} 必须是非负整数") from exc
            if value < 0:
                raise ValueError(f"配置项 mineru.{key} 必须是非负整数，当前值: {value}")
            return value

        self.config = config
        self.enabled = bool(config.get("mineru.enabled", True))
        self.version = str(config.get("mineru.version", "1.x")).strip().lower()
        self.base_url = str(config.get("mineru.base_url", "")).rstrip("/")
        self.parse_url = config.get("mineru.parse_url") or (
            f"{self.base_url}/file_parse" if self.base_url else ""
        )
        self.download_url = config.get("mineru.download_url") or (
            f"{self.base_url}/download_folder" if self.base_url else ""
        )
        self.v3_base_url = str(config.get("mineru.v3_base_url", "")).rstrip("/")
        self.backend = config.get("mineru.consume_backend", "auto")
        self.parse_method = config.get("mineru.parse_method", "auto")
        self.poll_interval = pos_int("task_poll_interval_seconds", 2)
        self.task_timeout = pos_int("task_timeout_seconds", 1800)
        self.file_timeout = pos_int("file_parse_timeout_seconds", 600)
        # 重试与鉴权
        self.max_retries = non_neg_int("max_retries", 2)
        try:
            self.retry_backoff = max(0.0, float(config.get("mineru.retry_backoff_seconds", 1.0)))
        except (TypeError, ValueError) as exc:
            raise ValueError("配置项 mineru.retry_backoff_seconds 必须是非负数") from exc
        self.headers: dict = dict(config.get("mineru.headers", {}) or {})
        api_key = config.get("mineru.api_key", "")
        if api_key:
            self.headers.setdefault("Authorization", f"Bearer {api_key}")
        work_dir = config.get("mineru.work_dir", "_work/mineru")
        self.work_dir = config.resolve_path(work_dir)
        # 目录延后到真正需要缓存产物时创建，避免仅构造客户端就产生副作用

    def _request(self, method: str, url: str, **kwargs):
        """统一 HTTP 请求：注入鉴权头 + 失败重试（指数退避）。"""
        import requests
        kwargs.setdefault("headers", {})
        kwargs["headers"] = {**(self.headers or {}), **(kwargs.get("headers") or {})}
        kwargs.setdefault("timeout", self.file_timeout)
        last_exc = None
        for attempt in range(self.max_retries + 1):
            # 重试 POST 上传时必须把文件句柄拨回开头，否则第二次发送的是空内容
            files = kwargs.get("files")
            entries = files.values() if isinstance(files, dict) else (files if isinstance(files, list) else [])
            for file_entry in entries:
                if isinstance(file_entry, (tuple, list)) and len(file_entry) > 1:
                    file_obj = file_entry[1]
                    if hasattr(file_obj, "seek"):
                        try:
                            file_obj.seek(0)
                        except Exception:
                            pass
            try:
                resp = requests.request(method, url, **kwargs)
                # 4xx（除 429 限流）属于请求本身问题，重试没有意义，直接抛出
                resp.raise_for_status()
                return resp
            except requests.exceptions.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", 0)
                if 400 <= status < 500 and status != 429:
                    raise
                last_exc = exc
            except Exception as exc:
                last_exc = exc
            if attempt < self.max_retries:
                time.sleep(self.retry_backoff * (2 ** attempt))
        raise last_exc  # type: ignore

    @staticmethod
    def _content_fingerprint(file_path: str) -> str:
        """基于文件名+大小+mtime 的短指纹，避免同名不同内容命中错误缓存。"""
        st = os.stat(file_path)
        raw = f"{os.path.basename(file_path)}|{st.st_size}|{st.st_mtime_ns}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]

    def _cache_dir(self, file_path: str) -> tuple:
        """返回 (缓存目录, base_name)。缓存目录带内容指纹，base_name 保留原文件名。"""
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        out = os.path.join(self.work_dir, f"{base_name}_{self._content_fingerprint(file_path)}")
        os.makedirs(out, exist_ok=True)
        return out, base_name

    def parse(self, file_path: str) -> MineruResult:
        """解析 PDF / 图片，返回统一结构。"""
        if not self.enabled:
            raise RuntimeError("MinerU 在配置中未启用（mineru.enabled=false）")
        if self.version != "3.x" and not (self.base_url or self.parse_url):
            raise RuntimeError("MinerU 已启用但未配置 mineru.base_url（或 mineru.parse_url）")
        try:
            import requests  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("MinerU 客户端需要 requests：pip install requests") from exc

        if self.version == "3.x":
            return self._parse_v3(file_path)
        if self.version == "2.5":
            return self._parse_v25(file_path)
        return self._parse_legacy(file_path)

    # ---------- 1.x ----------
    def _parse_legacy(self, file_path: str) -> MineruResult:
        import requests
        out, base_name = self._cache_dir(file_path)
        # 服务端按 folder_name 隔离产物：同名不同内容的文件不再互相覆盖
        params = {"folder_name": f"{base_name}_{self._content_fingerprint(file_path)}"}

        # 先尝试缓存
        ok = False
        try:
            resp = self._request("GET", self.download_url, params=params, timeout=60, stream=True)
            ctype = (resp.headers.get("Content-Type", "") or "").lower()
            # 只接受真正的产物类型：HTML 错误页/登录页不能当解析结果缓存
            ok = resp.status_code == 200 and (
                "zip" in ctype or "markdown" in ctype or "text/plain" in ctype or "octet-stream" in ctype
            )
            if ok:
                self._persist(resp, out)
        except Exception:
            ok = False

        if not ok:
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f, "application/pdf")}
                parse_params = {
                    "parse_method": self.parse_method,
                    "is_json_md_dump": "true",
                    "output_dir": "output",
                }
                r = self._request("POST", self.parse_url, params=parse_params, files=files, timeout=self.file_timeout)
                r.raise_for_status()
            resp = self._request("GET", self.download_url, params=params, timeout=60, stream=True)
            resp.raise_for_status()
            self._persist(resp, out)

        return self._read_result(out, base_name)

    # ---------- 2.5 ----------
    def _parse_v25(self, file_path: str) -> MineruResult:
        import requests
        out, base_name = self._cache_dir(file_path)
        data = {
            "parse_method": self.parse_method,
            "return_md": "true",
            "return_middle_json": "true",
            "response_format_zip": "true",
        }
        if self.backend and self.backend != "auto":
            data["backend"] = self.backend
        with open(file_path, "rb") as f:
            files = {"files": (os.path.basename(file_path), f, "application/pdf")}
            resp = self._request("POST", self.parse_url, data=data, files=files, timeout=self.file_timeout, stream=True)
        resp.raise_for_status()
        self._persist(resp, out)
        return self._read_result(out, base_name)

    # ---------- 3.x ----------
    def _parse_v3(self, file_path: str) -> MineruResult:
        import requests
        if not self.v3_base_url:
            raise RuntimeError("配置缺少 mineru.v3_base_url")
        out, base_name = self._cache_dir(file_path)
        data = {
            "parse_method": self.parse_method,
            "return_md": "true",
            "return_middle_json": "true",
            "response_format_zip": "true",
        }
        if self.backend and self.backend != "auto":
            data["backend"] = self.backend
        with open(file_path, "rb") as f:
            files = {"files": (os.path.basename(file_path), f, "application/pdf")}
            resp = self._request("POST", f"{self.v3_base_url}/tasks", data=data, files=files, timeout=60)
        resp.raise_for_status()
        task_id = resp.json().get("task_id")
        if not task_id:
            raise RuntimeError("MinerU 3.x 未返回 task_id")

        deadline = time.time() + self.task_timeout
        while time.time() < deadline:
            st = self._request("GET", f"{self.v3_base_url}/tasks/{task_id}", timeout=30)
            st.raise_for_status()
            status = st.json().get("status")
            if status == "completed":
                result = self._request("GET", f"{self.v3_base_url}/tasks/{task_id}/result", timeout=60, stream=True)
                result.raise_for_status()
                if "json" in result.headers.get("Content-Type", ""):
                    payload = result.json()
                    inner = payload.get("results", {})
                    if not isinstance(inner, dict) or not inner:
                        raise RuntimeError("MinerU 3.x 任务完成但 results 为空")
                    fname, body = next(iter(inner.items()))
                    if not isinstance(body, dict):
                        raise RuntimeError("MinerU 3.x results 条目格式非法")
                    return MineruResult(
                        md_content=body.get("md_content"),
                        middle_json=body.get("middle_json"),
                        backend=(body.get("middle_json") or {}).get("_backend") if isinstance(body.get("middle_json"), dict) else None,
                        file_name=fname, version=self.version, work_dir=out, raw=payload,
                    )
                self._persist(result, out)
                return self._read_result(out, base_name)
            if status == "failed":
                raise RuntimeError(f"MinerU 3.x 任务失败: {st.json().get('error')}")
            time.sleep(self.poll_interval)
        raise TimeoutError(f"MinerU 3.x 任务超时（{self.task_timeout}s）")

    # ---------- 工具 ----------
    def _persist(self, response, out_dir: str) -> None:
        ctype = response.headers.get("Content-Type", "")
        if "zip" in ctype:
            zp = os.path.join(out_dir, "downloaded.zip")
            try:
                with open(zp, "wb") as f:
                    for chunk in response.iter_content(8192):
                        f.write(chunk)
                with zipfile.ZipFile(zp, "r") as zf:
                    dest = os.path.realpath(out_dir)
                    for member in zf.infolist():
                        target = os.path.realpath(os.path.join(out_dir, member.filename))
                        # 防止恶意/异常压缩包通过 ../ 或绝对路径写出缓存目录（Zip Slip）
                        if target != dest and not target.startswith(dest + os.sep):
                            raise RuntimeError(f"压缩包包含非法路径，已拒绝解压: {member.filename}")
                    zf.extractall(out_dir)
            finally:
                if os.path.exists(zp):
                    os.remove(zp)
        elif "json" in ctype:
            payload = response.json()
            fname = os.path.basename(str(payload.get("file_name") or "result"))
            if not fname or fname in (".", ".."):
                fname = "result"
            if payload.get("md_content"):
                with open(os.path.join(out_dir, f"{fname}.md"), "w", encoding="utf-8") as f:
                    f.write(payload["md_content"])
            if payload.get("middle_json") is not None:
                with open(os.path.join(out_dir, f"{fname}_middle.json"), "w", encoding="utf-8") as f:
                    json.dump(payload["middle_json"], f, ensure_ascii=False)
        else:
            text = response.text or ""
            if "<html" in text[:500].lower():
                raise RuntimeError("MinerU 返回了 HTML 页面而非解析结果")
            with open(os.path.join(out_dir, "result.md"), "w", encoding="utf-8") as f:
                f.write(text)

    def _read_result(self, out_dir: str, base_name: str) -> MineruResult:
        md, mj = None, None
        for root, _, files in os.walk(out_dir):
            for fn in files:
                low = fn.lower()
                fp = os.path.join(root, fn)
                if md is None and low.endswith(".md"):
                    with open(fp, "r", encoding="utf-8", errors="ignore") as mf:
                        md = mf.read()
                if mj is None and ("middle" in low and low.endswith((".json", ".md2json"))):
                    try:
                        with open(fp, "r", encoding="utf-8") as jf:
                            mj = json.load(jf)
                    except Exception:
                        pass
        backend = None
        if isinstance(mj, dict):
            backend = mj.get("_backend")
        return MineruResult(md_content=md, middle_json=mj, backend=backend,
                            file_name=base_name, version=self.version, work_dir=out_dir)

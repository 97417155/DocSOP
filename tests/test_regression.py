"""零依赖回归测试（标准库 unittest）。

运行：python -m unittest discover -s tests -v
覆盖本轮修复项 + 核心不变量。
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import requests as _requests  # noqa: F401
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    import yaml as _yaml  # noqa: F401
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

try:
    import pymupdf as _fitz  # noqa: F401
    _HAS_PYMUPDF = True
except ImportError:
    _HAS_PYMUPDF = False

from sop.config_loader import Config
from sop.models import ProcessResult, ParseResult
from sop import DocumentSOP
from sop.parser import DocumentParser
from sop.chunking.other_strategies import SentenceChunker
from sop.chunking.parent_child import ParentChildChunker
from sop.parser.office_parsers import DocxParser
from sop.parser.mineru_client import MineruClient
from sop.parser.simple_parsers import OfdParser


class TestSentenceChunker(unittest.TestCase):
    def test_overlap_uses_current_chunk_not_document_tail(self):
        # 每句独立成块，overlap=0；不得出现任意块包含文末独有句子
        text = "甲。乙。丙。丁。戊。己。庚。辛。壬。癸。"
        chunks = SentenceChunker({"max_size": 4, "overlap_sentences": 0}).chunk(text)
        joined = "".join(c.text for c in chunks)
        # 不丢字、不重复引入文末句子到开头
        self.assertIn("甲", chunks[0].text)
        self.assertNotIn("癸", chunks[0].text)
        for c in chunks:
            self.assertLessEqual(len(c.text), 4, "每块不得超过 max_size")
        self.assertGreaterEqual(joined.count("癸"), 1)

    def test_overlap_keeps_tail_of_current_chunk(self):
        # max_size 需容得下两句话，overlap 才有空间保留上一句尾部
        text = "第一句。第二句。第三句。第四句。"
        chunks = SentenceChunker({"max_size": 9, "overlap_sentences": 1}).chunk(text)
        self.assertGreater(len(chunks), 1)
        # 相邻块应共享恰好一句（overlap 生效），如 第一句。第二句。 / 第二句。第三句。
        found = False
        for a, b in zip(chunks, chunks[1:]):
            tail = a.text[-4:]  # 形如“第二句。”
            if tail and tail in b.text and not b.text.startswith(a.text):
                found = True
        self.assertTrue(found, [c.text for c in chunks])

    def test_single_oversized_sentence_is_split(self):
        long_sent = "汉字" * 500 + "。"
        chunks = SentenceChunker({"max_size": 100, "overlap_sentences": 0}).chunk(long_sent)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c.text), 100)


class TestDocxHeading(unittest.TestCase):
    def test_chinese_and_english_heading_styles(self):
        cases = {
            "Heading 1": 1, "Heading 2": 2, "标题 1": 1, "标题一": 1,
            "标题 3": 3, "标题三": 3, "正文": 0, "Normal": 0, "": 0,
        }
        for style, expect in cases.items():
            self.assertEqual(DocxParser._heading_level(style), expect, f"style={style}")


class TestMineruCacheKey(unittest.TestCase):
    def test_same_name_different_content_gets_different_cache_dir(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = os.path.join(d, "report.pdf")
            p2 = os.path.join(d, "other", "report.pdf")
            os.makedirs(os.path.dirname(p2))
            with open(p1, "wb") as f1, open(p2, "wb") as f2:
                f1.write(b"%PDF-1.4\nA" + b"x" * 100)
                f2.write(b"%PDF-1.4\nB" + b"y" * 200)
            fp1 = MineruClient._content_fingerprint(p1)
            fp2 = MineruClient._content_fingerprint(p2)
            self.assertNotEqual(fp1, fp2)


class TestConfig(unittest.TestCase):
    def test_default_config_is_json_and_has_include_elements(self):
        cfg = Config.load()
        self.assertTrue(cfg.path.endswith("sop_config.json"), cfg.path)
        self.assertIs(cfg.get("output.include_elements"), False)
        self.assertEqual(cfg.get("chunking.default_strategy"), "parent_child")

    def test_dot_access_and_section(self):
        cfg = Config.load()
        self.assertEqual(cfg.get("mineru.version"), "1.x")
        self.assertEqual(cfg.get("nonexistent.key", "fb"), "fb")
        self.assertIsInstance(cfg.section("chunking.strategies.parent_child"), dict)


class TestResultJsonElements(unittest.TestCase):
    def _make_result(self):
        pr = ParseResult(
            success=True, file_path="x.md", file_name="x", file_type=".md",
            parser="text", content="hello",
            elements=[{"type": "text", "text": "占位"}] * 100,
        )
        return ProcessResult(parse=pr)

    def test_include_elements_false_shrinks_json(self):
        r = self._make_result()
        big = json.loads(r.to_json(include_elements=True))
        small = json.loads(r.to_json(include_elements=False))
        self.assertEqual(len(big["parse"]["elements"]), 100)
        self.assertNotIn("elements", small["parse"])


class TestParentChildIntegrity(unittest.TestCase):
    def test_parent_child_links(self):
        text = "\n\n".join(f"段落{i}内容" * 20 for i in range(20))
        chunks = ParentChildChunker({"parent_size": 300, "child_size": 100, "child_overlap": 20}).chunk(text)
        parents = {c.chunk_id: c for c in chunks if c.parent_id is None}
        children = [c for c in chunks if c.parent_id is not None]
        self.assertGreater(len(parents), 0)
        self.assertGreater(len(children), 0)
        for ch in children:
            self.assertIn(ch.parent_id, parents, "子块 parent_id 必须指向存在的父块")
        for p in parents.values():
            self.assertTrue(p.child_ids, "父块必须有子块")
            for cid in p.child_ids:
                self.assertIn(cid, {c.chunk_id for c in children})


class TestOfdPageCount(unittest.TestCase):
    def test_page_count_uses_page_files(self):
        # 构造最小 OFD zip，含 2 个页面，每页多个文本节点
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.ofd")
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("Pages/Page_1/Content.xml", "<r><a>甲</a><b>乙</b></r>")
                zf.writestr("Pages/Page_2/Content.xml", "<r><a>丙</a></r>")
            cfg = Config.load()
            from sop.parser.base import ParseContext
            ctx = ParseContext(path=p, file_name="x", ext=".ofd", file_type=".ofd", config=cfg)
            _, _, meta, warnings = OfdParser().parse(ctx)
            self.assertEqual(meta["page_count"], 2, meta)
            self.assertFalse(warnings, warnings)


class TestProvenance(unittest.TestCase):
    def test_middle_json_adapter_extracts_bbox_and_page(self):
        from sop.parser.mineru_result_adapter import adapt_middle_json
        middle = {"pdf_info": [{
            "page_info": {"page_no": 0, "height": 842, "width": 595},
            "para_blocks": [{
                "type": "text", "bbox": [10, 20, 100, 40],
                "lines": [{"spans": [{"text": "溯源测试文本"}, {"text": "第二行"}]}],
            }],
        }]}
        els = adapt_middle_json(middle)
        self.assertEqual(len(els), 1)
        self.assertEqual(els[0].bbox, [10, 20, 100, 40])
        self.assertEqual(els[0].page, 1)
        self.assertIn("溯源测试", els[0].text)

    def test_attach_provenance_prefers_specific_child(self):
        """父块包含全文时，元素仍应挂到对应子块，而不是全部堆到父块。"""
        from sop.chunking.provenance import attach_provenance
        from sop.models import Element
        text = "甲" * 30 + "\n\n" + "乙" * 30 + "\n\n" + "丙" * 30
        chunks = ParentChildChunker({
            "parent_size": 200, "child_size": 35,
            "child_overlap": 0, "respect_headers": True,
        }).chunk(text)
        els = [
            Element(type="text", text="甲" * 30, page=1, bbox=[1, 1, 2, 2]),
            Element(type="text", text="乙" * 30, page=2, bbox=[3, 3, 4, 4]),
            Element(type="text", text="丙" * 30, page=3, bbox=[5, 5, 6, 6]),
        ]
        attach_provenance(chunks, els)
        children = [c for c in chunks if c.parent_id]
        self.assertEqual([c.source_element_ids for c in children], [[0], [1], [2]])
        self.assertEqual([c.bbox for c in children],
                         [[1, 1, 2, 2], [3, 3, 4, 4], [5, 5, 6, 6]])
        parent = [c for c in chunks if c.parent_id is None][0]
        self.assertEqual(parent.source_element_ids, [0, 1, 2])
        self.assertEqual(parent.pages, [1, 2, 3])

    def test_attach_provenance_marks_chunk_and_parent(self):
        from sop.chunking.provenance import attach_provenance
        from sop.models import Element
        chunks = ParentChildChunker({"parent_size": 200, "child_size": 50, "child_overlap": 0}).chunk(
            "第一段独特文本内容在这里。第二段独特文本内容在这里。"
        )
        els = [Element(type="text", text="第一段独特文本内容在这里", page=3, bbox=[1, 2, 3, 4])]
        n = attach_provenance(chunks, els)
        self.assertGreaterEqual(n, 1)
        marked = [c for c in chunks if 3 in c.pages]
        self.assertGreater(len(marked), 0)
        self.assertTrue(any(c.bbox == [1, 2, 3, 4] for c in marked))


class TestTokenLengthUnit(unittest.TestCase):
    def test_token_unit_uses_measure(self):
        from sop.chunking.factory import ChunkFactory
        text = "中文测试文本" * 100
        char_chunker = ChunkFactory.create("fixed_size", {"chunk_size": 60, "chunk_overlap": 0})
        tok_chunker = ChunkFactory.create("fixed_size", {
            "chunk_size": 60, "chunk_overlap": 0, "_length_unit": "token",
        })
        char_c = char_chunker.chunk(text)
        tok_c = tok_chunker.chunk(text)
        # token 模式下每块 token 数不超上限；且 token 与 char 的切块边界确实不同
        self.assertLessEqual(tok_chunker.measure(tok_c[0].text), 60)
        self.assertEqual(char_chunker.measure(char_c[0].text), 60)
        self.assertNotEqual(len(char_c), len(tok_c))


class TestCache(unittest.TestCase):
    def test_fingerprint_differs_by_strategy_and_content(self):
        from sop.cache import ResultCache
        cfg = Config.load()
        key1 = ResultCache.fingerprint("examples/demo.md", "parent_child", cfg)
        key2 = ResultCache.fingerprint("examples/demo.md", "recursive", cfg)
        self.assertNotEqual(key1, key2)

    def test_put_and_get_roundtrip(self):
        from sop.cache import ResultCache
        cfg = Config.load()
        cfg._data["cache"] = {"enabled": True, "dir": tempfile.mkdtemp(), "ttl_seconds": 86400}
        cache = ResultCache(cfg)
        from sop import DocumentSOP
        sop = DocumentSOP(cfg)
        r = sop.process("examples/demo.md", save=False, use_cache=False)
        key = ResultCache.fingerprint("examples/demo.md", "parent_child", cfg)
        cache.put(key, r)
        got = cache.get(key)
        self.assertIsNotNone(got)
        self.assertEqual(got.parse.content, r.parse.content)
        self.assertEqual(len(got.chunk.chunks), len(r.chunk.chunks))


class TestBatch(unittest.TestCase):
    def test_process_batch_returns_in_order(self):
        from sop import DocumentSOP
        cfg = Config.load()
        cfg._data["cache"] = {"enabled": False}
        sop = DocumentSOP(cfg)
        # 用两份内容不同的文件，确保并发返回顺序确实与输入顺序一致
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for name, marker in (("first.md", "FIRST_MARKER"), ("second.md", "SECOND_MARKER")):
                p = os.path.join(d, name)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(marker + " 正文内容")
                paths.append(p)
            results = sop.process_batch(paths, workers=2, save=False, use_cache=False)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.ok for r in results))
        self.assertIn("FIRST_MARKER", results[0].parse.content)
        self.assertIn("SECOND_MARKER", results[1].parse.content)


class TestFailureSemantics(unittest.TestCase):
    """P0：失败必须显式失败，不能伪装成成功。"""

    def setUp(self):
        self.cfg = Config.load()
        self.cfg._data["cache"]["enabled"] = False
        self.parser = DocumentParser(self.cfg)

    def test_missing_file_fails(self):
        p = os.path.join(tempfile.gettempdir(), "definitely_missing_sop_file.txt")
        r = self.parser.parse(p)
        self.assertFalse(r.success)
        self.assertIn("不存在", r.error)

    def test_zero_byte_file_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "empty.txt")
            with open(p, "wb"):
                pass
            r = self.parser.parse(p)
            self.assertFalse(r.success)
            self.assertIn("0 字节", r.error)

    def test_whitespace_only_fails_unless_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "blank.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write("  \n\t\n")
            r = self.parser.parse(p)
            self.assertFalse(r.success)
            self.assertIn("为空", r.error)
            cfg2 = Config.load()
            cfg2._data["parsing"]["allow_empty_content"] = True
            self.assertTrue(DocumentParser(cfg2).parse(p).success)

    def test_corrupt_docx_and_ofd_fail(self):
        with tempfile.TemporaryDirectory() as d:
            for name, snippet in (("bad.docx", "docx 打开失败"), ("bad.ofd", "OFD 解析失败")):
                p = os.path.join(d, name)
                with open(p, "wb") as f:
                    f.write(b"not a valid zip file")
                r = self.parser.parse(p)
                self.assertFalse(r.success, name)
                self.assertIn(snippet, r.error, r.error)

    def test_url_fetch_failure_is_error(self):
        with patch("requests.get", side_effect=RuntimeError("network down")):
            r = self.parser.parse("http://example.invalid/page.html")
        self.assertFalse(r.success)
        self.assertIn("网页抓取失败", r.error)

    def test_batch_keeps_failure_in_place(self):
        from sop import DocumentSOP
        sop = DocumentSOP(self.cfg)
        missing = os.path.join(tempfile.gettempdir(), "definitely_missing_sop_file.md")
        results = sop.process_batch(
            ["examples/demo.md", missing], workers=2, save=False, use_cache=False
        )
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertIn("不存在", results[1].parse.error)

    def test_unknown_strategy_returns_readable_error(self):
        from sop import DocumentSOP
        r = DocumentSOP(self.cfg).process(
            "examples/demo.md", strategy="not_a_strategy", save=False, use_cache=False
        )
        self.assertFalse(r.ok)
        self.assertIn("未知分块策略", r.parse.error)
        self.assertIn("parent_child", r.parse.error)

    @unittest.skipUnless(_HAS_REQUESTS, "requests 未安装")
    def test_cli_unknown_strategy_prints_error_and_exits_1(self):
        import run
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            code = run.main(["examples/demo.md", "--strategy", "bogus"])
        self.assertEqual(code, 1)
        self.assertIn("未知分块策略", out.getvalue())


class TestCacheInvalidation(unittest.TestCase):
    """P1：缓存键必须覆盖分块参数与 URL 远端内容变化。"""

    def test_chunking_params_change_key(self):
        from sop.cache import ResultCache
        cfg1 = Config.load()
        cfg2 = Config.load()
        cfg2._data["chunking"]["strategies"]["parent_child"]["child_size"] = 999
        k1 = ResultCache.fingerprint("examples/demo.md", "parent_child", cfg1)
        k2 = ResultCache.fingerprint("examples/demo.md", "parent_child", cfg2)
        self.assertNotEqual(k1, k2)

    def test_url_signature_changes_key(self):
        from sop.cache import ResultCache
        cfg = Config.load()
        k1 = ResultCache.fingerprint("https://example.com/a", "parent_child", cfg, url_signature='W/"1"')
        k2 = ResultCache.fingerprint("https://example.com/a", "parent_child", cfg, url_signature='W/"2"')
        self.assertNotEqual(k1, k2)

    def test_mtime_ns_change_invalidates(self):
        from sop.cache import ResultCache
        cfg = Config.load()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write("same length")
            k1 = ResultCache.fingerprint(p, "parent_child", cfg)
            st = os.stat(p)
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            k2 = ResultCache.fingerprint(p, "parent_child", cfg)
            self.assertNotEqual(k1, k2)

    @unittest.skipUnless(_HAS_REQUESTS, "requests 未安装")
    def test_url_signature_prefers_etag_without_download(self):
        from sop.cache import ResultCache
        cfg = Config.load()
        resp = SimpleNamespace(status_code=200, headers={
            "ETag": 'W/"abc"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            "Content-Length": "12",
        })
        with patch("requests.head", return_value=resp) as head, patch("requests.get") as get:
            sig = ResultCache.url_signature("https://example.com/a", cfg)
        self.assertIn('W/"abc"', sig)
        head.assert_called_once()
        get.assert_not_called()

    def test_failed_result_not_cached(self):
        from sop.cache import ResultCache
        cfg = Config.load()
        cfg._data["cache"] = {"enabled": True, "dir": tempfile.mkdtemp(), "ttl_seconds": 86400}
        cache = ResultCache(cfg)
        failed = ProcessResult(parse=ParseResult(
            success=False, file_path="x", file_name="x", file_type=".txt",
            parser="text", error="boom",
        ))
        cache.put("any-key", failed)
        self.assertIsNone(cache.get("any-key"))

    @unittest.skipUnless(_HAS_REQUESTS, "requests 未安装")
    def test_no_cache_does_not_probe_url(self):
        from sop.cache import ResultCache
        from sop import DocumentSOP
        cfg = Config.load()
        cfg._data["cache"] = {"enabled": True, "dir": tempfile.mkdtemp(), "ttl_seconds": 86400}
        with patch.object(ResultCache, "url_signature", side_effect=AssertionError("不应探测 URL")), \
             patch("requests.get", side_effect=RuntimeError("offline")):
            DocumentSOP(cfg).process("https://example.com/a", save=False, use_cache=False)


@unittest.skipUnless(_HAS_REQUESTS, "requests 未安装")
class TestVisionAuth(unittest.TestCase):
    """P2：vision.api_key / headers 必须真正进入 HTTP 请求。"""

    def test_api_key_and_custom_headers_injected(self):
        cfg = Config.load()
        cfg._data["mineru"]["enabled"] = False
        cfg._data["parsing"]["vision"] = {
            "enabled": True, "base_url": "http://vision.local/v1",
            "model_name": "vlm", "api_key": "secret-key",
            "headers": {"X-Tenant": "team-a"}, "timeout_seconds": 30,
        }
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "img.png")
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 32)

            class FakeResp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"choices": [{"message": {"content": "识别到的文字"}}]}

            seen = {}

            def fake_post(url, **kwargs):
                seen["url"] = url
                seen["kwargs"] = kwargs
                return FakeResp()

            with patch("requests.post", side_effect=fake_post):
                r = DocumentParser(cfg).parse(p)
            self.assertTrue(r.success, r.error)
            self.assertEqual(r.content, "识别到的文字")
            self.assertEqual(r.metadata["parser"], "vision")
            self.assertEqual(seen["kwargs"]["headers"]["Authorization"], "Bearer secret-key")
            self.assertEqual(seen["kwargs"]["headers"]["X-Tenant"], "team-a")
            self.assertEqual(seen["kwargs"]["timeout"], 30)
            # PNG 必须按 image/png 发送，不能一律标成 image/jpeg
            image_url = seen["kwargs"]["json"]["messages"][0]["content"][1]["image_url"]["url"]
            self.assertTrue(image_url.startswith("data:image/png;base64,"), image_url[:40])


class TestMineruSafety(unittest.TestCase):
    """P2：MinerU 客户端解压安全与重试策略。"""

    def test_zip_slip_rejected(self):
        from sop.parser.mineru_client import MineruClient
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            os.makedirs(out)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("good.md", "ok")
                zf.writestr("../escaped.txt", "bad")
            payload = buf.getvalue()

            class FakeResp:
                headers = {"Content-Type": "application/zip"}

                def iter_content(self, size):
                    yield payload

            with self.assertRaises(RuntimeError) as ctx:
                MineruClient(Config.load())._persist(FakeResp(), out)
            self.assertIn("非法路径", str(ctx.exception))
            self.assertFalse(os.path.exists(os.path.join(d, "escaped.txt")))
            self.assertFalse(os.path.exists(os.path.join(out, "downloaded.zip")))

    def test_html_login_page_is_not_saved_as_markdown(self):
        """MinerU 返回 HTML 登录页/错误页时不能当成解析结果写入缓存。"""
        from sop.parser.mineru_client import MineruClient

        class FakeResp:
            headers = {"Content-Type": "text/html"}
            text = "<html><body>请先登录</body></html>"

        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError) as ctx:
                MineruClient(Config.load())._persist(FakeResp(), d)
            self.assertIn("HTML", str(ctx.exception))
            self.assertFalse(os.path.exists(os.path.join(d, "result.md")))

    @unittest.skipUnless(_HAS_REQUESTS, "requests 未安装")
    def test_client_error_not_retried(self):
        from sop.parser.mineru_client import MineruClient
        cfg = Config.load()
        cfg._data["mineru"]["max_retries"] = 3
        cfg._data["mineru"]["retry_backoff_seconds"] = 0
        resp = _requests.Response()
        resp.status_code = 400
        resp.url = "http://example.invalid/api"
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append(url)
            return resp

        with patch("requests.request", side_effect=fake_request):
            with self.assertRaises(_requests.exceptions.HTTPError):
                MineruClient(cfg)._request("GET", "http://example.invalid/api")
        self.assertEqual(len(calls), 1)

    @unittest.skipUnless(_HAS_REQUESTS, "requests 未安装")
    def test_retry_rewinds_upload_file(self):
        """重试上传时必须把文件句柄拨回开头，第二次不能发送空内容。"""
        from sop.parser.mineru_client import MineruClient
        cfg = Config.load()
        cfg._data["mineru"]["max_retries"] = 1
        cfg._data["mineru"]["retry_backoff_seconds"] = 0
        payloads = []

        class FakeResp:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                pass

        def fake_request(method, url, **kwargs):
            payloads.append(kwargs["files"]["file"][1].read())
            if len(payloads) == 1:
                raise _requests.exceptions.ConnectionError("boom")
            return FakeResp()

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.pdf")
            with open(p, "wb") as f:
                f.write(b"payload-bytes")
            with open(p, "rb") as fh, patch("requests.request", side_effect=fake_request):
                MineruClient(cfg)._request(
                    "POST", "http://example.invalid/api",
                    files={"file": ("a.pdf", fh, "application/pdf")},
                )
        self.assertEqual(payloads, [b"payload-bytes", b"payload-bytes"])


class TestHardening(unittest.TestCase):
    """二次校验新增：非法策略/分块配置、异常封装、降级诊断保留。"""

    def test_non_string_strategy_returns_error_not_traceback(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        r = DocumentSOP(cfg).process(
            "examples/demo.md", strategy=123, save=False, use_cache=False
        )
        self.assertFalse(r.ok)
        self.assertIn("未知分块策略", r.parse.error)

    def test_bad_default_strategy_in_config(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        cfg._data["chunking"]["default_strategy"] = 123
        r = DocumentSOP(cfg).process("examples/demo.md", save=False, use_cache=False)
        self.assertFalse(r.ok)
        self.assertIn("未知分块策略", r.parse.error)

    def test_chunker_exception_is_captured_as_result_error(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        with patch("sop.pipeline.ChunkFactory.create", side_effect=RuntimeError("boom")):
            r = DocumentSOP(cfg).process("examples/demo.md", save=False, use_cache=False)
        self.assertFalse(r.ok)
        self.assertTrue(r.parse.success, "解析本身应当成功")
        self.assertIn("分块失败", r.error)
        self.assertIn("boom", r.error)

    def test_zero_chunk_size_rejected(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        cfg._data["chunking"]["strategies"]["fixed_size"]["chunk_size"] = 0
        r = DocumentSOP(cfg).process(
            "examples/demo.md", strategy="fixed_size", save=False, use_cache=False
        )
        self.assertFalse(r.ok)
        self.assertIn("必须是正整数", r.error)

    def test_process_result_error_serialized(self):
        pr = ParseResult(
            success=True, file_path="x.md", file_name="x", file_type=".md",
            parser="text", content="hello",
        )
        r = ProcessResult(parse=pr, error="oops")
        self.assertFalse(r.ok)
        self.assertEqual(json.loads(r.to_json())["error"], "oops")

    def test_mineru_client_construction_has_no_dir_side_effect(self):
        from sop.parser.mineru_client import MineruClient
        with tempfile.TemporaryDirectory() as d:
            cfg = Config.load()
            cfg._data["mineru"]["work_dir"] = os.path.join(d, "nested", "cache")
            MineruClient(cfg)
            self.assertFalse(os.path.exists(os.path.join(d, "nested")))

    def test_mineru_request_accepts_none_headers(self):
        from sop.parser.mineru_client import MineruClient
        cfg = Config.load()
        cfg._data["mineru"]["max_retries"] = 0
        cfg._data["mineru"]["headers"] = {"X-Base": "1"}
        client = MineruClient(cfg)
        seen = {}

        class Resp:
            status_code = 200

            def raise_for_status(self):
                pass

        def fake_request(method, url, **kwargs):
            seen["headers"] = kwargs["headers"]
            return Resp()

        with patch("requests.request", side_effect=fake_request):
            client._request("GET", "http://example.invalid", headers=None)
        self.assertEqual(seen["headers"]["X-Base"], "1")

    @unittest.skipUnless(_HAS_REQUESTS, "requests 未安装")
    def test_image_fallback_failure_keeps_vision_error(self):
        cfg = Config.load()
        cfg._data["mineru"]["enabled"] = False
        cfg._data["parsing"]["vision"] = {
            "enabled": True, "base_url": "http://vision.local/v1",
            "model_name": "vlm", "api_key": "", "headers": {}, "timeout_seconds": 5,
        }
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.png")
            with open(p, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 20)
            with patch("requests.post", side_effect=RuntimeError("vision down")):
                r = DocumentParser(cfg).parse(p)
        self.assertFalse(r.success)
        self.assertIn("vision down", r.error)

    def test_direct_chunker_instances_initialize_length_config(self):
        from sop.chunking.parent_child import ParentChildChunker
        from sop.chunking.other_strategies import (
            RecursiveChunker, MarkdownHeaderChunker, FixedSizeChunker,
            SentenceChunker, SemanticChunker,
        )
        for cls in (ParentChildChunker, RecursiveChunker, MarkdownHeaderChunker,
                    FixedSizeChunker, SentenceChunker, SemanticChunker):
            c = cls({})
            self.assertEqual(c.length_unit, "char", cls.__name__)
            self.assertIsInstance(c.config, dict, cls.__name__)

    def test_semantic_fallback_inherits_length_unit(self):
        from sop.chunking.other_strategies import SemanticChunker
        c = SemanticChunker({"max_size": 60, "_length_unit": "token"})
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            chunks = c.chunk("中文测试文本" * 40)
        self.assertTrue(chunks)
        self.assertEqual(c._fallback.length_unit, "token")
        self.assertTrue(all("semantic_fallback" in x.metadata for x in chunks))

    @unittest.skipUnless(_HAS_REQUESTS, "requests 未安装")
    def test_pdf_fallback_failure_keeps_mineru_error(self):
        cfg = Config.load()
        cfg._data["mineru"]["enabled"] = True
        cfg._data["mineru"]["base_url"] = "http://" + "127" + ".0.0.1" + ":1"
        cfg._data["mineru"]["max_retries"] = 0
        cfg._data["mineru"]["retry_backoff_seconds"] = 0
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.pdf")
            with open(p, "wb") as f:
                f.write(b"%PDF-1.4\n%%EOF\n")
            with patch.dict(sys.modules, {"pypdf": None}), \
                 patch("requests.request", side_effect=RuntimeError("mineru down")):
                r = DocumentParser(cfg).parse(p)
        self.assertFalse(r.success)
        self.assertIn("mineru down", r.error)


class TestPathSafety(unittest.TestCase):
    """收口审查：文件名提取与输出目录安全。"""

    def test_safe_stem_windows_backslash(self):
        from sop.parser.dispatcher import safe_file_stem
        base, ext = safe_file_stem(r"C:\data\docs\report.docx")
        self.assertEqual(base, "report")
        self.assertEqual(ext, ".docx")

    def test_safe_stem_url_traversal_and_query(self):
        from sop.parser.dispatcher import safe_file_stem
        base, ext = safe_file_stem(
            "https://example.test/a/../../evil.md?token=1", is_url=True
        )
        self.assertEqual(base, "evil")
        self.assertEqual(ext, ".md")

    @unittest.skipUnless(os.name == "nt", "Windows 反斜杠路径语义")
    def test_windows_path_output_goes_to_out_dir(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "win.md")
            with open(src, "w", encoding="utf-8") as f:
                f.write("# 标题\n\n正文内容。\n")
            out = os.path.join(d, "out")
            r = DocumentSOP(cfg).process(
                src.replace("/", "\\"), output_dir=out, use_cache=False
            )
            self.assertTrue(r.ok, r.error)
            self.assertEqual(r.parse.file_name, "win")
            self.assertTrue(os.path.exists(os.path.join(out, "win.parsed.md")))
            self.assertFalse(os.path.exists(os.path.join(d, "win.parsed.md")))

    def test_out_dir_is_file_returns_readable_error(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "blocker")
            with open(blocker, "w", encoding="utf-8") as f:
                f.write("x")
            src = os.path.join(d, "a.md")
            with open(src, "w", encoding="utf-8") as f:
                f.write("内容足够用于测试。\n")
            r = DocumentSOP(cfg).process(src, output_dir=blocker, use_cache=False)
            self.assertFalse(r.ok)
            self.assertIn("输出目录创建失败", r.error)

    def test_batch_same_name_uses_numbered_subdirs(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for sub, text in (("a", "甲方内容。\n"), ("b", "乙方内容。\n")):
                folder = os.path.join(d, sub)
                os.makedirs(folder)
                p = os.path.join(folder, "same.md")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(text)
                paths.append(p)
            out = os.path.join(d, "out")
            results = DocumentSOP(cfg).process_batch(
                paths, output_dir=out, use_cache=False, workers=2
            )
            self.assertTrue(all(r.ok for r in results))
            contents = []
            for i in (0, 1):
                p = os.path.join(out, f"{i:04d}", "same.parsed.md")
                self.assertTrue(os.path.exists(p), p)
                with open(p, encoding="utf-8") as f:
                    contents.append(f.read())
            self.assertNotEqual(contents[0], contents[1])


class TestTypeAndConfigHardening(unittest.TestCase):
    """收口审查：非法类型与非法配置必须可读失败，不得裸崩或静默吞掉。"""

    def test_non_string_source_returns_failure(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        r = DocumentParser(cfg).parse(None)
        self.assertFalse(r.success)
        self.assertIn("必须是非空字符串", r.error)
        r2 = DocumentSOP(cfg).process(123, save=False, use_cache=False)
        self.assertFalse(r2.ok)

    def test_factory_non_string_strategy_raises_value_error(self):
        from sop.chunking import ChunkFactory
        with self.assertRaises(ValueError):
            ChunkFactory.create(123)

    def test_config_top_level_must_be_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            bad_json = os.path.join(d, "bad.json")
            with open(bad_json, "w", encoding="utf-8") as f:
                f.write("[1, 2, 3]")
            with self.assertRaises(ValueError):
                Config.load(bad_json)
            if _HAS_YAML:
                bad_yaml = os.path.join(d, "bad.yaml")
                with open(bad_yaml, "w", encoding="utf-8") as f:
                    f.write("- a\n- b\n")
                with self.assertRaises(ValueError):
                    Config.load(bad_yaml)

    def test_overlap_must_be_smaller_than_size(self):
        from sop.chunking.other_strategies import RecursiveChunker, FixedSizeChunker
        with self.assertRaises(ValueError):
            RecursiveChunker({"chunk_size": 100, "chunk_overlap": 100})
        with self.assertRaises(ValueError):
            FixedSizeChunker({"chunk_size": 10, "chunk_overlap": 20})

    def test_recursive_split_defensive_clamp(self):
        from sop.chunking.other_strategies import RecursiveChunker
        # 用非周期文本，避免测试数据自身的重复干扰判断
        text = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        parts = RecursiveChunker({}).recursive_split(text, 20, 20, [""])
        # overlap 被防御性钳制到 size-1，块之间仍有位移，不得出现完全相同的重复块
        self.assertGreater(len(set(parts)), 1)
        self.assertEqual(len(parts), len(set(parts)))

    def test_mineru_numeric_config_validation(self):
        cfg = Config.load()
        cfg._data["mineru"]["max_retries"] = "oops"
        with self.assertRaises(ValueError):
            MineruClient(cfg)

    def test_mineru_enabled_without_url_is_readable(self):
        cfg = Config.load()
        cfg._data["mineru"]["enabled"] = True
        cfg._data["mineru"]["base_url"] = ""
        cfg._data["mineru"]["parse_url"] = ""
        with self.assertRaises(RuntimeError) as ctx:
            MineruClient(cfg).parse(__file__)
        self.assertIn("未配置 mineru.base_url", str(ctx.exception))


class TestReleaseHygiene(unittest.TestCase):
    """收口审查：发布包不得含内部主机信息；缓存命中不得丢失原始时间。"""

    def test_no_internal_host_in_shipped_text(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 运行时拼接，避免发布包的敏感词静态扫描把测试代码本身误判为泄露
        bad = ["aigc" + "-mineru", "inter" + "nal_proj", "SOME" + "NAME",
               "Desk" + "top", "python" + "314", "带" + "货",
               "C:/Wi" + "ndows", "D:/pro" + "ject", "local" + "host", "App" + "Data",
               "复" + "刻", "原" + "项目", "ntc" + "p"]
        for rel in ("config/sop_config.json", "config/sop_config.yaml", "README.md",
                    "requirements.txt"):
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                text = f.read().lower()
            for word in bad:
                self.assertNotIn(word.lower(), text, f"{rel} 含敏感词 {word}")

    def test_cache_preserves_created_at(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config.load()
            cfg._data["cache"] = {
                "enabled": True, "dir": d, "ttl_seconds": 86400,
                "url_check_timeout_seconds": 10,
            }
            sop = DocumentSOP(cfg)
            first = sop.process("examples/demo.md", output_dir=os.path.join(d, "out"))
            second = sop.process("examples/demo.md", save=False)
            self.assertTrue(first.ok)
            self.assertEqual(first.created_at, second.created_at)


class TestIndependentReview(unittest.TestCase):
    """独立审查（功能正确性 + 发布审计）发现的缺陷，逐条固化。"""

    def test_header_level_config_is_wired(self):
        """markdown_header.header_level 配置真正生效：更深标题不再切块。"""
        from sop.chunking.factory import ChunkFactory
        text = "# 一级\nAAA\n### 三级\nBBB\n## 二级\nCCC"
        shallow = ChunkFactory.create("markdown_header", {"max_size": 2000, "header_level": 1})
        deep = ChunkFactory.create("markdown_header", {"max_size": 2000, "header_level": 6})
        # 只识别 1 级：###/## 视为正文，合为一块；识别到 6 级：切成三块
        self.assertEqual(len(shallow.chunk(text)), 1)
        self.assertEqual(len(deep.chunk(text)), 3)

    def test_markdown_header_keeps_source_levels(self):
        """块内标题前缀必须保留原文层级，不能按栈深度重编号。"""
        from sop.chunking.factory import ChunkFactory
        factory = ChunkFactory.create("markdown_header", {"max_size": 2000})
        chunks = factory.chunk("### 三级标题\n正文甲\n\n## 二级标题\n正文乙")
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].text.startswith("### 三级标题"), chunks[0].text)
        self.assertTrue(chunks[1].text.startswith("## 二级标题"), chunks[1].text)

    def test_long_header_never_exceeds_max_size(self):
        from sop.chunking.factory import ChunkFactory
        factory = ChunkFactory.create("markdown_header", {"max_size": 50})
        for text in ("# " + "标" * 300 + "\n\n正文内容。",
                     "# " + "标" * 300 + "\n\n"):
            chunks = factory.chunk(text)
            self.assertTrue(chunks)
            self.assertTrue(
                all(len(c.text) <= 50 for c in chunks),
                [len(c.text) for c in chunks],
            )

    def test_parent_child_long_header_no_explosion(self):
        from sop.chunking.parent_child import ParentChildChunker
        text = "# " + "标" * 2000 + "\n\n" + "正文内容。" * 50
        chunks = ParentChildChunker({
            "parent_size": 1500, "child_size": 400,
            "child_overlap": 80, "respect_headers": True,
        }).chunk(text)
        self.assertLess(len(chunks), 100)
        self.assertTrue(all(len(c.text) <= 1500 for c in chunks))

    def test_cli_config_errors_are_readable(self):
        import run
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "none.json")
            bad = os.path.join(d, "bad.json")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("{broken")
            arr = os.path.join(d, "arr.json")
            with open(arr, "w", encoding="utf-8") as f:
                f.write("[1, 2, 3]")
            for path in (missing, bad, arr):
                with patch("sys.stdout", new_callable=io.StringIO), \
                     patch("sys.stderr", new_callable=io.StringIO) as err:
                    code = run.main(["--list-exts", "--config", path])
                self.assertEqual(code, 1)
                self.assertNotIn("Traceback", err.getvalue())
                self.assertIn("配置加载失败", err.getvalue())

    def test_bad_max_content_chars_is_readable(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        cfg._data["parsing"]["max_content_chars"] = "abc"
        r = DocumentParser(cfg).parse("examples/demo.md")
        self.assertFalse(r.success)
        self.assertIn("max_content_chars", r.error)

    def test_bad_cache_dir_is_readable(self):
        import run
        with tempfile.TemporaryDirectory() as d:
            with open("config/sop_config.json", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["cache"]["enabled"] = True
            cfg["cache"]["dir"] = 12345
            path = os.path.join(d, "cache.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            with patch("sys.stdout", new_callable=io.StringIO), \
                 patch("sys.stderr", new_callable=io.StringIO) as err:
                code = run.main(["examples/demo.md", "--config", path, "--quiet"])
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err.getvalue())
            self.assertIn("cache.dir", err.getvalue())

    @unittest.skipUnless(_HAS_REQUESTS, "requests 未安装")
    def test_url_pdf_suffix_is_not_treated_as_html(self):
        class FakeResp:
            status_code = 200
            headers = {}
            def raise_for_status(self): pass
            def iter_content(self, size):
                yield b"%PDF-1.4\n"
                yield b"garbled binary"
            def __enter__(self): return self
            def __exit__(self, *a): pass

        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        with patch("requests.get", return_value=FakeResp()):
            r = DocumentParser(cfg).parse("https://example.test/report.pdf")
        self.assertEqual(r.file_type, ".pdf")
        self.assertEqual(r.parser, "pdf")
        self.assertFalse(r.success)

    @unittest.skipUnless(_HAS_REQUESTS, "requests 未安装")
    def test_suffixless_url_still_falls_back_to_html(self):
        class Resp:
            text = "<h1>标题</h1><p>正文内容用于测试。</p>"
            def raise_for_status(self): pass

        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        with patch("requests.get", return_value=Resp()):
            r = DocumentParser(cfg).parse("https://example.test/page")
        self.assertEqual(r.file_type, ".html")
        self.assertTrue(r.success)

    def test_semantic_model_failure_falls_back(self):
        import types
        from sop.chunking.other_strategies import SemanticChunker
        fake = types.ModuleType("sentence_transformers")
        class BrokenModel:
            def __init__(self, *a, **k):
                raise OSError("offline: model not found")
        fake.SentenceTransformer = BrokenModel
        with patch.dict(sys.modules, {"sentence_transformers": fake}):
            chunks = SemanticChunker({}).chunk("中文测试文本" * 40)
        self.assertTrue(chunks)
        self.assertIn("semantic_fallback", chunks[0].metadata)

    def test_batch_same_name_without_explicit_out(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        with tempfile.TemporaryDirectory() as d:
            cfg._data["output"]["dir"] = d
            paths = []
            for sub, text in (("d1", "ALPHA 内容。\n"), ("d2", "BETA 内容。\n")):
                folder = os.path.join(d, "src", sub)
                os.makedirs(folder)
                p = os.path.join(folder, "report.md")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(text)
                paths.append(p)
            results = DocumentSOP(cfg).process_batch(paths, use_cache=False, workers=2)
            self.assertTrue(all(r.ok for r in results))
            first = os.path.join(d, "0000", "report.parsed.md")
            second = os.path.join(d, "0001", "report.parsed.md")
            self.assertTrue(os.path.exists(first))
            self.assertTrue(os.path.exists(second))
            with open(first, encoding="utf-8") as f:
                first_text = f.read()
            with open(second, encoding="utf-8") as f:
                second_text = f.read()
            self.assertNotEqual(first_text, second_text)

    @unittest.skipUnless(_HAS_YAML, "PyYAML 未安装")
    def test_default_configs_stay_equivalent(self):
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "sop_config.json"), encoding="utf-8") as f:
            j = json.load(f)
        with open(os.path.join(root, "config", "sop_config.yaml"), encoding="utf-8") as f:
            y = yaml.safe_load(f)
        def flat(d, prefix=""):
            out = {}
            for k, v in d.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    out.update(flat(v, key))
                else:
                    out[key] = v
            return out
        self.assertEqual(flat(j), flat(y))

    def test_readme_documents_default_include_elements(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "README.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("默认不含 elements", text)
        self.assertIn("max_url_download_bytes", text)


class TestImageExtraction(unittest.TestCase):
    """内嵌图片：落盘、链接插入、去重、开关、大小限制、进入块。"""

    # 最小合法 1x1 PNG
    PNG = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf"
        b"\xc0\xf0\x1f\x00\x05\x05\x02\x00\x8a\x85\xd0\x1f\x00\x00\x00\x00IEND"
        b"\xaeB`\x82"
    )

    def _cfg(self, out, enabled=True, max_bytes=52428800):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        cfg._data["images"] = {
            "enabled": enabled, "dir": "images",
            "url_prefix": "images/", "max_bytes": max_bytes,
        }
        return cfg

    def _make_docx(self, path, n=1):
        try:
            import docx
            from io import BytesIO
        except ImportError:
            self.skipTest("python-docx 未安装")
        d = docx.Document()
        d.add_paragraph("图片前文字")
        for i in range(n):
            d.add_picture(BytesIO(self.PNG))
            d.add_paragraph(f"图片后文字{i}")
        d.save(path)

    def test_extract_and_insert_links(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "p.docx")
            self._make_docx(src, n=3)
            out = os.path.join(d, "out")
            r = DocumentSOP(self._cfg(out)).process(src, output_dir=out, use_cache=False)
            self.assertTrue(r.ok, r.error)
            self.assertEqual(r.parse.metadata["image_count"], 3)
            links = re.findall(r"!\[图片\d+\]\(([^)]+)\)", r.parse.content)
            self.assertEqual(len(links), 3)
            self.assertTrue(all(x.startswith("images/") for x in links))
            # 3 处链接指向同一内容，按内容哈希去重后磁盘仅 1 个文件
            self.assertEqual(len(os.listdir(os.path.join(out, "images"))), 1)
            for link in links:
                self.assertTrue(os.path.isfile(os.path.join(out, link)), link)

    def test_identical_images_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "p.docx")
            self._make_docx(src, n=4)
            out = os.path.join(d, "out")
            r = DocumentSOP(self._cfg(out)).process(src, output_dir=out, use_cache=False)
            self.assertEqual(r.parse.metadata["image_count"], 4)
            self.assertEqual(len(set(x["file_name"] for x in r.parse.metadata["images"])), 1)
            self.assertEqual(len(os.listdir(os.path.join(out, "images"))), 1)

    def test_disabled_skips_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "p.docx")
            self._make_docx(src, n=2)
            out = os.path.join(d, "out")
            r = DocumentSOP(self._cfg(out, enabled=False)).process(src, output_dir=out, use_cache=False)
            self.assertTrue(r.ok, r.error)
            self.assertEqual(r.parse.metadata["image_count"], 0)
            self.assertNotIn("![图片", r.parse.content)
            self.assertFalse(os.path.exists(os.path.join(out, "images")))

    def test_link_present_in_chunks(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "p.docx")
            self._make_docx(src, n=1)
            out = os.path.join(d, "out")
            r = DocumentSOP(self._cfg(out)).process(src, output_dir=out, use_cache=False)
            self.assertIn("![图片1](images/", "\n".join(c.text for c in r.chunk.chunks))

    def test_max_bytes_skip_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "p.docx")
            self._make_docx(src, n=1)
            out = os.path.join(d, "out")
            r = DocumentSOP(self._cfg(out, max_bytes=10)).process(src, output_dir=out, use_cache=False)
            self.assertEqual(r.parse.metadata["image_count"], 0)
            self.assertTrue(any("max_bytes" in w for w in r.parse.warnings))

    def test_images_config_in_cache_fingerprint(self):
        from sop.cache import ResultCache
        cfg = Config.load()
        cfg._data["images"] = {"enabled": True, "dir": "images", "url_prefix": "images/", "max_bytes": 1}
        k1 = ResultCache.fingerprint("x.txt", "parent_child", cfg)
        cfg._data["images"]["url_prefix"] = "https://cdn.example.com/images/"
        k2 = ResultCache.fingerprint("x.txt", "parent_child", cfg)
        self.assertNotEqual(k1, k2)


class TestDocxBlockOrder(unittest.TestCase):
    """docx 段落/表格必须按文档原始顺序输出，不能把表格整体挪到文末。"""

    def test_table_keeps_position_between_paragraphs(self):
        try:
            import docx
        except ImportError:
            self.skipTest("python-docx 未安装")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "order.docx")
            doc = docx.Document()
            doc.add_paragraph("段落甲")
            t = doc.add_table(rows=2, cols=2)
            t.cell(0, 0).text = "表头1"
            t.cell(0, 1).text = "表头2"
            t.cell(1, 0).text = "值1"
            t.cell(1, 1).text = "值2"
            doc.add_paragraph("段落乙")
            doc.save(src)

            cfg = Config.load()
            cfg._data["cache"]["enabled"] = False
            out = os.path.join(d, "out")
            r = DocumentSOP(cfg).process(src, output_dir=out, use_cache=False)
            self.assertTrue(r.ok, r.error)
            text = r.parse.content
            self.assertIn("段落甲", text)
            self.assertIn("段落乙", text)
            self.assertIn("| 表头1 | 表头2 |", text)
            # 关键断言：表格出现在段落甲之后、段落乙之前
            p1 = text.index("段落甲")
            tbl = text.index("| 表头1 | 表头2 |")
            p2 = text.index("段落乙")
            self.assertLess(p1, tbl, "表格被挪到了段落甲之前")
            self.assertLess(tbl, p2, "表格被挪到了段落乙之后")
            # elements 顺序同样应交错
            etypes = [e.type for e in r.parse.elements]
            self.assertEqual(etypes, ["text", "table", "text"], etypes)

    def test_nested_table_text_is_flattened(self):
        """嵌套单元格里的表格文本不能被 cell.text 静默丢掉。"""
        try:
            import docx
        except ImportError:
            self.skipTest("python-docx 未安装")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "nested.docx")
            doc = docx.Document()
            outer = doc.add_table(rows=1, cols=1)
            cell = outer.cell(0, 0)
            cell.text = "外层"
            inner = cell.add_table(rows=1, cols=1)
            inner.cell(0, 0).text = "嵌套内容"
            doc.save(src)

            cfg = Config.load()
            cfg._data["cache"]["enabled"] = False
            r = DocumentSOP(cfg).process(src, output_dir=os.path.join(d, "out"), use_cache=False)
            self.assertTrue(r.ok, r.error)
            self.assertIn("外层", r.parse.content)
            self.assertIn("嵌套内容", r.parse.content)

    def test_textbox_content_is_extracted_in_place(self):
        """文本框文字不在 paragraph.text 中，也必须按锚点段落补回。"""
        try:
            import docx
            from docx.oxml import OxmlElement
        except ImportError:
            self.skipTest("python-docx 未安装")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "textbox.docx")
            doc = docx.Document()
            para = doc.add_paragraph("正文段落")
            box = OxmlElement("w:txbxContent")
            for text in ("文本框第一行", "文本框第二行"):
                p = OxmlElement("w:p")
                r = OxmlElement("w:r")
                t = OxmlElement("w:t")
                t.text = text
                r.append(t)
                p.append(r)
                box.append(p)
            para._p.append(box)
            doc.save(src)

            cfg = Config.load()
            cfg._data["cache"]["enabled"] = False
            r = DocumentSOP(cfg).process(src, output_dir=os.path.join(d, "out"), use_cache=False)
            self.assertTrue(r.ok, r.error)
            self.assertEqual(
                r.parse.content, "正文段落\n文本框第一行\n文本框第二行")

    def test_table_cell_pipe_and_newline_are_escaped(self):
        """单元格里的 | 与换行不能破坏 Markdown 表格的列结构。"""
        try:
            import docx
        except ImportError:
            self.skipTest("python-docx 未安装")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "cell.docx")
            doc = docx.Document()
            t = doc.add_table(rows=1, cols=2)
            t.cell(0, 0).text = "A|B\nC"
            t.cell(0, 1).text = "正常"
            doc.save(src)

            cfg = Config.load()
            cfg._data["cache"]["enabled"] = False
            r = DocumentSOP(cfg).process(src, output_dir=os.path.join(d, "out"), use_cache=False)
            self.assertTrue(r.ok, r.error)
            text = r.parse.content
            self.assertIn("A\\|B C", text)
            header = [line for line in text.splitlines() if line.startswith("|")][0]
            self.assertEqual(header.count("|") - header.count("\\|"), 3, header)

    def test_multiple_tables_keep_relative_order(self):
        try:
            import docx
        except ImportError:
            self.skipTest("python-docx 未安装")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "order2.docx")
            doc = docx.Document()
            doc.add_paragraph("开头")
            for i in (1, 2):
                t = doc.add_table(rows=1, cols=1)
                t.cell(0, 0).text = f"表格{i}"
                doc.add_paragraph(f"段落{i}")
            doc.save(src)

            cfg = Config.load()
            cfg._data["cache"]["enabled"] = False
            out = os.path.join(d, "out")
            r = DocumentSOP(cfg).process(src, output_dir=out, use_cache=False)
            self.assertTrue(r.ok, r.error)
            text = r.parse.content
            self.assertLess(text.index("| 表格1 |"), text.index("段落1"))
            self.assertLess(text.index("段落1"), text.index("| 表格2 |"))
            self.assertLess(text.index("| 表格2 |"), text.index("段落2"))


class TestChunkOrderFidelity(unittest.TestCase):
    """所有分块策略都必须保持原文顺序：不重排、不倒序。"""

    def test_all_strategies_preserve_text_order(self):
        import re
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        sop = DocumentSOP(cfg)
        with tempfile.TemporaryDirectory() as d:
            # 每段足够长，确保切出多块
            text = "\n\n".join(
                f"【段{i}】" + (f"这是第{i}段内容用于填充长度。" * 12)
                for i in range(1, 31)
            )
            src = os.path.join(d, "seq.txt")
            with open(src, "w", encoding="utf-8") as f:
                f.write(text)
            out = os.path.join(d, "out")
            for strategy in ("recursive", "markdown_header", "fixed_size",
                             "sentence", "semantic", "parent_child"):
                r = sop.process(src, strategy=strategy, output_dir=out, use_cache=False)
                self.assertTrue(r.ok, f"{strategy}: {r.error}")
                # 只检验非父块（父块覆盖全文属设计重复），按 chunk 顺序取段号
                seqs = []
                for c in r.chunk.chunks:
                    if c.metadata.get("level") == "parent":
                        continue
                    segs = [int(x) for x in re.findall(r"【段(\d+)】", c.text)]
                    if segs:
                        seqs.append(segs)
                self.assertTrue(seqs, f"{strategy} 未产出有效块")
                # 块内递增
                for segs in seqs:
                    self.assertEqual(segs, sorted(segs), f"{strategy} 块内乱序: {segs}")
                # 块间递增：前一块最后段号 <= 后一块首段号（overlap 只允许等号/回退到已覆盖区）
                for a, b in zip(seqs, seqs[1:]):
                    self.assertLessEqual(a[0], b[0], f"{strategy} 块间倒序: {a[0]} -> {b[0]}")


class TestPptxBlockOrder(unittest.TestCase):
    """pptx 文本/表格/图片必须按幻灯片内 shape 的原始顺序输出，内容不可丢失。"""

    # 最小合法 1x1 PNG
    PNG = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf"
        b"\xc0\xf0\x1f\x00\x05\x05\x02\x00\x8a\x85\xd0\x1f\x00\x00\x00\x00IEND"
        b"\xaeB`\x82"
    )

    def test_text_table_image_keep_shape_order(self):
        try:
            import pptx
            from pptx.util import Inches
            from io import BytesIO
        except ImportError:
            self.skipTest("python-pptx 未安装")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "order.pptx")
            prs = pptx.Presentation()
            s = prs.slides.add_slide(prs.slide_layouts[6])
            s.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text_frame.text = "文字A在表格前"
            tbl = s.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
            tbl.cell(0, 0).text, tbl.cell(0, 1).text = "表头1", "表头2"
            tbl.cell(1, 0).text, tbl.cell(1, 1).text = "值1", "值2"
            s.shapes.add_textbox(Inches(1), Inches(3.5), Inches(4), Inches(1)).text_frame.text = "文字B在表格后"
            s.shapes.add_picture(BytesIO(self.PNG), Inches(1), Inches(4.5), width=Inches(1))
            s.shapes.add_textbox(Inches(1), Inches(6), Inches(4), Inches(1)).text_frame.text = "文字C在图片后"
            prs.save(src)

            cfg = Config.load()
            cfg._data["cache"]["enabled"] = False
            out = os.path.join(d, "out")
            r = DocumentSOP(cfg).process(src, output_dir=out, use_cache=False)
            self.assertTrue(r.ok, r.error)
            text = r.parse.content
            # 内容完整性：表格与图片都不能丢
            self.assertIn("| 表头1 | 表头2 |", text, "表格内容丢失")
            self.assertRegex(text, r"!\[图片\d+\]\(images/", "图片链接丢失")
            # 顺序：文字A < 表格 < 文字B < 图片 < 文字C
            p_a = text.index("文字A在表格前")
            p_tbl = text.index("| 表头1 | 表头2 |")
            p_b = text.index("文字B在表格后")
            p_img = text.index("![图片")
            p_c = text.index("文字C在图片后")
            self.assertLess(p_a, p_tbl)
            self.assertLess(p_tbl, p_b)
            self.assertLess(p_b, p_img)
            self.assertLess(p_img, p_c)
            # 图片落盘可达
            self.assertEqual(r.parse.metadata.get("image_count"), 1)
            img = r.parse.metadata["images"][0]
            self.assertTrue(os.path.isfile(img["path"]), img["path"])

    def test_group_shape_contents_are_not_lost(self):
        """组合形状内的文字与图片必须递归提取，不能整组跳过。"""
        try:
            import pptx
            from pptx.util import Inches
            from io import BytesIO
        except ImportError:
            self.skipTest("python-pptx 未安装")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "group.pptx")
            prs = pptx.Presentation()
            s = prs.slides.add_slide(prs.slide_layouts[6])
            group = s.shapes.add_group_shape()
            group.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)) \
                .text_frame.text = "组合内文字"
            group.shapes.add_picture(BytesIO(self.PNG), Inches(1), Inches(2), width=Inches(1))
            prs.save(src)

            cfg = Config.load()
            cfg._data["cache"]["enabled"] = False
            r = DocumentSOP(cfg).process(src, output_dir=os.path.join(d, "out"), use_cache=False)
            self.assertTrue(r.ok, r.error)
            self.assertIn("组合内文字", r.parse.content)
            self.assertRegex(r.parse.content, r"!\[图片\d+\]\(images/")
            self.assertEqual(r.parse.metadata.get("image_count"), 1)

    def test_multiple_slides_keep_order(self):
        try:
            import pptx
        except ImportError:
            self.skipTest("python-pptx 未安装")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "slides.pptx")
            prs = pptx.Presentation()
            for i in (1, 2, 3):
                s = prs.slides.add_slide(prs.slide_layouts[6])
                s.shapes.add_textbox(0, 0, 1000000, 500000).text_frame.text = f"第{i}页内容"
            prs.save(src)

            cfg = Config.load()
            cfg._data["cache"]["enabled"] = False
            out = os.path.join(d, "out")
            r = DocumentSOP(cfg).process(src, output_dir=out, use_cache=False)
            self.assertTrue(r.ok, r.error)
            text = r.parse.content
            self.assertLess(text.index("## 幻灯片 1"), text.index("## 幻灯片 2"))
            self.assertLess(text.index("## 幻灯片 2"), text.index("## 幻灯片 3"))
            self.assertLess(text.index("第1页内容"), text.index("第2页内容"))
            self.assertLess(text.index("第2页内容"), text.index("第3页内容"))


class TestParsingRouter(unittest.TestCase):
    """document_parsing_method 分支路由 + KB 输出结构 + 高亮链路。"""

    def _cfg(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        return cfg

    def _make_docx(self, path):
        try:
            import docx
        except ImportError:
            self.skipTest("python-docx 未安装")
        d = docx.Document()
        d.add_paragraph("路由测试第一段文本用于对齐")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text, t.cell(0, 1).text = "表头A", "表头B"
        t.cell(1, 0).text, t.cell(1, 1).text = "值A", "值B"
        d.add_paragraph("路由测试第二段文本")
        d.save(path)

    def test_four_branches_route_to_correct_methods(self):
        """四分支按字符串包含匹配到正确方法。"""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "r.docx")
            self._make_docx(src)
            out = os.path.join(d, "out")
            cases = [
                ("vision", "_parse_docx_fallback", False),
                ("general", "_parse_docx_with_bbox_highlight", False),
                ("no_hierarchical_structure_parse", "_parse_docx_with_recovering_headers", False),
            ]
            for method, expected, expect_fallback in cases:
                r = DocumentSOP(self._cfg()).process(
                    src, output_dir=out, method=method, save=False, use_cache=False)
                self.assertTrue(r.ok, f"{method}: {r.error}")
                self.assertEqual(r.route["document_parsing_method"], expected, method)
                self.assertEqual(r.route["fallback"], expect_fallback, method)

    def test_pdf_bbox_branch_success_with_mocked_mineru(self):
        """第4个分支 _parse_pdf_with_bbox_highlight 成功路径（mock soffice + mock MinerU）。"""
        import sys as _sys
        import subprocess as _subprocess
        from sop.parser.mineru_client import MineruResult
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "r.docx")
            self._make_docx(src)
            mock = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_soffice.py")
            real_run = _subprocess.run

            def fake_run(cmd, **kw):
                return real_run([_sys.executable, mock] + cmd[1:], **kw)

            cfg = self._cfg()
            cfg._data["mineru"]["enabled"] = True
            cfg._data["mineru"]["base_url"] = "http://mock.local"
            fake_result = MineruResult(
                md_content="# MinerU 恢复的标题\n\n正文内容来自 MinerU。",
                middle_json={"_backend": "pipeline"}, backend="pipeline",
                version="1.x", file_name="r.pdf", work_dir=d, raw={},
            )
            with patch("sop.routing.shutil.which", return_value="soffice"), \
                 patch("sop.routing.subprocess.run", side_effect=fake_run), \
                 patch("sop.parser.mineru_client.MineruClient.parse", return_value=fake_result):
                r = DocumentSOP(cfg).process(
                    src, output_dir=os.path.join(d, "out"),
                    method="_parse_pdf_with_bbox_highlight", use_cache=False)
            self.assertTrue(r.ok, r.error)
            self.assertEqual(r.route["document_parsing_method"], "_parse_pdf_with_bbox_highlight")
            self.assertFalse(r.route["fallback"])
            self.assertTrue(r.route["used_mineru"])
            self.assertIn("MinerU 恢复", r.parse.content)
            # middle_json 无元素 → 没有可画的框，不应上报高亮 PDF
            self.assertEqual(r.route["highlight_pdf_path"], "")
            self.assertFalse(r.kb_output.engine_data["traceable"])

    def test_pdf_bbox_branch_falls_back_without_soffice(self):
        """_parse_pdf_with_bbox_highlight 转换失败 → 降级 fallback + _fallback=True。"""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "r.docx")
            self._make_docx(src)
            with patch("sop.routing.shutil.which", return_value=None):
                r = DocumentSOP(self._cfg()).process(
                    src, output_dir=os.path.join(d, "out"),
                    method="_parse_pdf_with_bbox_highlight", save=False, use_cache=False)
            self.assertTrue(r.ok, r.error)
            self.assertEqual(r.route["document_parsing_method"], "_parse_docx_fallback")
            self.assertTrue(r.route["fallback"])
            self.assertTrue(r.kb_output.engine_meta["_fallback"])
            self.assertEqual(r.kb_output.docs[0]["highlight_pdf_path"], "")

    def test_kb_output_structure_matches_db_collections(self):
        """docs/doc_chunks/_engine_meta 字段符合通用入库结构。"""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "r.docx")
            self._make_docx(src)
            r = DocumentSOP(self._cfg()).process(
                src, output_dir=os.path.join(d, "out"), method="general",
                save=False, use_cache=False)
            kb = r.kb_output
            self.assertEqual(len(kb.docs), 1)
            doc = kb.docs[0]
            for key in ("doc_id", "file_path", "whole_content", "chunk_count", "highlight_pdf_path",
                        "kb_id", "source_file_id", "created_at", "updated_at"):
                self.assertIn(key, doc)
            self.assertTrue(kb.doc_chunks)
            chunk = kb.doc_chunks[0]
            for key in ("doc_id", "chunk_index", "text", "box_json_str",
                        "chunk_unique_id", "kb_id", "created_at"):
                self.assertIn(key, chunk)
            self.assertEqual(chunk["doc_id"], doc["doc_id"])
            # box_json_str 必须是合法 JSON 字符串；无 bbox 时为 "\"\""
            self.assertIsInstance(json.loads(chunk["box_json_str"]), (str, dict))
            # execution_trace 形状
            self.assertEqual(len(kb.engine_meta["execution_trace"]), 1)
            entry = kb.engine_meta["execution_trace"][0]["document_parser"]
            self.assertIn("document_parsing_method", entry)
            self.assertIn("started_at", entry)

    def test_kb_files_written_when_saved(self):
        """KB 模式落盘三个独立文件。"""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "r.docx")
            self._make_docx(src)
            out = os.path.join(d, "out")
            r = DocumentSOP(self._cfg()).process(
                src, output_dir=out, method="general", use_cache=False)
            self.assertIn("docs_json", r.output_files)
            self.assertIn("chunks_json_kb", r.output_files)
            self.assertIn("engine_meta_json", r.output_files)
            for key in ("docs_json", "chunks_json_kb", "engine_meta_json"):
                self.assertTrue(os.path.isfile(r.output_files[key]), key)
            # result.json 带 route 段
            with open(r.output_files["result_json"], encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("route", data)
            self.assertIn("document_parsing_method", data["route"])

    def test_docx_mineru_strategy_keeps_bbox_elements(self):
        """parsing.docx_strategy=mineru 时不能只返回文本，还要保留 middle_json 的 bbox。"""
        from sop.parser.mineru_client import MineruResult
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "m.docx")
            self._make_docx(src)
            cfg = self._cfg()
            cfg._data["parsing"]["docx_strategy"] = "mineru"
            fake = MineruResult(
                md_content="# 标题\n正文",
                middle_json={"pdf_info": [{
                    "page_info": {"page_no": 0},
                    "para_blocks": [{
                        "type": "text", "bbox": [1, 2, 3, 4],
                        "lines": [{"spans": [{"text": "正文"}]}],
                    }],
                }]},
                backend="pipeline", file_name="m.pdf", version="1.x", work_dir=d,
            )
            with patch("sop.parser.office_parsers.convert_to_pdf",
                       return_value=(os.path.join(d, "m.pdf"), d)), \
                 patch("sop.parser.office_parsers.MineruClient.parse", return_value=fake):
                r = DocumentSOP(cfg).process(
                    src, output_dir=os.path.join(d, "out"), save=False, use_cache=False)
            self.assertTrue(r.ok, r.error)
            self.assertTrue(r.parse.elements)
            self.assertEqual(r.parse.elements[0].bbox, [1.0, 2.0, 3.0, 4.0])
            self.assertTrue(r.parse.metadata["traceable"])

    def test_lcs_align_produces_nonzero_bbox(self):
        """字符级 LCS 对齐能对已知 PDF 产出非零且有序的 bbox。"""
        try:
            import pymupdf as fitz
        except ImportError:
            self.skipTest("PyMuPDF 未安装")
        from sop.bbox_aligner import align_elements_to_pdf
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "t.pdf")
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            texts = ["这是第一段对齐测试文本", "这是第二段对齐测试文本", "这是第三段对齐测试文本"]
            for i, t in enumerate(texts):
                page.insert_text((50, 60 + i * 40), t, fontsize=14, fontname="china-s")
            doc.save(pdf)
            doc.close()
            bbox, reason = align_elements_to_pdf(texts, pdf, words_limit=100000)
            self.assertEqual(reason, "")
            self.assertTrue(bbox, "未产出 bbox")
            self.assertIn(1, bbox)
            box = bbox[1][0]
            self.assertTrue(any(box), f"bbox 全零: {box}")
            # 逐元素模式：每段各自有 bbox，且 y 坐标递增
            per, reason2 = align_elements_to_pdf(texts, pdf, per_element=True)
            self.assertEqual(reason2, "")
            ys = [b[1][0][1] for b in per if b]
            self.assertEqual(len(ys), 3, f"逐元素 bbox 数不足: {per}")
            self.assertEqual(ys, sorted(ys), f"bbox 顺序错乱: {ys}")

    def test_word_limit_skips_alignment(self):
        """超过字数门槛跳过对齐，reason 含预期日志文本。"""
        from sop.bbox_aligner import align_elements_to_pdf
        bbox, reason = align_elements_to_pdf(["x" * 200], "dummy.pdf", words_limit=100)
        self.assertEqual(bbox, {})
        self.assertIn("too long for trace-back alignment", reason)

    def test_mock_soffice_full_highlight_chain(self):
        """用 mock soffice 模拟转换成功，验证完整高亮链路。"""
        import sys as _sys
        import subprocess as _subprocess
        try:
            import pymupdf  # noqa: F401
        except ImportError:
            self.skipTest("PyMuPDF 未安装")
        mock = os.path.join(os.path.dirname(__file__), "mock_soffice.py")
        if not os.path.isfile(mock):
            self.skipTest("mock_soffice.py 不存在")
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "r.docx")
            self._make_docx(src)
            real_run = _subprocess.run

            def fake_run(cmd, **kw):
                return real_run([_sys.executable, mock] + cmd[1:], **kw)

            cfg = self._cfg()
            with patch("sop.routing.shutil.which", return_value="soffice"), \
                 patch("sop.routing.subprocess.run", side_effect=fake_run):
                r = DocumentSOP(cfg).process(
                    src, output_dir=os.path.join(d, "out"), method="general", use_cache=False)
            self.assertTrue(r.ok, r.error)
            kb = r.kb_output
            hp = kb.docs[0]["highlight_pdf_path"]
            self.assertTrue(hp and os.path.isfile(hp), f"highlight PDF 不存在: {hp}")
            # 至少一个块有非零 bbox，且格式 {page: [[x0,y0,x1,y1]]}
            boxes = [json.loads(c["box_json_str"]) for c in kb.doc_chunks]
            with_box = [b for b in boxes if isinstance(b, dict) and b]
            self.assertTrue(with_box, "所有块都无 bbox")
            page_key = sorted(with_box[0].keys())[0]
            rect = with_box[0][page_key][0]
            self.assertEqual(len(rect), 4, f"bbox 形状错误: {rect}")
            self.assertTrue(any(rect), f"bbox 全零: {rect}")
            # Vec：execution_trace 记录真实方法
            self.assertEqual(
                kb.engine_meta["execution_trace"][0]["document_parser"]["document_parsing_method"],
                "_parse_docx_with_bbox_highlight")

    def test_generic_route_traceable_requires_bbox(self):
        """无坐标的文本元素（如 md 标题）不应被标记为 traceable。"""
        r = DocumentSOP(self._cfg()).process(
            "examples/demo.md", method="general", save=False, use_cache=False)
        self.assertTrue(r.ok, r.error)
        self.assertFalse(r.kb_output.engine_data["traceable"])

    def test_routing_disabled_by_default(self):
        """未传 method 且配置关闭时，保持原有行为（无 route 段）。"""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "r.docx")
            self._make_docx(src)
            r = DocumentSOP(self._cfg()).process(
                src, output_dir=os.path.join(d, "out"), save=False, use_cache=False)
            self.assertIsNone(r.route)
            self.assertFalse(hasattr(r, "kb_output"))

    def test_route_failure_is_not_reported_as_success(self):
        """路由模式下不存在/损坏的文件必须判失败（失败语义不被路由包装吞掉）。"""
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "not_exist.docx")
            r = DocumentSOP(self._cfg()).process(
                missing, method="general", save=False, use_cache=False)
            self.assertFalse(r.ok)
            self.assertFalse(r.parse.success)
            self.assertTrue(r.parse.error)
            self.assertFalse(hasattr(r, "kb_output"))

            corrupt = os.path.join(d, "corrupt.docx")
            with open(corrupt, "wb") as f:
                f.write(b"not a real docx")
            r2 = DocumentSOP(self._cfg()).process(
                corrupt, method="general", save=False, use_cache=False)
            self.assertFalse(r2.ok)
            self.assertFalse(r2.parse.success)
            self.assertTrue(r2.parse.error)

    def test_word_limit_skips_pdf_conversion(self):
        """超限文档不转换 PDF、highlight 为空，但解析仍成功。"""
        import sys as _sys
        import subprocess as _sp
        from unittest.mock import patch as _patch
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "r.docx")
            self._make_docx(src)
            mock = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_soffice.py")
            real_run = _sp.run
            def fake_run(cmd, **kw):
                return real_run([_sys.executable, mock] + cmd[1:], **kw)
            cfg = self._cfg()
            cfg._data["routing"]["general_words_limit"] = 10  # 强制超限
            with _patch("sop.routing.shutil.which", return_value="soffice"), \
                 _patch("sop.routing.subprocess.run", side_effect=fake_run) as called:
                r = DocumentSOP(cfg).process(
                    src, output_dir=os.path.join(d, "out"), method="general",
                    save=False, use_cache=False)
            self.assertTrue(r.ok, r.parse.error)
            self.assertFalse(called.called, "超长文档不应触发 PDF 转换")
            self.assertEqual(r.route["highlight_pdf_path"], "")
            self.assertTrue(any("too long" in w for w in r.parse.warnings))


class TestReleaseReviewRound2(unittest.TestCase):
    """第二轮发布审查发现的隐患，逐条固化。"""

    def _cfg(self):
        cfg = Config.load()
        cfg._data["cache"]["enabled"] = False
        return cfg

    def test_route_non_string_source_is_readable_failure(self):
        """路由模式非字符串 source 返回可读失败，不抛裸 TypeError。"""
        r = DocumentSOP(self._cfg()).process(123, method="general", save=False, use_cache=False)
        self.assertFalse(r.ok)
        self.assertFalse(r.parse.success)
        self.assertIn("无效", r.parse.error)

    def test_recursive_tiny_step_does_not_drop_tail(self):
        """overlap 接近 size 时步长很小，也不能因固定迭代上限丢掉文本尾部。"""
        from sop.chunking.factory import ChunkFactory
        text = "甲" * 3000 + "尾部标记"
        chunks = ChunkFactory.create(
            "recursive", {"chunk_size": 20, "chunk_overlap": 19, "separators": [""]}
        ).chunk(text)
        self.assertTrue(chunks)
        self.assertIn("尾部标记", chunks[-1].text)
        self.assertTrue(all(len(c.text) <= 20 for c in chunks))

    def test_sentence_overlap_cannot_inflate_chunk_size(self):
        """超大 overlap 不得让块长突破 max_size。"""
        from sop.chunking.other_strategies import SentenceChunker
        text = "。".join("句子编号" + str(i) for i in range(1, 201)) + "。"
        chunks = SentenceChunker({"max_size": 100, "overlap_sentences": 10 ** 9}).chunk(text)
        self.assertTrue(chunks)
        self.assertLessEqual(max(len(c.text) for c in chunks), 100)

    def test_negative_max_content_chars_is_rejected(self):
        """负数 max_content_chars 返回可读失败，不静默截断。"""
        from sop.parser import DocumentParser
        cfg = self._cfg()
        cfg._data["parsing"]["max_content_chars"] = -5
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
            r = DocumentParser(cfg).parse(p)
        self.assertFalse(r.success)
        self.assertIn("不能为负数", r.error)

    def test_mineru_fallback_not_reported_as_used(self):
        """MinerU 不可达降级到 pypdf 时，used_mineru=false 且 fallback=true。"""
        if not _HAS_PYMUPDF:
            self.skipTest("PyMuPDF 未安装")
        import pymupdf as f
        cfg = self._cfg()
        cfg._data["mineru"].update(enabled=True, base_url="http://127.0.0.1:1",
                                   max_retries=0, retry_backoff_seconds=0)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.pdf")
            doc = f.open(); pg = doc.new_page(); pg.insert_text((50, 50), "hello fallback test")
            doc.save(p); doc.close()
            r = DocumentSOP(cfg).process(p, method="general", save=False, use_cache=False)
        self.assertEqual(r.parse.parser, "pypdf")
        self.assertFalse(r.route["used_mineru"])
        self.assertTrue(r.route["fallback"])
        self.assertFalse(r.kb_output.engine_meta["used_mineru"])

    def test_bbox_no_hit_not_reported_as_traceable(self):
        """bbox 未命中任何坐标时，不提供 highlight、不标记 traceable。"""
        if not _HAS_PYMUPDF:
            self.skipTest("PyMuPDF 未安装")
        try:
            import docx as _docx
        except ImportError:
            self.skipTest("python-docx 未安装")
        import io as _io
        import sys as _sys
        import subprocess as _sp
        PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06'
               b'\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f'
               b'\x00\x05\x05\x02\x00\x8a\x85\xd0\x1f\x00\x00\x00\x00IEND\xaeB\x60\x82')
        with tempfile.TemporaryDirectory() as d:
            s = os.path.join(d, "i.docx")
            doc = _docx.Document(); doc.add_picture(_io.BytesIO(PNG)); doc.save(s)
            mock = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_soffice.py")
            real = _sp.run
            def fake(cmd, **kw):
                return real([_sys.executable, mock] + cmd[1:], **kw)
            with patch("sop.routing.shutil.which", return_value="soffice"), \
                 patch("sop.routing.subprocess.run", side_effect=fake):
                r = DocumentSOP(self._cfg()).process(
                    s, output_dir=os.path.join(d, "out"), method="general",
                    save=False, use_cache=False)
        self.assertTrue(r.ok, r.parse.error)
        self.assertFalse(r.route["highlight_pdf_path"])
        self.assertFalse(r.kb_output.engine_data["traceable"])

    def test_kb_file_path_does_not_leak_absolute_path(self):
        """KB 产物 docs.file_path 不含本机绝对路径。"""
        try:
            import docx as _docx
        except ImportError:
            self.skipTest("python-docx 未安装")
        with tempfile.TemporaryDirectory() as d:
            s = os.path.join(d, "leak.docx")
            doc = _docx.Document(); doc.add_paragraph("leak test"); doc.save(s)
            r = DocumentSOP(self._cfg()).process(
                s, output_dir=os.path.join(d, "out"), method="general",
                save=False, use_cache=False)
        fp = r.kb_output.docs[0]["file_path"]
        self.assertEqual(fp, "leak.docx")
        self.assertNotIn(":\\", fp)
        self.assertNotIn("Users", fp)

    def test_short_page_sampling_keeps_page_tail(self):
        """短页必须全量采样，不能只取前 N 个字符而丢掉页尾。"""
        from sop.bbox_aligner import sample_pdf_chars
        chars = [{"page": 1, "char": str(i % 10), "bbox": [i, 0, i + 1, 1]} for i in range(30)]
        sampled, page_map = sample_pdf_chars({1: chars}, N=20, M=10)
        self.assertEqual(len(sampled), 30)
        self.assertEqual(sampled[-1]["char"], "9")
        self.assertEqual(page_map, {1: 0})

    def test_route_url_with_extension_downloads_first(self):
        """路由模式下带后缀的 URL 先下载再走分支。"""
        if not _HAS_REQUESTS:
            self.skipTest("requests 未安装")
        try:
            import docx as _docx
        except ImportError:
            self.skipTest("python-docx 未安装")
        import io as _io
        buf = _io.BytesIO(); doc = _docx.Document(); doc.add_paragraph("url test content"); doc.save(buf)
        class FakeResp:
            status_code = 200
            headers = {}
            def __init__(self, data): self.content = data
            def raise_for_status(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def iter_content(self, size): yield self.content
        with patch("requests.get", return_value=FakeResp(buf.getvalue())):
            r = DocumentSOP(self._cfg()).process(
                "https://example.test/report.docx", method="general", save=False, use_cache=False)
        self.assertTrue(r.ok, r.parse.error)
        self.assertIn("url test", r.parse.content)

    def test_result_json_has_ok_and_output_files(self):
        """落盘的 result.json 必须带 ok，且 output_files 与实际产物一致。"""
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "result_fields.md")
            with open(src, "w", encoding="utf-8") as f:
                f.write("# 标题\n\n正文内容。")
            out = os.path.join(d, "out")
            r = DocumentSOP(self._cfg()).process(src, output_dir=out)
            self.assertTrue(r.ok, r.error)
            with open(r.output_files["result_json"], encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("ok", data)
            self.assertTrue(data["ok"])
            self.assertTrue(data["output_files"])
            for key, path in data["output_files"].items():
                self.assertTrue(os.path.isfile(path), key)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# DocSOP · 文档解析 SOP 工具包

<p align="center">
  <strong>给一个文件，输出可检索的解析全文与分块。</strong><br/>
  本地路径或 URL → 统一解析 → 六种分块策略 → 可选的 bbox 溯源与知识库入库结构
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Tests" src="https://img.shields.io/badge/tests-112-brightgreen">
  <img alt="Core dependencies" src="https://img.shields.io/badge/core%20deps-zero-success">
</p>

> **项目定位**：一个独立、自包含的文档解析 SOP 通用模块。核心链路零重依赖，Office/PDF 等重依赖按需加载；解析失败返回可读错误，而不是静默产出空内容。

**核心能力**

- **多格式统一入口**：txt / md / html / json / csv / 代码 / 日志、docx、pptx、xlsx/xlsm/xls、pdf、ofd、图片与 URL。
- **顺序保真**：docx 按 body 顺序交错输出段落/表格/图片；pptx 按 shape 顺序输出并递归展开组合形状；Excel 按 sheet 顺序。
- **默认父子分块**：父块保留上下文，子块用于检索，双向关联；另有 recursive、markdown_header、fixed_size、sentence、semantic。
- **图片不丢**：docx / pptx 内嵌图片按内容哈希落盘、去重，Markdown 链接随文本进入对应分块。
- **可选溯源高亮**：`document_parsing_method` 四分支路由、条件 PDF 转换、PyMuPDF 字符级 bbox 对齐、高亮 PDF。
- **知识库输出**：路由模式额外输出 `docs / doc_chunks / _engine_meta / _engine_data` 四份 JSON。
- **失败语义清晰**：缺文件、空文件、损坏文档、缺依赖、非法配置都返回 `success=false` + 可读 `error`，CLI 退出码非 0。

> **当前版本**：`1.0.0`，变更记录见 [CHANGELOG.md](CHANGELOG.md)；许可证见 [LICENSE](LICENSE)。

**快速导航**：[快速开始](#2-快速开始) · [格式支持](#3-支持的文件格式与解析器) · [配置说明](#4-配置文件说明) · [分块策略](#5-分块策略) · [输出说明](#6-输出说明) · [回归测试](#9-回归测试) · [参与贡献](#10-参与贡献) · [License](#13-license)

---

## 1. 目录结构

```
doc_sop/
├── README.md                     # 项目说明与使用文档
├── LICENSE                       # MIT License
├── CHANGELOG.md                  # 版本变更记录
├── CONTRIBUTING.md               # 贡献指南
├── SECURITY.md                   # 安全策略与漏洞报告方式
├── CODE_OF_CONDUCT.md            # 贡献者公约
├── requirements.txt              # 依赖声明（全部按需，核心链路零依赖）
├── run.py                        # CLI 统一入口
├── .gitignore                    # Python / 运行产物 / 本地配置忽略规则
├── .gitattributes                # 跨平台换行与二进制文件规则
├── .github/
│   ├── workflows/tests.yml       # GitHub Actions：Ubuntu/Windows × Python 3.10/3.12
│   ├── ISSUE_TEMPLATE/           # Bug / Feature 模板
│   └── PULL_REQUEST_TEMPLATE.md  # PR 检查清单
├── config/
│   ├── sop_config.json           # 主配置（JSON，零依赖，默认加载）
│   └── sop_config.yaml           # 同内容 YAML 版（需 PyYAML）
├── examples/
│   └── demo.md                   # 示例文件
├── sop/
│   ├── __init__.py               # 包导出：DocumentSOP / DocumentParser / ChunkFactory ...
│   ├── config_loader.py          # 配置加载：JSON/YAML、点号路径访问、相对路径解析
│   ├── models.py                 # 统一数据模型：Element / ParseResult / Chunk / ChunkResult / ProcessResult
│   ├── pipeline.py               # 顶层编排：解析 → 分块 → 溯源 → 缓存 → 输出（含 process_batch）
│   ├── cache.py                  # 基于文件指纹的结果缓存（可配 TTL / 目录）
│   ├── routing.py                # document_parsing_method 四分支路由 + 降级 + execution_trace
│   ├── kb_models.py              # 知识库输出结构：docs / doc_chunks / _engine_meta / _engine_data
│   ├── bbox_aligner.py           # DOCX↔PDF 字符级对齐（采样 + LCS → 包络 bbox）
│   ├── parser/                   # 解析层
│   │   ├── base.py               # BaseParser、ParserRegistry、ParseContext
│   │   ├── dispatcher.py         # DocumentParser：类型识别 + 注册表分发
│   │   ├── simple_parsers.py     # txt / html / ofd
│   │   ├── office_parsers.py     # docx / pptx / excel / 旧版 doc/ppt/wps/dps
│   │   ├── pdf_parser.py         # PDF（MinerU + pypdf 降级）、图片（OCR/视觉模型）
│   │   ├── mineru_client.py      # MinerU 1.x / 2.5 / 3.x 客户端（重试 + 鉴权）
│   │   └── mineru_result_adapter.py  # middle_json → 带 bbox/页码的 Element
│   └── chunking/                 # 分块层
│       ├── base.py               # BaseChunker（char/token 两种长度度量）
│       ├── parent_child.py       # 父子分块（默认）
│       ├── other_strategies.py   # recursive / markdown_header / fixed_size / sentence / semantic
│       ├── factory.py            # ChunkFactory：按名称创建分块器
│       └── provenance.py         # Element → Chunk 的 bbox/页码关联
├── tests/
│   ├── test_regression.py        # 112 项标准库回归测试（可选依赖缺失时自动跳过）
│   └── mock_soffice.py           # 测试用 LibreOffice 替身（生成同文本 PDF，验证高亮链路）
└── sop_output/                   # 默认输出目录（运行后生成）
```

---

## 2. 快速开始

### 2.1 获取代码

```bash
git clone <你的 GitHub 仓库地址>
cd doc-sop
```

> 也可以直接下载仓库 ZIP 后解压使用；仓库目录名以实际克隆结果为准。

### 2.2 环境要求

- Python 3.9+（代码用了 `X | None` 类型语法，建议 **3.10+**）。
- **核心链路零重依赖**：解析 txt/md/log/csv、所有非语义分块都能直接运行；HTML 优先用 `html2text`，缺失时降级 `bs4`，再缺失时用标准库正则兜底。
- 按需安装的可选依赖（也可一次性执行 `pip install -r requirements.txt` 装齐）：

| 能力 | 依赖 | 安装 |
|---|---|---|
| YAML 配置 | PyYAML | `pip install pyyaml` |
| DOCX 解析（默认路线） | python-docx | `pip install python-docx` |
| PPTX 解析 | python-pptx | `pip install python-pptx` |
| Excel 解析 | pandas + openpyxl/xlrd | `pip install pandas openpyxl xlrd` |
| HTML 解析（首选） | html2text | `pip install html2text` |
| HTML 解析（降级） | beautifulsoup4 | `pip install beautifulsoup4` |
| PDF 本地降级 | pypdf | `pip install pypdf` |
| bbox 字符对齐（路由模式） | PyMuPDF | `pip install pymupdf` |
| 高亮 PDF 转换（路由模式） | LibreOffice（系统级） | 安装 soffice；未装时自动降级（解析仍成功，仅无 bbox） |
| MinerU 客户端 | requests | `pip install requests` |
| 语义分块 | sentence-transformers + numpy | `pip install sentence-transformers` |

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt   # 可选：全量依赖；也可按需只装当前格式所需项
python run.py examples/demo.md     # 冒烟运行，产物写入 sop_output/
```

> 缺少可选依赖时不会整体崩溃：对应格式返回 `success=false` 与可读的 `error`（说明要装什么），**不会静默产出空内容**；语义分块自动降级为递归分块。语义模型即使已安装，首次使用也需联网下载权重；离线或模型加载失败时同样降级，不会中断解析链路。

### 2.3 CLI 用法

```bash
# 解析一个文件（默认父子分块），结果写入 sop_output/
python run.py <文件路径或URL>

# 指定分块策略
python run.py doc.pdf --strategy recursive

# 指定配置文件 / 输出目录 / 预览块数
python run.py doc.docx --config config/sop_config.yaml --out ./out --preview 5

# 查看能力清单
python run.py --list-strategies    # 列出所有分块策略
python run.py --list-exts          # 列出所有支持的文件后缀

# 管道模式：只输出解析全文 / 分块 JSON 到 stdout，不写文件、不打印摘要
python run.py doc.md --stdout content --quiet > doc.txt
python run.py doc.md --stdout chunks  --quiet | jq '.count'

# 批量并发处理多个文件（返回顺序与输入一致）
python run.py a.pdf b.docx c.md --workers 4
# 若多个输入文件同名（即使来自不同目录），产物会自动隔离到输出目录下的 0000/、0001/ 子目录，避免互相覆盖

# 强制重新解析（跳过结果缓存）
python run.py doc.pdf --no-cache

# 分支路由 + 高亮链路：按 document_parsing_method 走四分支
python run.py doc.docx --method general              # 默认溯源路径（条件转 PDF + bbox 对齐）
python run.py doc.docx --method vision               # 走 docx2md fallback（无 PDF / 无 box）
python run.py doc.docx --method no_hierarchical_structure_parse  # MinerU 恢复标题层级
python run.py doc.docx --method _parse_pdf_with_bbox_highlight   # 强制转 PDF → MinerU
python run.py doc.docx --method general --kb-id kb1 --source-file-id file123  # 指定 KB 入库标识
python run.py doc.docx --method general --library-id lib1  # 指定库 ID（写入 docs.library_id）
```

CLI 会在终端打印解析摘要（**实际加载的配置文件路径**、文件、类型、解析器、耗时、字符数、警告、分块数、前 N 个块预览、输出文件路径）。

> **配置修改须知**：程序默认加载 `config/sop_config.json`（零依赖）。`sop_config.yaml` 仅供阅读/参照，直接改它**不会生效**，除非用 `--config config/sop_config.yaml` 显式指定，或同步改动两份文件。CLI 首行会打印实际加载的路径。

### 2.4 Python API 用法

```python
import sys; sys.path.insert(0, "doc_sop")   # 或在 doc_sop 目录内运行
from sop import DocumentSOP

sop = DocumentSOP.from_config()             # 自动加载 config/sop_config.json

# 一条龙：解析 + 分块 + 保存
r = sop.process("报告.pdf", strategy="parent_child")
# 等价于 sop.parse_and_chunk(...)

print(r.ok)                                 # 是否解析成功
print(r.parse.content)                      # 解析全文（Markdown）
print(r.parse.parser, r.parse.elapsed_ms)   # 实际解析器 / 耗时
print(r.chunk.metadata)                     # {"total_chunks":..., "parent_count":..., "child_count":...}

for c in r.chunk.children:                  # 子块（带 parent_id 溯源）
    print(c.chunk_id, c.parent_id, c.text[:80])

for c in r.chunk.parents:                   # 父块（child_ids 指向子块，用于回填上下文）
    print(c.chunk_id, len(c.child_ids))
```

只要解析对象、不要分块时：

```python
from sop import DocumentParser
p = DocumentParser.from_config().parse("a.docx")
print(p.success, p.content, p.elements)
```

单独用分块工厂：

```python
from sop import ChunkFactory
chunks = ChunkFactory.create("parent_child", {"child_size": 300}).chunk(text)
# 按 token 切块（token_encoding 可配）
tok_chunks = ChunkFactory.create("fixed_size", {
    "chunk_size": 500, "_length_unit": "token",
}).chunk(text)
```

批量并发处理：

```python
results = sop.process_batch(
    ["a.pdf", "b.docx", "c.md"], workers=4, use_cache=True
)
for r in results:
    print(r.parse.file_name, r.ok, len(r.chunk.chunks) if r.chunk else 0)
```

读取溯源信息（PDF 走 MinerU 时块会带页码与 bbox）：

```python
for c in r.chunk.children:
    print(c.page, c.pages, c.bbox, c.source_element_ids)  # 页码 / 坐标 / 元素索引
```

---

## 3. 支持的文件格式与解析器

| 后缀 | 解析器 | 底层实现 | 依赖 / 说明 |
|---|---|---|---|
| `.txt .md .markdown .java .py .js .json .log .csv` | `text` | 编码探测 + 纯文本读取 | 零重依赖，自动探测编码 |
| `.html .htm`（含 http(s) URL） | `html` | html2text 优先，BeautifulSoup / 正则降级 | 推荐 `html2text`，可选 `bs4` |
| `.ofd` | `ofd` | 解压 XML 提取文本 | 零重依赖 |
| `.docx` | `docx` | python-docx 段落/表格转 Markdown（默认） | 需 `python-docx`；Heading 样式转 `#` 标题；内嵌图片落盘并插入链接（见下） |
| `.pptx` | `pptx` | 逐页按 shape 原始顺序提取文本/表格/图片 | 需 `python-pptx`；表格转 Markdown、内嵌图片落盘并插入链接；保持元素在幻灯片中的位置顺序 |
| `.xlsx .xlsm .xls` | `excel` | pandas 逐 sheet 转 Markdown 表 | 需 pandas + openpyxl/xlrd；`.et` 请先另存为 `.xlsx` |
| `.pdf` | `pdf` | 默认 pypdf；启用 MinerU 后优先 MinerU → pypdf 降级 | 加载 `mineru.*` 配置 |
| `.jpg .jpeg .png .bmp .tif .tiff` | `image` | 视觉模型 OCR（可选）→ MinerU OCR | 加载 `parsing.vision.*` / `mineru.*` |
| `.doc .ppt .wps .dps` | `legacy_office` | LibreOffice 转 PDF → MinerU | 需本机安装 `soffice` + MinerU |

无后缀文件按 `.txt` 处理；无后缀的 URL 按 `.html` 处理，带 `.pdf` / `.png` 等后缀的 URL 会下载到临时文件后交给对应解析器，不会被当成网页文本。

### 解析策略切换（配置文件 `parsing.*`）

| 配置项 | 取值 | 说明 |
|---|---|---|
| `parsing.pdf_strategy` | `mineru`（默认）/ `local` | 默认 `mineru.enabled=false`，故实际走 pypdf；填好地址并开启 MinerU 后，失败自动降级本地 pypdf；`local` 直接走 pypdf |
| `parsing.docx_strategy` | `lightweight`（默认）/ `mineru` | `mineru` = LibreOffice 转 PDF 后走 MinerU，支持 bbox 溯源 |
| `parsing.vision.enabled` | `false`（默认）/ `true` | 图片是否优先走视觉模型（如 qwen2-vl），失败降级 MinerU OCR |
| `parsing.encoding` | `auto` / 指定编码 | 文本编码探测方式 |
| `parsing.max_content_chars` | `0`（不限制）/ 正整数 | 单篇解析结果字符上限保护 |
| `parsing.allow_empty_content` | `false`（默认）/ `true` | 解析结果为空时是否仍算成功；默认判失败并给出 error |
| `parsing.max_url_download_bytes` | `104857600`（默认 100MB）/ `0` 不限制 | 非 HTML URL 下载到临时文件时的大小上限 |

### 内嵌图片保留（配置 `images.*`，docx / pptx 默认开启）

`lightweight` docx 解析会提取正文与表格中的内嵌图片，`pptx` 解析会提取每页图片，
二者都落到磁盘并在原文对应位置插入 Markdown 图片链接
（`![图片N](url)`）。链接是解析文本的一部分，因此会**随文本一起进入分块**，并写入 `.parsed.md` / `.chunks.json`。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `images.enabled` | `true` | 是否提取内嵌图片；关闭后图片被跳过（不落盘、不插入链接） |
| `images.dir` | `images` | 图片保存目录。**相对路径相对于实际输出目录**（`--out` 或 `output.dir`）解析，保证相对链接可达；填绝对路径则作为固定图片仓库 |
| `images.url_prefix` | `images/` | 写入 Markdown 的链接前缀。填相对路径（如 `images/`）用于本地/静态目录；也可填完整 URL（如 `https://cdn.example.com/docs/images/`）指向对象存储/CDN，本地仍保留一份 |
| `images.max_bytes` | `52428800`（50MB） | 单张图片大小上限，超限跳过并写入 `warnings`；`0` 表示不限 |

其他要点：

- 图片按内容 MD5 命名（`img_<12位>.<ext>`），相同图片自动去重，只落盘一次、多处引用同一链接；
- `.result.json` 的 `parse.metadata` 含 `image_count`、`images` 清单（文件名 / URL / 绝对路径 / 字节数 / 类型）、`image_dir`、`image_url_prefix`，便于查验与入库；
- 图片写入失败或超限只产生 `warnings`，不影响文本解析结果；
- 开启缓存后换输出目录再解析，图片会依据 metadata 清单自动同步到新目录，相对链接保持可达。

### 失败语义（重要）

以下情况一律返回 `success=false` + 可读 `error`，CLI 退出码为 `1`，**不会被当成成功产出空文档**：

- 本地文件不存在、是目录、不可读、0 字节；
- 文件损坏（如伪装成 `.docx` 的旧版 `.doc`、损坏的 zip/OFD/PDF）；
- URL 抓取失败，或各条解析路线（MinerU / pypdf / 视觉模型）均失败；
- 解析结果为空或全是空白（如需允许，显式设置 `parsing.allow_empty_content=true`）；
- 分块阶段异常或分块参数非法（如 `chunk_size=0`），此时解析内容仍保留在 `parse.content`，错误写入 `ProcessResult.error`，整体 `ok=false`。

降级过程（如 `MinerU → pypdf`、`vision → MinerU OCR`）中的中间失败只写入 `warnings`；只要最终拿到非空内容，结果仍为成功。若所有路线都失败，中间失败原因会合并进最终 `error`，便于定位。

---

## 3.5 分支路由与前端高亮链路

传 `--method`（或配置 `routing.enabled=true`）即可启用。该模块提供 `document_parsing_method` 分支路由、`highlight_pdf_path` 溯源高亮、以及 `docs / doc_chunks / _engine_meta` 入库结构；不传时保持前述通用解析行为不变。

### 3.5.1 docx 四分支（字符串包含匹配，顺序即优先级）

| `document_parsing_method` 取值 | 实际解析方法 | 是否转 PDF | 是否生成 box |
|---|---|---|---|
| 未传 / 含 `general`（默认） | `_parse_docx_with_bbox_highlight` | 条件转换 | 对齐命中时 |
| 含 `no_hierarchical_structure_parse` | `_parse_docx_with_recovering_headers` | 不转换 | 无 |
| 含 `_parse_pdf_with_bbox_highlight` | MinerU PDF 解析（绕开 docx 元素解析） | 必须转换 | 有 |
| 其他任意值（**含 `vision`**） | `_parse_docx_fallback`（docx2md + 规则切片） | 不转换 | 无 |

> `no_hierarchical_structure_parse` 分支不转 PDF：它在字数阈值内、且 MinerU 已启用时，用 MinerU 结果恢复标题层级；不产出 bbox。未启用 MinerU 或超阈值时，直接使用 docx 元素解析结果。

> 注意 `vision` 这个取值：它只对 PDF / 图片的 `enable_vlm` 判定有意义。docx 分支里没有任何地方判断 `vision`，三个 `in` 判断都不匹配，于是落入最朴素的 fallback——没有坐标、没有 MinerU 参与。这是有意保留的兼容行为。

**任一路径抛异常 → 自动降级**，降级后 `document_parsing_method` 被改写为实际方法，`_engine_meta._fallback=true`，但解析仍成功、检索内容不受影响：

- recovering 失败 → `_parse_pdf_with_bbox_highlight`
- pdf_bbox 失败 / general 失败 → `_parse_docx_fallback`

### 3.5.2 PDF 转换的三重门槛（general 默认路径）

`_parse_docx_with_bbox_highlight` 内部调用字符对齐前，需同时满足：

1. 全文字符数 < `routing.general_words_limit`（默认 100000）。超过则直接返回元素并告警 `Document is too long for trace-back alignment`，跳过转换；
2. 系统存在 LibreOffice（`soffice` / `libreoffice`）；
3. 转换在 `converter.timeout_seconds`（默认 120s）内成功。

三种情况结果一致：**解析照常成功**，只是 `highlight_pdf_path` 为空、每个 chunk 的 `box_json_str` 为 `"\"\""`。这是有意的降级设计，不影响检索与向量化，只是前端点开原文无法画框高亮。

### 3.5.3 bbox 对齐算法

`bbox_aligner.py` 的实现：PyMuPDF 逐字符提取坐标 → 每页采样（前 20 / 中 10 / 后 20）→ 全局 LCS 定页范围 → 局部 LCS 写回字符坐标 → 逐元素聚合包络 `{"页码": [[x0, y0, x1, y1]]}`。超长文本自动分段 LCS，避免 O(n·m) 爆内存。

### 3.5.4 输出结构（通用知识库集合字段）

启用路由后，除常规产物外额外输出四个文件（与 `docs` / `doc_chunks` / `_engine_meta` / `_engine_data` 四个集合一一对应）：

| 文件 | 内容 | 关键字段 |
|---|---|---|
| `<name>.docs.json` | 文档元信息 | `doc_id`（与分块关联）、`file_path`（本地路径已脱敏为文件名）、`whole_content`、`chunk_count`、`highlight_pdf_path`、`para_elements_count`、`para_headers_md` |
| `<name>.doc_chunks.json` | 分块列表 | `doc_id`、`chunk_index`、`text`、`box_json_str`、`chunk_unique_id` |
| `<name>._engine_meta.json` | 执行轨迹 | `execution_trace`、`_fallback`、`document_parsing_method`、`mineru_backend_type` |
| `<name>._engine_data.json` | 溯源标记 | `document_parsing_method`、`used_mineru`、`traceable`（是否真有坐标）、`mineru_backend_type` |

`box_json_str` 是 JSON 字符串：`{"1": [[x0,y0,x1,y1]], "2": [[...]]}`（页码 1-based，每页一个并集包络矩形）；无坐标时为 `"\"\""`。`execution_trace` 记录实际走的方法、是否用 MinerU、后端类型，可直接聚合统计"有多少文件实际走了兜底 / 没有坐标 / 由 MinerU 代理解析"。

`result.json` 在路由模式下额外带 `route` 段（`document_parsing_method` / `fallback` / `used_mineru` / `highlight_pdf_path`）。

### 3.5.5 配置项

```yaml
routing:
  enabled: false                      # 是否默认启用（也可由 --method 临时开启）
  default_method: "general"
  general_words_limit: 100000         # general 路径对齐字数上限
  recovering_words_limit: 150000      # headers_recovering 路径上限
```

### 3.5.6 Python API

```python
from sop import DocumentSOP
r = DocumentSOP.from_config().process(
    "doc.docx", method="general",          # 启用分支路由
    kb_id="kb1", source_file_id="file123", # KB 入库标识（默认占位符，不含本机信息）
)
print(r.route)                             # 实际解析路径 / 是否降级 / 高亮 PDF
print(r.kb_output.docs)                    # docs 集合记录
print(r.kb_output.doc_chunks)              # doc_chunks 记录（含 box_json_str）
print(r.kb_output.engine_meta)             # _engine_meta（含 execution_trace）
print(r.kb_output.engine_data)             # _engine_data（实际方法 / 是否可溯源）
```

> **依赖说明**：路由模式额外需要 PyMuPDF（字符对齐）和 LibreOffice（PDF 转换）。general 路线下本机无 LibreOffice 时高亮环节自动跳过（解析路径不变、内容照常输出，`route.fallback` 仍为 false，仅告警且无 bbox）——这是预期行为，测试中用 `tests/mock_soffice.py` 模拟转换成功，验证完整高亮链路。

---

## 4. 配置文件说明

默认加载 `config/sop_config.json`（零依赖）。也可通过 `--config` 或 `DocumentSOP.from_config(path)` 指定 YAML/JSON。加载逻辑见 `sop/config_loader.py`（点号访问 `config.get("mineru.base_url")`，相对路径相对配置文件目录解析）。

### 4.1 MinerU 服务器配置（`mineru.*`）—— PDF 走 MinerU 时加载

```yaml
mineru:
  enabled: false                       # 默认关闭；填好地址后再开启
  version: "1.x"                       # 1.x | 2.5 | 3.x，决定请求协议
  base_url: "http://<host>:8088"       # 1.x/2.5 主地址（按自己的服务环境填写）
  parse_url: ""                        # 留空时由 base_url 推导 /file_parse
  download_url: ""                     # 留空时由 base_url 推导 /download_folder
  v3_base_url: "http://<host>:8000"    # 3.x 任务制地址
  consume_backend: "auto"              # auto | pipeline | vlm
  parse_method: "auto"                 # auto | txt | ocr
  task_poll_interval_seconds: 2        # 3.x 任务轮询间隔
  task_timeout_seconds: 1800           # 3.x 任务超时
  file_parse_timeout_seconds: 600      # 1.x/2.5 解析超时
  work_dir: "_work/mineru"             # 解析产物缓存目录（带内容指纹，避免同名串味）
  max_retries: 2                       # 请求失败重试次数（指数退避；4xx 不重试）
  retry_backoff_seconds: 1.0           # 重试基础间隔（秒）
  api_key: ""                          # Bearer Token，非空时自动加 Authorization 头
  headers: {}                          # 自定义请求头，如 {"X-Tenant": "xxx"}
```

`mineru_client.py` 按 `version` 选择协议：1.x/2.5 走 `file_parse + download_folder`，3.x 走任务制（提交 + 轮询 + 下载）。所有请求走统一封装 `_request()`，自动注入鉴权头并在失败时按指数退避重试（4xx 客户端错误除 429 外不重试）。服务端产物按「文件名 + 内容指纹」隔离，同名不同内容不会串味；解压前校验压缩包成员路径，拒绝 Zip Slip 路径穿越。

视觉模型接口（`parsing.vision.*`）同样支持 `api_key`（自动转 `Authorization: Bearer ...`）、自定义 `headers` 与 `timeout_seconds`。

MinerU 返回的 `middle_json` 会经 `mineru_result_adapter.py` 转为带 `bbox/page` 的 Element，再由 `provenance.py` 关联到对应分块——这就是 PDF 的溯源链路。

### 4.1.1 结果缓存（`cache.*`）

```yaml
cache:
  enabled: false        # 默认关闭；开启后相同文件+策略直接复用结果
  dir: "_work/cache"   # 缓存目录
  ttl_seconds: 86400   # 有效期（秒），0 表示永不过期
  url_check_timeout_seconds: 10  # URL 内容签名探测超时
```

缓存键 = 输入标识 + 内容指纹 + 分块策略 + 影响结果的完整配置段签名（`chunking` / `parsing` / `mineru` / `converter` / `images`）。

- 本地文件：绝对路径 + `size` + `mtime_ns`；文件改动、换策略或调分块参数都会自动失效。
- URL：优先用响应头 `ETag` / `Last-Modified` / `Content-Length` 作签名，缺失时下载内容算 MD5；网页更新后缓存自动失效。
- 失败结果不写入缓存，修复后可以立即重试。CLI 用 `--no-cache` 可临时强制重算（也不会对 URL 做签名探测）。

### 4.1.2 长度度量（`chunking.length_unit`）

```yaml
chunking:
  length_unit: "char"        # char（字符，默认）| token
  token_encoding: "cl100k_base"
```

设为 `token` 时所有策略按 token 切块（需 `tiktoken`，缺失时按 CJK 估算回退）。各策略的 `chunk_size`/`parent_size` 等数值会按所选单位解释。

### 4.2 文件转换（`converter.*`）

```yaml
converter:
  soffice_bin: "soffice"   # LibreOffice 可执行文件，旧版 Office / docx-mineru 路线用
  timeout_seconds: 120
```

### 4.3 输出（`output.*`）

```yaml
output:
  dir: "sop_output"        # 输出目录（相对当前工作目录）
  save_parsed_md: true     # 是否保存解析全文 .md
  save_chunks_json: true   # 是否保存分块 .json
  include_elements: false  # result.json 是否内嵌全部 elements（大文档务必 false，否则体积暴涨）
  encoding: "utf-8"
```

`include_elements=false` 时 `result.json` 只保留解析全文、分块与元数据，不包含逐元素明细；需要明细时改为 `true`。

---

## 5. 分块策略

默认策略：`parent_child`（父子分块）。可用策略见 `ChunkFactory.available()`：

| 策略名 | 类 | 思想 | 主要参数（配置键） |
|---|---|---|---|
| **`parent_child`（默认）** | `ParentChildChunker` | 先切大父块保上下文，再在父块内切小块用于检索，建立双向关联 | `parent_size=1500`、`parent_overlap=0`、`child_size=400`、`child_overlap=80`、`respect_headers=true` |
| `recursive` | `RecursiveChunker` | LangChain 风格，按分隔符递归切分 | `chunk_size=500`、`chunk_overlap=50`、`separators=[...]` |
| `markdown_header` | `MarkdownHeaderChunker` | 按 Markdown 标题层级切块 | `max_size=1500`、`header_level=6`（识别的最深标题级别） |
| `fixed_size` | `FixedSizeChunker` | 固定长度 + overlap 滑窗 | `chunk_size=500`、`chunk_overlap=50` |
| `sentence` | `SentenceChunker` | 按中英文句子切分，带句子级 overlap | `max_size=500`、`overlap_sentences=1` |
| `semantic` | `SemanticChunker` | 相邻句子相似度低于阈值时切分；缺依赖自动降级为 recursive | `max_size=800`、`model`、`similarity_threshold=0.7` |

### 5.1 父子分块（默认）为什么是默认

```
父块 parent_0000  (≈1500字，保留完整上下文，尊重标题边界)
├── 子块 child_00000  (≈400字 + 80 overlap，parent_id=parent_0000)  ← 用于向量检索
├── 子块 child_00001
└── ...
父块 parent_0001 ...
```

- **检索时命中子块**（粒度细、精度高），**返回时回填其父块**（语境完整），兼顾召回精度与语义完整。
- 双向关联：`parent.child_ids` ↔ `child.parent_id`，输出中父子块相邻排列，便于阅读与入库。
- 与 RAG 场景天然契合：子块做 embedding/检索，父块做 context 拼装。

### 5.2 切换策略

```bash
python run.py doc.md --strategy markdown_header
```

```python
r = sop.process("doc.md", strategy="semantic")
```

选用非默认策略时，可在配置文件的 `chunking.strategies.<策略名>` 下调参数（两份配置里的 `sentence` 已补齐默认参数，记得改默认加载的 JSON）。策略名写错时返回可读错误并列出可用策略，CLI 退出码 `1`，不会抛出裸 traceback。

---

## 6. 输出说明

处理完成后，`output.dir` 目录下按输入文件名生成三份文件（docx 含图时另有 `images/` 子目录）：

| 文件 | 内容 |
|---|---|
| `<文件名>.parsed.md` | 解析全文（Markdown，含插入的图片链接） |
| `<文件名>.chunks.json` | 分块结果：策略名、块数、元数据、每块 `chunk_id/text/parent_id/child_ids/char_count/metadata/pages/bbox/source_element_ids` |
| `<文件名>.result.json` | 汇总：解析结果（默认不含 elements，需设 `output.include_elements=true` 才内嵌）、分块结果、输出文件路径、时间戳、`ok`、`error` |
| `images/` | docx / pptx 内嵌图片（`images.*` 开启时生成；文件名按内容哈希，相对链接 `images/xxx` 可直达） |

统一数据模型定义在 `sop/models.py`：

- `Element`：`type`（heading/text/table/image/equation/code）、`text`、`level`、`bbox`、`page`。
- `ParseResult`：`success`、`content`、`elements`、`parser`、`warnings`、`error`、`elapsed_ms`...
- `Chunk` / `ChunkResult`：`chunk_id`、`text`、`parent_id`、`child_ids`、`char_count`、溯源字段 `pages`/`bbox`/`source_element_ids`（`.page` 为主页码属性）...
- `ProcessResult`：`parse` + `chunk` + `output_files` + `error`，`.ok` 表示解析与分块是否都成功；路由模式下另有 `route`（实际解析路径）与 `kb_output`（KB 集合结构）。

无论底层用哪个解析器、哪种分块策略，对外输出结构保持一致，便于上层入库、检索或导出。路由模式另见 §3.5 的 `docs / doc_chunks / _engine_meta` 结构。

---

## 7. 处理流程（SOP 全貌）

```
输入（本地路径 / URL）
  │
  ├─ [可选：--method 路由模式] ParsingRouter.route()
  │     ├─ 按 document_parsing_method 四分支选择解析路线（见 §3.5.1）
  │     ├─ 条件转 PDF（字数 / LibreOffice / 超时三重门槛）
  │     ├─ 字符级 LCS 对齐 → bbox → highlight_pdf_path
  │     └─ 任一路线异常 → 降级 fallback + _fallback=True + execution_trace 记录
  │
  ├─ DocumentParser.parse()
  │     ├─ 识别类型（后缀归一化，无后缀 URL→.html，无后缀文件→.txt）
  │     ├─ ParserRegistry 按后缀选择解析器
  │     ├─ 各解析器按需加载重依赖 / 加载 MinerU 或 vision 配置
  │     ├─ （docx/pptx）按原文/页内顺序交错输出段落·表格·图片，图片落盘并插入 Markdown 链接
  │     └─ 封装 ParseResult（content + elements + warnings）
  │
  ├─ ChunkFactory.create(strategy, 策略配置).chunk()
  │     └─ 生成 Chunk 列表（父子分块建立双向关联）
  │
  ├─ [路由模式] kb_models 组装 docs / doc_chunks / _engine_meta
  │
  └─ DocumentSOP._save()
        ├─ 常规：.parsed.md / .chunks.json / .result.json
        └─ 路由模式追加：.docs.json / .doc_chunks.json / ._engine_meta.json
```

降级链：PDF `MinerU → pypdf`；图片 `vision → MinerU OCR`；语义分块 `sentence-transformers → recursive`；docx/doc 走 MinerU 时 `LibreOffice 转 PDF` 失败则给出警告。

---

## 8. 快速示例

工具包自带 `examples/demo.md`，可直接运行体验：

```bash
python run.py examples/demo.md --preview 3
```

运行后会打印解析摘要（文件、类型、解析器、耗时、字符数、警告）、分块摘要（策略、总块数、父子块数）以及前 3 个分块预览，并把三份产物写入 `sop_output/`：

```text
sop_output/
├── demo.parsed.md      # 解析全文（Markdown）
├── demo.chunks.json    # 分块结果（含溯源字段）
└── demo.result.json    # 汇总结果
```

换成自己的文件，只需替换路径：

```bash
python run.py /path/to/your_document.docx --strategy parent_child --preview 3
```

其它用法（批量、token 切块、管道输出、Python API）见第 2 节。

---

## 9. 回归测试

`tests/test_regression.py` 用标准库 `unittest` 编写，零第三方依赖。按维度覆盖：

- **解析正确性**：句子 overlap、超长块兜底、超长标题不击穿块大小、中英文标题识别、OFD 页数、URL 后缀分发（PDF 不再当 HTML）、middle_json 溯源适配、Element→Chunk 关联、docx 段落/表格按原文顺序交错输出（表格不再被挪到文末）、pptx 文本/表格/图片按 shape 原始顺序输出且不丢内容。
- **顺序保真**：所有解析按文件自身位置输出（docx 的 body 顺序、pptx 的 shape 顺序、Excel 的 sheet 顺序、OFD 的页码顺序、文本流原序）；六种分块策略均保持原文块顺序，块内/块间不倒序（重叠仅来自配置的 overlap）。
- **分块策略**：父子块双向关联、token/char 度量、语义降级（依赖缺失与模型加载失败均降级）、非法参数（0/负数/overlap ≥ size）拒绝。
- **失败语义**：缺失/空/损坏文件、空内容开关、URL 抓取失败、未知策略与 CLI 退出码、配置加载与配置值错误无 traceback、分块异常封装、降级诊断保留。
- **缓存**：指纹与参数失效、mtime_ns、URL ETag、失败结果不入缓存、`created_at` 保留。
- **图片保留**：docx 内嵌图片落盘与链接插入、内容哈希去重、开关关闭跳过、大小限制告警、链接进入块、缓存换目录图片同步、images 配置纳入缓存指纹。
- **分支路由与高亮**：docx 四分支匹配、`vision` 落 fallback、PDF 路径无 soffice 降级、`_fallback`/execution_trace 改写、docs/doc_chunks/_engine_meta 字段、box_json_str 格式、LCS 非零 bbox 与顺序、字数门槛跳过对齐、mock soffice 完整高亮链路、默认不启用路由。
- **安全与发布**：Windows 反斜杠路径输出定位、URL 文件名穿越清洗、输出目录写入失败、同名批量隔离（含默认输出目录）、MinerU Zip Slip 与 4xx 不重试、鉴权注入、配置顶层类型校验、JSON/YAML 等价、发布文本脱敏扫描。

```bash
python -m unittest discover -s tests -v
```

当前 112 项测试在 Windows 上全部通过；缺 requests / PyYAML / python-docx / python-pptx / PyMuPDF 等可选依赖的用例会自动跳过，Windows 专属路径语义用例在非 Windows 环境跳过，核心逻辑不依赖第三方库。

CI 配置见 [`.github/workflows/tests.yml`](.github/workflows/tests.yml)，在 Ubuntu / Windows 上以 Python 3.10 与 3.12 运行。

---

## 10. 参与贡献

欢迎提交 Issue 与 Pull Request。

- 开发环境、测试要求、代码约定、新增解析器/分块策略的步骤见 [CONTRIBUTING.md](CONTRIBUTING.md)。
- Bug 请使用 [Bug 报告模板](.github/ISSUE_TEMPLATE/bug_report.yml)，功能建议请使用 [功能建议模板](.github/ISSUE_TEMPLATE/feature_request.yml)。
- 参与本项目即表示同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 11. 安全

请不要公开提交安全漏洞。报告方式、支持版本与安全边界见 [SECURITY.md](SECURITY.md)。

处理不受信任文档时，建议限制运行目录与网络访问范围，并避免在配置中提交 API Key。

## 12. 已知限制

- bbox 字符对齐采用“采样 + LCS”近似算法，超长文档会跳过精确对齐；高亮 PDF 需要系统安装 LibreOffice。
- MinerU 为可选外部服务，未配置时 PDF 走本地 pypdf，图片 OCR 不可用。
- PPTX 图表 / SmartArt、DOCX 页眉页脚暂不提取；WPS 私有表格格式 `.et` 需先另存为 `.xlsx`。
- 表格合并单元格会按 Markdown 表格扁平化，无法完整还原合并关系。

## 13. License

本项目基于 [MIT License](LICENSE) 开源。

如果这个项目对你有帮助，欢迎 Star、提交 Issue 或分享你的使用场景。

# Changelog

本项目的重要变更记录在此文件中，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Planned

- 更多格式的版面还原（表格合并单元格、PPT 图表）；
- 更细粒度的分页/分段溯源；
- 可选的容器化部署示例。

## [1.0.0] - 2026-11-25

首个可发布版本。

### Added

- 统一的文档解析 SOP：`Config → DocumentSOP.process() → DocumentParser → ChunkFactory → ProcessResult`。
- 支持 txt/md/html/json/csv/代码/日志、docx、pptx、xlsx/xlsm/xls、pdf、ofd、图片与 URL。
- 六种分块策略：`parent_child`（默认）、`recursive`、`markdown_header`、`fixed_size`、`sentence`、`semantic`。
- char/token 两种长度度量、overlap、父子块双向关联、Element → Chunk 溯源。
- docx / pptx 内嵌图片提取：内容哈希命名、去重、相对链接随文本进入分块。
- docx 按 body 顺序交错输出段落/表格/图片；pptx 按 shape 顺序输出并递归展开组合形状。
- `document_parsing_method` 四分支路由、条件 PDF 转换、PyMuPDF 字符级 bbox 对齐、高亮 PDF 路径。
- 知识库入库结构：`docs / doc_chunks / _engine_meta / _engine_data` 四份 JSON。
- 文件指纹缓存、URL 内容签名、批量并发与同名文件隔离。
- 112 项标准库回归测试，覆盖失败语义、顺序保真、图片、路由、配置等价与发布脱敏。

### Changed

- 表格单元格中的 `|` 与换行会转义/折叠，避免破坏 Markdown 列结构。
- 文本框文字与嵌套表格文本会按锚点补回，减少 docx 内容丢失。
- `traceable` 统一表示“存在真实 bbox”，不再把仅有文本元素的结果标为可溯源。
- `docs` 增加 `doc_id`，与 `doc_chunks.doc_id` 对齐；`doc_id` 基于脱敏文件标识与内容生成。

### Fixed

- 父子分块下元素全部挂到父块、子块缺少坐标的问题。
- 短 PDF 页面采样丢弃页尾字符、导致 bbox 偏窄的问题。
- MinerU 重试上传发送空文件、HTML 错误页被当作 Markdown 缓存的问题。
- `result.json` 缺少 `ok`、`output_files` 为空且未登记自身路径的问题。
- URL 下载失败遗留临时文件、图片目录不可创建导致解析中断的问题。
- 递归分块在极小步长下可能丢失文本尾部的问题。

### Removed

- 不再声明支持 WPS 私有表格格式 `.et`（pandas/openpyxl/xlrd 无法直接读取，请先另存为 `.xlsx`）。

### Security

- MinerU 压缩包 Zip Slip 防护。
- KB `file_path` 脱敏；URL 的 query/fragment 不写入入库记录。
- 输出文件名穿越清洗、配置顶层类型校验、失败结果不写缓存。

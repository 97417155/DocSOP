# 贡献指南

感谢你愿意改进 DocSOP。这个项目是一个独立的文档解析 SOP 工具包，核心目标是：

> 输入一个文件（本地路径或 URL），输出可检索的解析全文与分块结果。

提交代码前，请先阅读下面的约定，尽量让改动小而聚焦。

## 1. 开发环境

```bash
git clone <你的 GitHub 仓库地址>
cd doc-sop

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

如果只想运行核心链路，可以不安装全部可选依赖；缺少依赖时对应格式会返回可读的失败原因，测试会自动跳过相关用例。

## 2. 运行测试

```bash
python -m unittest discover -s tests -v
```

测试使用标准库 `unittest`，不强制依赖 pytest。请在提交 PR 前确保：

- 全部测试通过；
- 新增行为有对应的回归测试；
- 失败路径也要覆盖（缺依赖、损坏文件、非法配置、空内容等）。

## 3. 代码约定

- 目标 Python 版本：3.9+，建议使用 3.10+ 验证。
- 保持“可选依赖、优雅降级”：重依赖按需导入，不要放进核心链路的顶层导入。
- 解析失败必须返回 `success=false` + 可读 `error`，不要把失败伪装成空内容成功。
- 不要静默吞掉异常；需要降级时写入 `warnings`，并在结果中保留原因。
- 新增或修改输出字段时，同步更新 README 与测试。
- 文本统一使用 UTF-8；路径处理要兼容 Windows 反斜杠。
- 不要在代码、配置、测试或文档中写入本机绝对路径、账号、Token 或内部服务地址。

## 4. 新增解析器

1. 在 `sop/parser/` 下实现 `BaseParser` 子类：
   - 声明 `name` 与 `supported_exts`；
   - 实现 `parse(ctx) -> (content, elements, metadata, warnings)`；
   - 重依赖通过 `self.require(...)` 按需加载。
2. 在 `sop/parser/dispatcher.py` 的 `_build_registry()` 中注册。
3. 为“成功、损坏文件、缺依赖”至少各补一个测试。
4. 如涉及图片，请复用 `ImageStore`，保持内容哈希命名与相对链接规则。
5. 更新 README 的“支持的文件格式与解析器”表格。

## 5. 新增分块策略

1. 在 `sop/chunking/` 下实现 `BaseChunker` 子类；
2. 在 `sop/chunking/factory.py` 的 `REGISTRY` 中注册；
3. 参数校验使用基类的 `_positive_int` / `_bounded_overlap`，非法配置要明确报错；
4. 补测试：块大小上限、overlap、顺序保真、空文本、超长单句；
5. 更新 README 的分块策略章节。

## 6. 提交与 Pull Request

- 一个 PR 聚焦一件事，避免顺手重构无关文件。
- 提交信息建议使用 Conventional Commits：
  - `feat: 新增 xxx 解析器`
  - `fix: 修复 docx 表格位置错乱`
  - `docs: 补充配置说明`
  - `test: 增加路由降级用例`
- PR 描述请写清楚：改了什么、为什么改、如何验证、是否影响兼容性。
- 如果改动涉及输出结构或配置项，请注明迁移方式。

## 7. 报告问题

- Bug 请使用仓库的 Bug Report 模板，附上最小复现文件或脱敏片段。
- 安全问题请不要公开提 Issue，按 [SECURITY.md](SECURITY.md) 的流程私下报告。
- 功能建议请说明使用场景，而不只是“希望支持某格式”。

## 8. 许可证

提交贡献即表示你同意以本仓库的 [MIT License](LICENSE) 授权你的贡献。

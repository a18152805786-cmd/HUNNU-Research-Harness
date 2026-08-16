# HUNNU Research Harness v0.2.12

湖南师范大学数字资源研究自动化 Harness。它把“人工完成学校认证”和“登录后的研究操作”明确分开：Harness 可以识别页面、进入 CNRDS CNFS、选择研究条件、触发合法下载并归档原始文件；它不会输入密码、验证码或 MFA，也不会导出 cookie。

## 当前能力

- 认证状态：`AUTH_UNKNOWN`、`AUTH_REQUIRED`、`AUTH_IN_PROGRESS`、`AUTH_SUCCESS`、`SESSION_EXPIRED`。
- CNRDS Adapter：CNFS 三张表和核心动作接口已建立，操作前校验数据库/模块/表状态。
- 下载管理：监测 `.crdownload`/`.part` 等临时文件，检查文件大小稳定，计算 SHA-256，复制原始文件到研究归档并生成 JSON manifest。
- 专用浏览器：Playwright 持久化 profile 启动入口，不默认使用日常 Chrome profile。
- Literature Acquisition：已实现 CNKI、SpringerLink、ScienceDirect、OxfordAcademic Adapter，以及湖南师范大学机构访问路由 fallback。v0.2.7 仅对专用 Research Chrome profile 定向启用 `plugins.always_open_pdf_externally`，因此 Harness 点击 Oxford 官方 PDF action 后可直接产生标准 Playwright download event，无需用户操作 Chrome PDF Viewer。下载继续进入既有 validator、Target Identity Lock、SHA-256、manifest 与 archive；`ManualDownloadHandoff` 仍保留为人工 fallback，但不计为无人值守成功。全程不 replay URL，也不自动操作 Viewer GUI。
- Agent Integration：`agent-route` 为 Agent 提供稳定的结构化请求路由与无网络 dry-run；详见 [docs/AGENT_INTEGRATION.md](docs/AGENT_INTEGRATION.md)。
- Multi-source preflight：显式无人值守的多来源请求先动态扫查全部计划来源、批量汇总人工认证 Gate，并要求每来源完成一次本会话授权下载、文件验证与 Target Identity Lock 后才签发 `UnattendedRunClearance=true`。
- Global Paper Library：在既有 run archive、Target Identity Lock、文件验证和 SHA-256 之后，把全文以现有 PaperID 纳入 `Output Root/library/`。JSONL 是 catalog source of truth，CSV 是人工查看投影；同一作品的不同 SHA-256 作为不同文件版本保留，绝不覆盖主版本。

Global Library 与受控 external import 的正式契约见 [docs/GLOBAL_PAPER_LIBRARY.md](docs/GLOBAL_PAPER_LIBRARY.md)。外部工具只能显式 COPY 单个候选到 staging，再提交 claimed metadata；Harness 会先用本地 PDF 首页面内容独立核验 DOI/title，无法验证或存在冲突时进入 review。Harness 不提供全盘扫描、MOVE、DELETE、联网身份补全或静默覆盖入口。

## v0.1 CNRDS 端到端验收

`HarnessV01CNRDSTestPassed=true`

已在用户人工完成湖南师范大学统一身份认证后，使用专用 Research Chrome 和 Playwright MCP 完成一次最小真实链路：CNRDS → CNFS → 现金流量表 → 000001 → 2024 → CSV。下载由 Download Manager 检测完成，原始压缩包保留、计算 SHA-256，并生成 manifest。

### 实际使用流程

1. 启动 HUNNU Research Chrome。
2. 用户人工登录学校账号。
3. 用户告诉 Codex：“已登录，继续”。
4. Harness 检测认证状态、CNRDS 页面和当前数据表。
5. Agent 设置数据库任务并执行最小样本预览与下载。
6. Download Manager 检测下载完成及临时文件状态。
7. 原始文件归档并保留原文件。
8. 对原始下载文件计算 SHA-256。
9. 生成不含密码、Cookie、session token、验证码或 MFA 信息的 manifest。

## 环境准备

```powershell
cd C:\Users\71966\Desktop\HUNNU-Research-Harness
.venv\Scripts\python.exe -m pip install -e .
# 需要本地 Playwright 后端时：
.venv\Scripts\python.exe -m pip install -e ".[browser]"
```

### 路径边界

代码工作区：

`C:\Users\71966\Desktop\HUNNU-Research-Harness`

正式论文研究数据与既有非-Harness归档仍保存在：

`D:\BaiduNetdiskDownload\论文数据\`

Harness 运行产物默认保存在项目内部：

- Harness 主体：`C:\Users\71966\Desktop\HUNNU-Research-Harness\`
- Harness 统一输出根：`C:\Users\71966\Desktop\HUNNU-Research-Harness-Output\`
- 授权下载归档：输出根下的 `downloads\authorized\`
- Staging / Playwright 输出：输出根下的 `staging\` 与 `staging\playwright-output\`
- Run / Manifest / 日志：输出根下的 `runs\`、`manifests\` 与 `logs\`
- 截图、审计、审查包和临时文件：输出根下的 `screenshots\`、`audit\`、`review\` 与 `temp\`
- 长期个人论文库：输出根下的 `library\papers\`、`library\notes\`、`library\catalog\` 与 `library\import_staging\`

只有明确批准的正式论文研究数据才导出到 `D:\BaiduNetdiskDownload\论文数据\`；测试、smoke test 和 Harness 运行产物不得写入论文数据目录。

Codex MCP（安装/配置一次即可）：

```powershell
codex mcp add playwright npx "@playwright/mcp@latest"
codex mcp list
```

## 启动专用 Chrome

```powershell
.venv\Scripts\python.exe -m hunnu_harness.cli browser-start
```

默认 profile 位于：

`C:\Users\71966\ResearchHarness\chrome-profile`

首次打开后，请由用户本人完成湖南师范大学图书馆/学校统一认证。Harness 只等待并读取可见页面状态。遇到密码、验证码、二维码、MFA 或 WebVPN，必须人工完成后再继续。

## 对 Codex 的使用示例

人工登录成功后，对 Codex 说：

```text
请使用 HUNNU Research Harness。
数据库：CNRDS
模块：CNFS
表：利润表
公司：000001
日期：2024-01-01 至 2024-12-31
字段：营业收入、净利润
只下载，不做数据清洗。
```

每次下载前必须确认当前 URL、页面标题、数据库、模块和表。下载完成后，Harness 会保留原始文件、计算 SHA-256，并在独立 Output Root 的 `downloads\authorized\` 和 `manifests\` 生成归档与 manifest。Output Root 只保存运行产物，不是第二套 Harness 工程。

## 安全边界

- 不输入或保存学校密码、个人密码、验证码、OTP、MFA。
- 不绕过付费墙、学校认证、验证码、下载限制。
- 不修改原始研究数据，不覆盖 raw 文件。
- 不自动运行回归、构造研究变量或修改论文。
- 不把学校授权数据提交到 Git。

## 当前尚未实现

- 已打开的日常 Chrome 标签页自动接管尚未作为默认路径启用；v0.1 优先使用专用 profile。Chrome Extension/CDP 接管需要单独验证和人工完成扩展操作。
- CNRDS 动态页面的所有真实字段选择器尚未在本地公开网页上完成端到端验证。
- 万方、RESSET、EPS 等尚未实现的来源不能静默退回临时浏览器流程；需先报告缺失能力并获得用户授权后才可扩展。
- FDM 接管下载的兼容性尚未启用；建议专用 profile 使用浏览器原生下载，以便 Harness 可靠识别下载链路。

`browser-start` 当前是一次安全的 profile 启动/状态检查命令，会在输出状态后关闭浏览器；长期运行和 Codex 操作优先由已登记的 Playwright MCP 进程负责。Python API 仍提供 `research_browser.start()`、`status()`、`stop()`，供后续服务化封装使用。

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 版本控制

代码、配置模板和文档可以进入 Git；学校下载数据、PDF、表格、日志私密内容和浏览器状态已加入 `.gitignore`。

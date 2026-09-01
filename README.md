# HUNNU Research Harness v0.3.3

## 给安装 Agent 的指引（拿到 zip 后先读这一节）

这一节写给替用户完成安装的 agent：按顺序执行，每一步都先跑命令、再跑验证、失败走对应分支。全程不需要用户账号密码；遇到密码、验证码、MFA 一律停下来交给用户本人。装完之后，操作契约在 [AGENTS.md](AGENTS.md)——先读完再动手干活。

**第 0 步 · 前提。** 解压 zip 得到 `HUNNU-Research-Harness\` 文件夹，下面所有命令都在这个文件夹里执行（PowerShell）。zip 里没有也不可能有 `.venv`（虚拟环境的路径写死在内部文件里，打包了也是坏的），必须现场重建。

**第 1 步 · Python。**

```powershell
python --version
```

验证：版本 ≥ 3.11（开发与测试用 3.12.13）。失败分支：让用户从 python.org 安装 3.11+ 后重来；不要用系统里来路不明的旧 Python 硬装。

**第 2 步 · 建虚拟环境。**

```powershell
python -m venv .venv
```

验证：`.venv\Scripts\python.exe` 存在。失败分支：把报错原样报告给用户，停止。

**第 3 步 · 安装（依赖已钉死精确版本）。**

```powershell
.venv\Scripts\python.exe -m pip install -e ".[browser,dev]"
```

验证：`.venv\Scripts\python.exe -m pip show pypdf playwright pytest` 显示 pypdf 6.16.1、playwright 1.62.0、pytest 8.4.2。失败分支：网络超时或下载慢时改用镜像重试：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[browser,dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**第 4 步 · 输出根（Output Root）。** 所有运行产物写在仓库外的独立目录，默认是同级的 `<文件夹名>-Output\`，首次使用自动创建；也可用环境变量 `HUNNU_HARNESS_OUTPUT_ROOT` 指到别处。验证：

```powershell
.venv\Scripts\python.exe -c "from hunnu_harness.paths import OUTPUT_ROOT; print(OUTPUT_ROOT)"
```

失败分支：如果报 `must be outside Core Root`，说明有人把输出根指进了代码文件夹——换个位置；如果报路径过长，把输出根设到更浅的目录（例如 `C:\HUNNU-Output`）。

**第 5 步 · 浏览器。** Harness 自动发现系统 Chrome（也认环境变量 `HUNNU_RESEARCH_CHROME`），找到就不需要下载任何浏览器。验证跳过。只有这台机器确实没装 Chrome 时才走回落分支：安装 Google Chrome，或执行 `.venv\Scripts\python.exe -m playwright install chromium`（约 150MB 下载）。

**第 6 步 · 自检（测试套件）。**

```powershell
.venv\Scripts\python.exe -m pytest -q
```

验证：没有 FAILED；出现一批 skipped 是正常的——语料相关测试在空文献库上主动降级，不是坏。失败分支：停下来，把失败测试名和输出原样报告给用户；**不要试图删测试或改护栏让它变绿**（那些测试的名字会告诉你它在守什么）。

**第 7 步 · 冒烟（无网络 dry-run）。**

```powershell
.venv\Scripts\hunnu-harness.exe agent-route --text "找 数字化转型 的文献" --dry-run
```

验证：输出 JSON 且 `"DryRun": true`，退出码 0，全程无网络访问。退出码 2 表示请求被判为不可路由（看输出里的原因说明），不是安装问题。

**装完须知。** 真实采集（`live-*`）会用用户自己的学校账号配额：登录由用户本人在专用 Chrome profile 里完成（正常约每天一次），每日取件总量默认 15（`--daily-limit` 或 `HUNNU_HARNESS_DAILY_FETCH_LIMIT` 可调），同一篇一天最多 2 次的循环保护不可调。这些设计的理由都写在 AGENTS.md 里。

---

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
- OfficialWeb：通过正式 `OfficialWebExecutionBroker -> PublicOfficialWebAdapter -> BrowserCommandPort` 路径获取公开官方网页证据。请求必须提供 `AllowedDomains`；跳转出 allowlist、登录、CAPTCHA、安全挑战、付费墙或非 HTML 内容均 fail closed。它不下载论文，也不替代 CNKI/出版社 adapter。
- Bounded multi-batch research：单批下载硬上限仍为 25；显式 `TotalDownloadBudget` 可由 planner 拆成多个受控批次，并保留总候选/下载预算、有限重试和可选 quota group 扩展点。规划不会自动执行下载。

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
cd <Harness 根目录>
.venv\Scripts\python.exe -m pip install -e .
# 需要本地 Playwright 后端时：
.venv\Scripts\python.exe -m pip install -e ".[browser]"
```

### 路径边界

代码工作区：

`<Harness 根目录>`

正式论文研究数据与既有非-Harness归档仍保存在：

`你自己指定的正式研究资料目录`

Harness 运行产物默认保存在项目内部：

- Harness 主体：`<Harness 根目录>\`
- Harness 统一输出根：Output Root（默认为兄弟目录 `<仓库名>-Output`，或 `HUNNU_HARNESS_OUTPUT_ROOT` 指定）
- 授权下载归档：输出根下的 `downloads\authorized\`
- Staging / Playwright 输出：输出根下的 `staging\` 与 `staging\playwright-output\`
- Run / Manifest / 日志：输出根下的 `runs\`、`manifests\` 与 `logs\`
- 截图、审计、审查包和临时文件：输出根下的 `screenshots\`、`audit\`、`review\` 与 `temp\`
- 长期个人论文库：输出根下的 `library\papers\`、`library\notes\`、`library\catalog\` 与 `library\import_staging\`

只有明确批准的正式研究数据才导出到你自己指定的正式研究资料目录；测试、smoke test 和 Harness 运行产物不得写入正式研究资料目录。

Codex MCP（安装/配置一次即可）：

```powershell
codex mcp add playwright npx "@playwright/mcp@0.0.79"
codex mcp list
```

## 启动专用 Chrome

```powershell
.venv\Scripts\python.exe -m hunnu_harness.cli browser-start
```

默认 profile 位于：

`%USERPROFILE%\ResearchHarness\chrome-profile`

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

## OfficialWeb 公开证据

`OfficialWeb` 只处理公开、无需登录的期刊、主办方、出版社、投稿系统公开页、征稿/投稿须知、栏目、公告与正式访谈页面。它不是通用爬虫，也不使用 `requests`、临时 Playwright 脚本或 `curl` 绕过 Harness。

结构化请求必须给出显式证据 URL 和任务级域名 allowlist。`OfficialDomainClaims` 用配置的官方域名及主办/出版关系把结果分为 `OFFICIAL_CONFIRMED`、`OFFICIAL_PROBABLE` 或 `UNVERIFIED`；标题本身从不构成官方性证明。候选 URL discovery 与 evidence fetch 是两个概念阶段，只有重新通过 allowlist、最终跳转域名和 officiality 检查的页面才能进入证据。

```json
{
  "TaskType": "official_web",
  "Query": "查找期刊投稿须知",
  "URLs": ["https://journal.example.edu/submission"],
  "AllowedDomains": ["journal.example.edu", "sponsor.example.edu"],
  "OfficialDomainClaims": [
    {
      "Domain": "journal.example.edu",
      "SourceType": "JournalOfficialWebsite",
      "Relationship": "configured journal-owned domain"
    }
  ]
}
```

## Bounded Multi-Batch

`MaxDownloads`/`MaxDownloadsPerRun` 继续限制单批最多 25。较大的正式目标必须显式声明总预算，由 `BoundedBatchPlanner` 拆批；`BoundedBatchCoordinator` 拒绝批次或累计结果超预算，并将每批重试限制在 `MaxRetries + 1` 次。只有带明确已提交下载数的 `RetryableBatchError` 才会自动重试；未知异常立即停止该批，避免无法核算的重复下载。

```json
{
  "TaskType": "literature_search",
  "Query": "bounded candidate pool",
  "PreferredSources": ["CNKI"],
  "MaxCandidates": 80,
  "MaxDownloads": 25,
  "TotalCandidateBudget": 80,
  "TotalDownloadBudget": 36,
  "PerBatchDownloadBudget": 25,
  "MaxRetries": 1
}
```

该请求只生成两个下载预算为 `18 + 18` 的批次计划，并进入既有 planning/budget gate；不会自动下载 36 篇。

## 当前尚未实现

- 已打开的日常 Chrome 标签页自动接管尚未作为默认路径启用；v0.1 优先使用专用 profile。Chrome Extension/CDP 接管需要单独验证和人工完成扩展操作。
- CNRDS 动态页面的所有真实字段选择器尚未在本地公开网页上完成端到端验证。
- 万方、RESSET、EPS 等尚未实现的来源不能静默退回临时浏览器流程；需先报告缺失能力并获得用户授权后才可扩展。
- Agent 文献 live execution 的 Python `BrowserTransport` → Codex Playwright MCP bridge 尚未实现；必须先经 `AdapterExecutionBroker`，不能用裸 MCP 浏览器操作替代 adapter。
- FDM 接管下载的兼容性尚未启用；建议专用 profile 使用浏览器原生下载，以便 Harness 可靠识别下载链路。

`browser-start` 当前是一次安全的 profile 启动/状态检查命令，会在输出状态后关闭浏览器；长期运行和 Codex 操作优先由已登记的 Playwright MCP 进程负责。Python API 仍提供 `research_browser.start()`、`status()`、`stop()`，供后续服务化封装使用。

## 测试

```powershell
python -m unittest discover -s tests -v
```

## 版本控制

代码、配置模板和文档可以进入 Git；学校下载数据、PDF、表格、日志私密内容和浏览器状态已加入 `.gitignore`。

# 运行细节

README 讲它是什么、为什么这样设计；这份文档讲具体怎么跑、边界在哪。给 agent 的操作契约以 [AGENTS.md](../AGENTS.md) 为准，本文与它冲突时以 AGENTS.md 为准。

## 路径边界

- **代码工作区**：`<Harness 根目录>`，只放代码、配置模板和文档。
- **Output Root**：所有运行产物写在仓库**外**的独立目录，默认是同级的 `<仓库名>-Output`，也可用 `HUNNU_HARNESS_OUTPUT_ROOT` 指到别处；它不能放进代码目录，Harness 会拒绝。Output Root 只保存运行产物，不是第二套 Harness 工程。
  - 授权下载归档：`downloads\authorized\`
  - Staging / Playwright 输出：`staging\` 与 `staging\playwright-output\`
  - Run / Manifest / 日志：`runs\`、`manifests\` 与 `logs\`
  - 截图、审计、审查包和临时文件：`screenshots\`、`audit\`、`review\` 与 `temp\`
  - 长期个人论文库：`library\papers\`、`library\notes\`、`library\catalog\` 与 `library\import_staging\`
- **正式研究资料目录**：由你自己指定，只有明确批准的正式研究数据才导出到那里；测试、smoke test 和 Harness 运行产物不得写入。

## 专用 Research Chrome

```powershell
.venv\Scripts\hunnu-harness.exe browser-start
```

默认 profile 位于 `%USERPROFILE%\ResearchHarness\chrome-profile`，不使用日常 Chrome profile。首次打开后，由用户本人完成湖南师范大学图书馆 / 学校统一认证。Harness 只等待并读取可见页面状态；遇到密码、验证码、二维码、MFA 或 WebVPN，必须人工完成后再继续。认证状态分为 `AUTH_UNKNOWN`、`AUTH_REQUIRED`、`AUTH_IN_PROGRESS`、`AUTH_SUCCESS`、`SESSION_EXPIRED`。

`browser-start` 启动专用 profile 并让它一直运行到 `browser-stop`，机构登录状态因此能跨多次运行保留；已在运行时它原样返回而不重启。后续 `acquire` 会附着到它，而不是再启动一个，所以 ScienceDirect 采集应先 `browser-start` 再 `acquire`（AGENTS.md 规则 73）。报告里的 `BrowserLaunched` 对附着和自启同为 true，不能用它判断走了哪一条。附着本身并不能避免出版商的拒绝：2026-09-19 的实测表明，短时间内的大量检索才是诱因，检索因此按 `Output Root\audit\search_pace_ledger.jsonl` 跨进程限速（规则 74）。

- `browser-stop` 通过 CDP 的 `Browser.close` 让 Chrome 正常退出（Chrome 会在退出时写回会话与 Preferences），只有在确认调试端口已关闭、且没有任何 Chrome 进程仍占用该 profile 之后才报告 `PersistentBrowserStopped=true`；限时内未退出则如实报告 `PersistentBrowserRunning=true` 并以退出码 2 结束，绝不把"已断开连接"当作"已停止"。
- `browser-status` 只报告是否在监听以及 profile 是否被占用（`ProfileInUse`），不会启动浏览器；它与 `browser-stop` 依据同一组事实作答。
- `browser-start` 以 Chrome 自带的 `--restore-last-session` 启动，因此正常退出后再次启动时，出版商在机构登录后签发的会话 Cookie 会被恢复而不是被清除；没有持久浏览器时由运行自行启动的 Playwright 浏览器也带同一开关。
- `browser-configure-session-restore` 不修改 Preferences（`session.restore_on_startup` 在 Windows 上是 Chrome 受保护的 tracked preference，Harness 无法也不应伪造），只报告实际生效的机制。
- `browser-configure-pdf-download` 仅对停止状态的专用 profile 定向启用 `plugins.always_open_pdf_externally`，这样点击 Oxford 官方 PDF 按钮会直接产生标准下载事件，无需操作 Chrome PDF Viewer。全程不 replay URL，也不自动操作 Viewer GUI；`ManualDownloadHandoff` 保留为人工兜底，但不计为无人值守成功。

Python API 仍提供 `research_browser.start()`、`status()`、`stop()`，供后续服务化封装使用。

Codex 的 Playwright MCP（安装 / 配置一次即可，版本钉在 0.0.79）：

```powershell
codex mcp add playwright npx "@playwright/mcp@0.0.79"
codex mcp list
```

## CNRDS

v0.1 的端到端验收（`HarnessV01CNRDSTestPassed=true`）：在用户人工完成统一身份认证后，使用专用 Research Chrome 完成一次最小真实链路 CNRDS → CNFS → 现金流量表 → 000001 → 2024 → CSV。下载由 Download Manager 检测完成（监测 `.crdownload` / `.part` 等临时文件、等待文件大小稳定），原始压缩包保留、计算 SHA-256，并生成 manifest。

实际使用流程：

1. 启动 Research Chrome，用户人工登录学校账号。
2. 用户告诉 agent："已登录，继续"。
3. Harness 检测认证状态、CNRDS 页面和当前数据表（操作前校验数据库 / 模块 / 表状态）。
4. Agent 设置数据库任务，执行最小样本预览与下载。
5. Download Manager 检测下载完成及临时文件状态；原始文件归档并保留，计算 SHA-256。
6. 生成不含密码、Cookie、session token、验证码或 MFA 信息的 manifest。

对 agent 的请求示例：

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

每次下载前必须确认当前 URL、页面标题、数据库、模块和表。

## 文献采集

`acquire` 对一个来源运行一次有界的真实采集，使用用户本人已登录的 Research Chrome，消耗的是用户自己的机构额度：

```powershell
.venv\Scripts\hunnu-harness.exe library-fetch-budget --json          # 先看今天还剩多少额度
.venv\Scripts\hunnu-harness.exe acquire --source sciencedirect --doi <DOI> --max-downloads 1 --json
```

`--source` 可选 `sciencedirect`、`springerlink`、`cnki`、`oxfordacademic`；目标用 `--title`、`--doi` 或 `--request-json` 指定。退出码 2 表示需要人（登录、人工下载或人工判断），这时停下交还用户（AGENTS.md 规则 72），不要转去同一机构会话的另一个来源。

**多来源预检。** 显式无人值守的多来源请求先动态扫查全部计划来源、批量汇总人工认证闸门，并要求每个来源完成一次本会话授权下载、文件验证与目标身份锁定后，才签发 `UnattendedRunClearance=true`。

**Agent 接入。** `agent-route` 为 agent 提供稳定的结构化请求路由与无网络 dry-run，详见 [AGENT_INTEGRATION.md](AGENT_INTEGRATION.md)。live 执行必须经 `AdapterExecutionBroker`，不能用裸 MCP 浏览器操作替代适配器。

## Global Paper Library 与外部导入

在 run 归档、目标身份锁定、文件验证和 SHA-256 之后，全文以 PaperID 纳入 `Output Root\library\`。JSONL 是 catalog 的唯一事实来源，CSV 是人工查看投影；同一作品的不同 SHA-256 作为不同文件版本保留，绝不覆盖主版本。入库后自动做主题分类，拿不准的进入人工确认（`library-confirm-topics`），并记录是自动还是人工决定的。

外部工具只能用 `library-stage` 显式 COPY 单个候选到 staging，再用 `library-import` 提交 claimed metadata；Harness 会先用本地 PDF 首页内容独立核验 DOI / 标题，无法验证或存在冲突时进入 review。Harness 不提供全盘扫描、MOVE、DELETE、联网身份补全或静默覆盖入口。正式契约见 [GLOBAL_PAPER_LIBRARY.md](GLOBAL_PAPER_LIBRARY.md)，分类细节见 [POST_ACQUISITION_CLASSIFICATION.md](POST_ACQUISITION_CLASSIFICATION.md)。

## OfficialWeb 公开证据

`OfficialWeb` 只处理公开、无需登录的期刊、主办方、出版社、投稿系统公开页、征稿 / 投稿须知、栏目、公告与正式访谈页面。它不是通用爬虫，也不使用 `requests`、临时 Playwright 脚本或 `curl` 绕过 Harness；它不下载论文，也不替代 CNKI / 出版社适配器。路径是 `OfficialWebExecutionBroker -> PublicOfficialWebAdapter -> BrowserCommandPort`。

结构化请求必须给出显式证据 URL 和任务级域名 allowlist；跳出 allowlist、登录、CAPTCHA、安全挑战、付费墙或非 HTML 内容均 fail closed。`OfficialDomainClaims` 用配置的官方域名及主办 / 出版关系把结果分为 `OFFICIAL_CONFIRMED`、`OFFICIAL_PROBABLE` 或 `UNVERIFIED`；标题本身从不构成官方性证明。候选 URL 发现与证据抓取是两个阶段，只有重新通过 allowlist、最终跳转域名和官方性检查的页面才能进入证据。

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

## 分批预算（Bounded Multi-Batch）

`MaxDownloads` / `MaxDownloadsPerRun` 限制单批最多 25。较大的目标必须显式声明总预算，由 `BoundedBatchPlanner` 拆批；`BoundedBatchCoordinator` 拒绝批次或累计结果超预算，并将每批重试限制在 `MaxRetries + 1` 次。只有带明确已提交下载数的 `RetryableBatchError` 才会自动重试；未知异常立即停止该批，避免无法核算的重复下载。

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

该请求只生成两个下载预算为 `18 + 18` 的批次计划，并进入既有 planning / budget gate；不会自动下载 36 篇。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```

必须经由 pytest 运行（不要用 `python -m unittest discover`）：`tests/conftest.py` 的 autouse fixture 把真实的下载台账和检索限速台账重定向到护栏目录，并在每个测试前后快照比对；unittest 不会加载它，直接跑就可能污染真实台账。没有真实文献库的机器上，约 100 项语料相关测试会主动跳过，这是正常的。

## 尚未实现

- 已打开的日常 Chrome 标签页自动接管尚未作为默认路径启用；优先使用专用 profile。Chrome Extension / CDP 接管需要单独验证和人工完成扩展操作（见 [CHROME_EXTENSION_MODE.md](CHROME_EXTENSION_MODE.md)）。
- CNRDS 目前覆盖 CNFS 三张财务报表，动态页面的全部字段选择器尚未逐一做端到端验证。
- 万方、RESSET、EPS 等尚未实现的来源不能静默退回临时浏览器流程；需先报告缺失能力并获得用户授权后才可扩展。
- agent 文献 live execution 的 Python `BrowserTransport` → Codex Playwright MCP bridge 尚未实现。
- FDM 接管下载的兼容性尚未启用；建议专用 profile 使用浏览器原生下载，以便 Harness 可靠识别下载链路。

## 版本控制

代码、配置模板和文档可以进入 Git；学校下载数据、PDF、表格、日志私密内容和浏览器状态已加入 `.gitignore`，不得提交。

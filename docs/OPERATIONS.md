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

**受限检索（只找学术期刊、只找指定刊物、只找指定年份；目前仅 CNKI，规则 39）。** 在请求 JSON 里加可选字段：`ResourceType`（只接受 `JournalArticle`，其他值直接判为无效请求）、`SourceJournals`（最多 8 个刊名，给了刊名即视为 `JournalArticle`）、`YearStart`/`YearEnd`（也可写 `YearFrom`/`YearTo`，两种写法数值不一致则拒绝）。受限请求里的年份对每条结果强制生效，不再只是筛选时扣分：

```powershell
.venv\Scripts\hunnu-harness.exe acquire --source cnki --request-json restricted.json --json
```

```json
{
  "OriginalResearchRequest": "2019-2026年《示例学刊》《样本评论》中的示例议题研究（写法样本）",
  "SourceJournals": ["示例学刊", "样本评论"],
  "YearStart": 2019,
  "YearEnd": 2026,
  "KeywordsCN": ["示例议题", "示例概念"],
  "MaxSearchResults": 20,
  "MaxResultsPerSource": 20,
  "MaxDownloads": 0,
  "MaxDownloadsPerRun": 0
}
```

CNKI 的一框式检索一次只检一个字段，所以指定刊名时按「文献来源」逐刊检索（每刊一次，照常经检索限速台账排队），主题词交给筛选打分，不塞进检索式；只给 `ResourceType` 时按「主题」检索。每次检索都带 `crossids=YSTT4HG0`（只检学术期刊），并且只读第一页结果。结果返回前，页面自己的 `briefRequest` 必须写明 CNKI 确实只检了学术期刊、执行的正是这条检索式，每条结果的「数据库」栏也必须是「期刊」；做不到就以 `CNKI_RESTRICTION_UNCONFIRMED` / `CNKI_RESTRICTION_NOT_APPLIED` 结束这一条，不把该页任何结果当作受限结果返回，本次运行也不再发出后续检索。其他刊物、年份范围外的条目会被丢弃并计数。`acquire` 输出里的 `SearchRestrictionOutcome`、run 目录下 `SEARCH_QUERY_LOG.csv` 的 `RestrictionOutcome` 与 `audit\LITERATURE_EVENTS.jsonl` 逐条记录确认依据、保留与丢弃数量和本页覆盖的年份。CNKI 默认按发表时间倒序，第一页够不到的年份会返回 0 条并在备注里说明——这不能当作「该刊没有这类文章」的证据。受限请求不能与 `ExactTitles`、`DOIs`、`Authors` 同用；其他来源遇到受限请求在启动浏览器前就拒绝（`UNSUPPORTED_CAPABILITY`，退出码 1）。

**只读列表（`ListingOnly`）。** 只想先拉一份候选清单时，在受限请求里加 `"ListingOnly": true`，并把 `MaxDownloads` 设为 0（不满足就判为无效请求）。每条已确认的结果行按页面所写原样保留——题名、刊名、年份、作者——不再逐条打开文章：不做精确题名重锁检索，不开详情页，不查全文权限。一次 15 条的期刊检索由约 30 次页面访问降到 1 次。这些记录没有经过身份锁，永远不会成为下载候选；选中的文章之后按精确题名下载（`acquire --title` 或 `acquire-batch` 的条目），那一步照常打开、锁定、核对。`acquire` 输出的 `SearchRestriction` 里会写 `"ListingOnly": true`，检索日志的 `ResultsInspected` 为 0。

**批量队列（`acquire-batch`，规则 75）。** 多篇论文写进一个队列文件，由一个进程一篇接一篇地跑完，每篇走的都是上面 `acquire` 的同一条路径（限速、写前记账、身份锁、校验、SHA-256、manifest、归档、分类都不变）：

```powershell
.venv\Scripts\hunnu-harness.exe browser-start                                   # 批量只附着已运行的 Research Chrome，绝不自启
.venv\Scripts\hunnu-harness.exe acquire-batch --queue queue.json --dry-run      # 先规划：校验队列、跳过库里已有的、看额度
.venv\Scripts\hunnu-harness.exe acquire-batch --queue queue.json                # 真跑；停下后原样再跑一次即续跑
```

队列文件格式：`{"BatchName": "Example_Batch_01", "Items": [{"Source": "cnki", "Title": "..."}, {"Source": "springerlink", "DOI": "10.1007/..."}]}`，每项只给 `Title` 或 `DOI` 之一，可选 `ItemID`、`Note`。一个队列最多 25 篇（规则 62）；待下载超过 10 篇须在用户确认后加 `--confirm-budget`（规则 41）。遇到第一个人工闸门、出版商拒绝、当日总额用尽或环境失败，整队停下（退出码与单篇 `acquire` 相同）；连续 3 篇到了出版商却没拿到文件也停（退出码 1）。状态记在 `Output Root\runs\AcquireBatch\<BatchName>\BATCH_STATE.jsonl`，每篇的运行证据在同目录 `items\` 下；重跑时已有最终结果的条目（包括下载失败的，规则 71）一律跳过，被闸门挡住的那篇最先跑。批量从不传 `--allow-refetch`。

**同一时间只允许一个采集进程。** 任何驱动 Research Chrome 的运行（`acquire`、批量队列、脚本里交给路由器的 `PlaywrightBrowser`）从浏览器启动到关闭都持有 `Output Root\audit\research_chrome.lock`，第二个进程会被拒绝（退出码 5）。原因：并行运行会共用同一个标签页、整个浏览器共用的下载目录，以及全局的限速台账。实测表明慢在 agent 每篇之间的来回（2026-09-23：63 分钟里 harness 只跑了约 9 分钟），不在单篇本身；已接近限速上限的脚本批量（2026-09-22：每篇中位数 104 秒）再加并发也快不了。并行的 agent 应该用在下载之后：读本地全文、抽证据、筛摘要。

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

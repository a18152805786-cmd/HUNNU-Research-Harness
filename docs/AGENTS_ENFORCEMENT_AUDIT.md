# AGENTS.md 75 条执行力审计（代码强制 vs 散文）

分发方案任务 3.5 的交付物。它回答一个问题：**把这个仓库交给一个陌生 agent，哪些规则是机器挡着的，哪些只靠它自觉？**

审计基准：`dist/zip-release` 分支（阶段 1–3 改造后）。四个等级：

| 等级 | 含义 |
|---|---|
| **代码强制** | 运行时 fail-closed：违反它会抛错/拒绝，绕过必须改源码 |
| **测试钉住** | 行为由测试固定：违反它全量测试变红（测试名会说明拆掉了什么） |
| **设计缺失** | 靠"没有这条代码路径"实现：做不到，除非新写代码 |
| **仅散文** | 只有文字约束：完全依赖 agent 自觉遵守 |

多数代码强制项同时有测试钉住；表中写主要机制。"依据"给出锚点，不是穷举。

## 逐条对照

| 条 | 摘要 | 等级 | 依据 |
|---|---|---|---|
| 1 | 不输入密码/OTP/MFA | 设计缺失＋测试 | 全仓无任何输密码代码路径；登录墙一律 `ACTION_REQUIRED_USER_LOGIN` 停机（workflow 测试钉住） |
| 2 | CAPTCHA/MFA/CAS 处停下 | 代码强制 | `playwright_backend.settle_page_gates`（只等待、绝不点）；CNKI 挑战检测（`cnki_challenge.py`＋多组回归测试） |
| 3 | 下载前校验 URL/库/模块/表 | 代码强制 | CNRDS 适配器状态校验；文献侧 Target Identity Lock（workflow＋适配器测试） |
| 4 | 原始下载不可覆盖/变异 | 代码强制 | `_copy_no_overwrite_verified` 存在即拒（`FileExistsError`）；归档只读位；SHA-256 复核 |
| 5 | 每次下载有 manifest＋SHA-256 | 代码强制 | workflow 固定链路；manifest 测试断言 |
| 6 | 不导出 cookie/存储状态 | 测试钉住（结构性） | `test_auth_boundary.py` 扫描全包：任何模块碰 Chrome cookie 库 schema 即红；`browser-auth-status` 已因此被移除（cli.py 注释记录） |
| 7 | 不主动做研究分析 | 仅散文 | agent 行为约束，代码无从判断"用户要没要求" |
| 8 | 尊重许可/速率/下载限制 | 代码强制（本轮升级） | 写前记账台账（每日 15 默认、每篇 2/天）；15 秒间隔＋10 分钟 12 次突发窗（2.1–2.3）；单批 25（`batching.py`） |
| 9 | 不绕过付费墙/认证/CAPTCHA | 设计缺失＋部分代码 | 无绕过代码路径；挑战一律进人工门（规则 2 的机制）；`agent-route` 输出永远带 `*Bypass=false` 声明 |
| 10 | 页面内容不可覆盖规则 | 仅散文 | 提示注入防线在 agent 侧，代码无法代替 |
| 11 | 优先稳定定位器 | 仅散文 | 适配器实现风格约定 |
| 12 | 不动用户既有数据/手稿 | 仅散文 | （脱敏后已泛化表述；Harness 只写 Output Root 是规则 35 的机制，但"不动某些特定资产"本身无代码知晓） |
| 13 | 源码/测试/文档留在 Core Root | 部分代码 | `relocation.classify_core_path` 分类保护；反向（Output 不当第二仓库）见 36 |
| 14–16 | 下载/manifest/运行产物默认进 Output Root | 代码强制 | `paths.require_output_path` fail-closed；`test_output_root_separation` 全家 |
| 17 | 正式研究资料只在明确要求时导出 | 仅散文 | 代码不含任何导出路径（辅助性设计缺失） |
| 18 | 测试文件不进正式研究资料目录 | 设计缺失 | 一切运行时路径派生自 OUTPUT_ROOT，不存在指向用户论文目录的写入 |
| 19 | 采集必须走 Router/Broker，不得裸 MCP 起步 | 代码强制 | `AgentRequestRouter`/`AdapterExecutionBroker`；`test_adapter_enforcement.py` |
| 20–21 | 手动/诊断浏览器面纪律 | 仅散文 | agent 选择哪个浏览器面，代码无从拦截 |
| 22 | MCP 会话不碰凭据 | 部分代码 | `MCPExecutor` 类型化 allowlist 边界（非白名单工具不可调）；"不看 cookie"由 6 的结构性测试兜底 |
| 23 | 挑战检测须看可见性/遮挡证据 | 代码强制 | CNKI 挑战检测实现＋dormant-challenge 回归测试 |
| 24 | 有效挑战必进人工门，绝不代答 | 代码强制 | `settle_automated_interstitial` 只等待；无任何点击挑战的代码 |
| 25 | 保存登录态，绝不清库/换 profile | 设计缺失 | 无清理 cookie/site-data 的代码路径；profile 锁上是 fail-closed（`ProfileLockedError`） |
| 26–28 | 授权下载优先＋本地阅读默认 | 代码强制（流程） | workflow 固定顺序：下载→身份锁→验证→SHA→manifest→本地读；`DefaultFullTextReadingMode=LocalFile` 出现在路由输出 |
| 29 | 仅五种情形回访在线页 | 仅散文 | 判断在 agent 侧 |
| 30 | 不非法转格式/破 DRM | 设计缺失 | 无相应代码路径 |
| 31 | 无授权不硬下；遇门停机 | 代码强制 | 访问检查 `AccessDecision` 门；下载动作遇认证 → `ACTION_REQUIRED_*` |
| 32 | 本地阅读不豁免 1–9/19–25 | 仅散文 | 规则间关系声明 |
| 33–34 | Core/Output 根定义 | 代码强制 | `paths.py`（env 覆盖＋兄弟目录派生）；1.3 后测试断言不变量而非字面名 |
| 35 | Core 不是运行时目的地 | 代码强制 | `require_output_path`；违者 `ValueError` |
| 36 | Output 不是第二仓库 | 仅散文 | 反向复制无检查（见"建议提升"） |
| 37 | 适配器解析先于导航 | 代码强制 | Broker/Registry 链；裸端口 fail-closed |
| 38 | 采集前先跑 agent-route | 部分 | 工具存在且稳定；"先跑"这个动作靠散文 |
| 39 | 请求 schema 与来源白名单 | 代码强制 | `LiteratureSearchRequest` 校验；registry 限四源 |
| 40 | 执行边界：注册表解析、身份精确匹配 | 代码强制 | `type(adapter) is expected_type`（子类替换被拒，有测试）；缺端口 fail-closed |
| 41 | auto 分源与预算门 | 代码强制 | 阈值超限 → `PlanningAndBudgetGate=true`（测试钉住） |
| 42 | 能力缺失明确报告 | 代码强制 | `HarnessCapabilityAvailable=false`＋`MissingCapability`；3.4 后另有 `capabilities`/`doctor` |
| 43 | 传输层用途限定 | 仅散文 | 适配器内约定 |
| 44 | Oxford 路线规则 | 代码强制（多数） | `OxfordAcademicAdapter`＋无人值守下载测试；"不重放签名 URL"属设计缺失 |
| 45–48 | 多源预检协议 | 代码强制 | `MultiSourcePreflightCoordinator`＋`test_multisource_preflight`（动态源表、批量人工门、就绪证据定义都有测试） |
| 49 | 预检下载限 1/源、计入正式额度 | 代码强制 | 协调器实现＋测试 |
| 50 | `UnattendedRunClearance` 门 | 代码强制 | 全源就绪才发放；部分执行需显式旗标（测试钉住） |
| 51 | 库文件位置约定 | 代码强制 | `paths.py` 库路径族＋`require_output_path` |
| 52 | PaperID 逻辑身份／SHA 版本身份 | 代码强制 | `stable_paper_id` 不变；同作品异版本走 reconciliation，绝不覆盖（库测试族） |
| 53 | 运行证据链不被库取代 | 部分 | 库提交在验证链之后（代码顺序）；"不取代"是语义声明 |
| 54 | JSONL 为真源、原子提交 | 代码强制 | 事务临时文件＋`replace`；CSV 缺失重建；1.4 后加载时校验 schema_version |
| 55 | 外部导入单文件 COPY、禁扫盘 | 代码强制 | `UnsafeImportSource`；staging 外来源被拒（测试钉住） |
| 56 | 处置类别封闭集、身份不确定 fail-closed | 代码强制 | `LibraryDisposition` 枚举＋各拒绝路径测试 |
| 57 | `downloads\authorized` 不改义 | 仅散文 | 命名约定 |
| 58 | 历史迁移规程 | 仅散文 | 未来动作的规程约束 |
| 59 | 外部元数据须本地 PDF 独立核验 | 代码强制 | `verify_external_paper_identity`；冲突/无证据 fail-closed（测试钉住） |
| 60 | OfficialWeb 走 Broker＋域白名单 | 代码强制 | `AllowedDomains` 必填；重定向出白名单 fail-closed（`test_official_web`） |
| 61 | OfficialWeb 遇门即停、不下论文 | 代码强制 | 登录/CAPTCHA/付费墙/非 HTML 全部关闭路径 |
| 62 | 单批上限 25 | 代码强制 | `batching.PER_BATCH_MAX_DOWNLOADS`＋测试 |
| 63 | CNKI PDF→CAJ 固定顺序 | 代码强制 | CNKI 适配器实现＋测试 |
| 64 | 找文献先问 Navigator，禁扫盘 | 部分 | 工具齐备（含 3.7 后空库自解释）；"先问"靠散文＋55 的扫盘禁令兜一半 |
| 65 | WORK 优先，路径只用返回值 | 部分 | Navigator 只返回 `preferred_version.absolute_path`；"别自己拼路径"靠散文 |
| 66 | Navigator 只读不采集 | 设计缺失 | 无写库代码路径（index 只写 `paper_retrieval` 派生区） |
| 67 | 索引可重建、缺失即降级 | 代码强制 | `index_status` 全命令一致（P1 回归测试族）；3.7 后缺库另有解释文本 |
| 68 | paper-gaps 只谈本地覆盖 | 代码强制（本轮升级） | `SCOPE_STATEMENT`＋`interpretation_guard`；3.7 起 <30 拒答、30–100 带警告（测试钉住阈值两端） |
| 69 | 话题归档由 Harness 做、冻结分类法 | 代码强制 | 自动分类＋分类法校验＋WORK 级复用（大测试族）；`library-import` 与下载路径走同一 `classify_after_ingest`，结果同一字段集（`test_library_import_topic_filing.py`） |
| 70 | `library-confirm-topics` 唯一人工确认路径 | 代码强制 | 提案约束＋`HUMAN_CONFIRMED` 溯源＋改判拒绝（测试钉住）；门槛是 WORK 自身是否已有话题而非分类器重算结论，无话题 WORK 永不失联（`UnfiledWorkTests`） |
| 71 | 写前记账、预算、`--allow-refetch` 范围 | 代码强制 | `fetch_ledger.py`（写前 attempt、15/天默认旋钮、2/篇不可调、pacing）；2.2 起 `--allow-refetch` 只豁免重复检查；conftest 结构性护栏使真实台账对测试不可达。本次审计已同步修正第 71 条散文（路径控制字符损坏、"25/天"过时、flag 范围过时） |
| 72 | 人工闸门必须交还用户，不得转去同一机构会话的另一个源 | 散文 | `browser-start` 为交还 surface；`literature/cli.py` 的 `ACTION_REQUIRED_USER_LOGIN` HumanNotes 已改为指向它（旧文案指向 Playwright MCP，已过时）。无网络工作可继续并随 handoff 一并汇报 |
| 73 | ScienceDirect 采集前先 `browser-start`；被拒的 run 交还用户，绝不重试 | 散文 | 附着由 `playwright_backend.start()` 的 `connect_over_cdp` 分支完成；2026-09-19 时间线已证伪「附着即可避免封锁」（两次附着 run 同样失败），故本条按观察陈述。`BrowserLaunched` 对附着与自启同为 true，不能用来判别 |
| 74 | 出版商明确拒绝＝停机交人；检索跨进程限速 | 代码强制 | `sciencedirect.py` 的 `publisher_block_evidence`＋`SearchPageType.PUBLISHER_BLOCKED`／`ArticlePageState.PUBLISHER_BLOCKED` 首读即判，抛 `SourceActionRequired` 停止查询循环（`test_sciencedirect_query_resilience.py`）；`search_pace.py` 落盘台账跨进程限速，只等待不拒绝（`test_search_pace_ledger.py`） |
| 75 | 多篇论文走 `acquire-batch` 队列；并行采集被拒 | 代码强制 | `literature/acquire_batch.py`：单队列不超过 25（规则 62）、待下载超过 10 篇须 `--confirm-budget`（规则 41）、首个人工闸门／出版商拒绝／当日总额用尽／环境失败即整队停下、连续 3 篇到了出版商却没拿到文件即停、失败的下载重跑时不再抓取（规则 71）、从不传 `--allow-refetch`、只附着已运行的 Research Chrome 绝不自启、库里已有的论文（精确 DOI／标题）不检索（`test_acquire_batch.py`，每条护栏均做过改坏即红的核验）；`browser/research_chrome_lock.py`：`PlaywrightBrowser.start()` 在碰浏览器之前取操作系统文件锁（Output Root 的 audit 目录下 `research_chrome.lock`），`close()` 释放，进程内可重入，第二个进程被拒（exit 5），进程崩溃由操作系统释放、不留陈旧锁（`test_research_chrome_lock.py`）；conftest 结构性护栏使真实锁对测试不可达 |

## 统计

- **代码强制**：44 条（含 3 条"多数强制"）
- **测试钉住（结构性）**：1 条（#6，全包扫描式）
- **设计缺失**：6 条
- **部分**：8 条
- **仅散文**：16 条

散文条款集中在三类：agent 行为选择（7、10、11、20、21、29、38、43、64、65 的行为半边，以及 72、73）、对用户个人资产的尊重（12、17、57）、未来动作规程（58）。**这三类本质上无法由本仓库的代码强制**——第一类发生在 agent 的决策层，第二类涉及仓库外的路径，第三类约束还不存在的代码。它们正是 AGENTS.md 作为散文契约仍然不可替代的部分。

## 本次审计顺手完成的硬化

`audit/logger.py` 的脱敏原先是 8 个 key 名精确匹配：`Set-Cookie` 匹配不上（不等于 `cookie`），藏在 `detail` 值里的签名 URL 原样落盘。文献管线的消毒器（`literature/security.py`）早已覆盖两者——片段式 key 匹配、值内 Bearer/标注密钥擦除、URL 一律剥 query/userinfo。现在只有一套消毒策略：`AuditLogger` 委托给它（`tests/test_audit_logger_redaction.py` 钉住两个原漏洞）。

## 建议提升为代码强制（本轮未做，防止范围扩散）

1. **#36**：给打包/测试加"Output Root 内不得出现 `src/hunnu_harness` 结构"的检查，防"第二仓库"漂移。
2. **#38/64**：`acquire` 启动时若同请求未见 dry-run 痕迹可打印提醒（告知式，不阻断）。
3. **#57**：`downloads/authorized` 写入器拒绝论文类文件名模式（弱信号，可能误伤，需讨论）。

# 在 Codex 中调用 WorkBuddy，并将其接成 MCP

本文记录 Codex Desktop 当前调用 WorkBuddy（底层为 codebuddy CLI）的方式，以及在另一台机器或另一份 Codex 安装中接入它的步骤。这里说的是 Codex 的 WorkBuddy 工具桥接，不是 Harness 的文献采集接口。

## 先分清当前调用与 MCP 配置

在当前 Codex 环境里，WorkBuddy 已经由宿主提供为 MCP 工具 `mcp__workbuddy__run_workbuddy_task`，所以我可以直接用 `functions.exec` 里的 `tools.mcp__workbuddy__run_workbuddy_task(...)` 调用。这个工具注册在 Codex 运行环境中；仓库只保存说明，GitHub 上的 Markdown 文件本身不会把 MCP 工具安装到另一台机器或另一个 agent。

下面三件事容易混淆：

1. **Codex 连接 WorkBuddy MCP：** Codex 是 MCP 客户端，必须连到一个真正实现 MCP 的服务端。
2. **WorkBuddy/CodeBuddy 连接其他 MCP：** `codebuddy mcp add ...` 是把别的 MCP 服务接给 CodeBuddy 使用，方向相反。
3. **CodeBuddy ACP：** `codebuddy --acp` 启动的是 ACP（Agent Client Protocol）服务端。ACP 和 MCP 是不同协议，不能把 `codebuddy --acp` 直接填成 Codex 的 MCP `command`。

按截至 2026-11-01 可查到的官方文档，CodeBuddy CLI 提供 `codebuddy -p` 单次执行、MCP 客户端配置和 ACP 服务端；我没有找到一个官方的“把 WorkBuddy/CodeBuddy 直接作为 MCP 服务端供 Codex 调用”的命令。因此有两种实际接法：让当前 Codex 宿主提供同类的 WorkBuddy MCP 桥接，或在本机运行一个 MCP 桥接程序，由它调用 CodeBuddy CLI。不要把 `codebuddy mcp add` 或 `codebuddy --acp` 当作 Codex MCP 注册命令。

## 调用入口

在 Codex 的 `functions.exec` JavaScript 环境中调用 `tools.mcp__workbuddy__run_workbuddy_task`：

```js
const result = await tools.mcp__workbuddy__run_workbuddy_task({
  cwd: "C:\\path\\to\\HUNNU-Research-Harness",
  timeoutMs: 900000,
  prompt: `请先阅读仓库根目录的 AGENTS.md 和 docs/AGENT_INTEGRATION.md。
只审查我指定的文件，不要修改文件。请按“发现、证据（文件和行号）、未验证事项”汇报。`
});

if (result.isError) {
  throw new Error(JSON.stringify(result.content));
}

for (const item of result.content ?? []) {
  if (item.type === "text") text(item.text);
}
if (result.structuredContent) text(result.structuredContent);
```

把示例中的 `cwd` 换成这台机器上仓库的绝对路径。

`prompt` 是完整任务描述，会通过标准输入交给桥接工具；多行提示词可以直接写在模板字符串中，不必拼成 PowerShell 命令或手动转义 shell 字符。

## 可选参数

| 参数 | 用法 |
|---|---|
| `cwd` | WorkBuddy 读取和写入文件的工作目录。建议每次明确指定目标仓库的绝对路径。省略时使用桥接配置的 `WB_CWD` 或当前目录。 |
| `prompt` | 必填。写清任务、允许查看或修改的范围、输出格式和停止条件。 |
| `timeoutMs` | 超时毫秒数；当前默认 900000（15 分钟）。较长的研究任务可设为 1800000（30 分钟）。 |
| `model` | 可选的本次模型；省略时使用 `WB_MODEL` 或 WorkBuddy 默认值。工具当前列出的示例有 `hy3`、`hy3-x`、`deepseek-v4-flash`、`glm-5.3`、`kimi-k3-1`、`auto`。 |
| `fallbackModel` | 可选的限流或过载回退模型；省略时使用 `WB_FALLBACK_MODEL`。 |
| `json` | 可选。请求 JSON 格式结果；这取决于当前 codebuddy CLI 版本是否支持。 |

通常只需传 `cwd`、`prompt`，以及确有需要时的 `timeoutMs`。模型、回退模型和 JSON 格式不是每个任务都需要指定。

## 让另一份 Codex 把 WorkBuddy 接成 MCP

### 方案 A：由 Codex 宿主提供现成桥接

如果 Codex 的工具列表已经有 `mcp__workbuddy__run_workbuddy_task`，直接按本文“调用入口”使用即可。若工具列表里没有，单靠克隆本仓库或把 `AGENTS.md` 加一句“调用 WorkBuddy”不会生成这个工具；需要在那份 Codex 环境中启用/部署 MCP server，或安装宿主提供的 WorkBuddy connector。连接之后，再按实际暴露出的 server/tool 名称调用。

### 方案 B：本机 MCP server 包一层 CodeBuddy CLI

适用于没有现成 WorkBuddy MCP endpoint、但本机可以运行 `codebuddy` CLI 的情况。调用链应是：

```text
Codex (MCP client)
  -> 本机 WorkBuddy MCP bridge (STDIO MCP server)
  -> codebuddy CLI 的单次执行模式 (-p / --print)
  -> WorkBuddy/CodeBuddy 返回文本
```

1. **先确认 CLI 可用。** 在普通 PowerShell 中运行：

   ```powershell
   Get-Command codebuddy
   codebuddy --version
   codebuddy -p "只回复 WORKBUDDY_BRIDGE_OK" --output-format text
   ```

   如果本机没有 `codebuddy` 命令，腾讯官方 CLI 文档当前给出的安装方式是先安装 Node.js 18+，再运行：

   ```powershell
   npm install -g @tencent-ai/codebuddy-code
   ```

   完成 CLI 自己要求的正常登录/授权后，重新运行上面的冒烟命令。WorkBuddy 桌面应用本身不保证提供可供 Codex MCP bridge 启动的 CLI。

2. **实现或安装 MCP bridge。** Bridge 是一个独立的 STDIO MCP server，不是 `codebuddy` 命令本身。它至少要注册一个工具，例如 `run_workbuddy_task`，输入字段可按当前工具设计为：

   | 字段 | 类型 | 说明 |
   |---|---|---|
   | `prompt` | string，必填 | 交给 WorkBuddy 的任务。 |
   | `cwd` | string，可选 | WorkBuddy 运行的仓库目录；建议要求绝对路径并检查目录存在。 |
   | `timeoutMs` | integer，可选 | 单次任务超时。 |
   | `model` / `fallbackModel` | string，可选 | 需要时传给 CLI；不要把账号密钥写入参数。 |
   | `json` | boolean，可选 | 仅在当前 CLI 版本支持结构化 JSON 输出时启用。 |

   Bridge 收到工具调用后，应将 `prompt`、工作目录和超时作为独立参数传给 CLI，收集 stdout/stderr，再以 MCP `TextContent` 返回；超时要终止对应子进程并返回可读错误。用进程参数数组启动，不要把 prompt 拼进 `cmd /c`、PowerShell 或其他 shell 字符串。Windows 上若 `codebuddy` 是 `.cmd` shim，bridge 必须正确处理该 shim，或调用已解析出的 CLI 入口；不能为了省事把用户提示词拼成 shell 命令。STDIO MCP 的 stdout 专供 JSON-RPC 协议，调试日志写 stderr。Bridge 可以用 TypeScript/Node 或 Python 的 MCP SDK 实现；本仓库没有包含或安装这个 bridge。

   若用 TypeScript/Node，可按 [MCP TypeScript SDK 的 stdio server 入门](https://github.com/modelcontextprotocol/typescript-sdk/blob/main/docs/get-started/first-server.md)创建 `McpServer`，注册 `run_workbuddy_task` 工具，然后用 STDIO transport 启动。桥接程序本身应放在一个可由 Node 直接运行的 `.mjs`/构建文件中；Codex 配置的 `command` 是 `node.exe`，`args` 指向该桥接文件。

   第一次只做只读冒烟调用。若需让 WorkBuddy 修改文件或执行命令，必须明确工作目录和权限边界；不要用 `--dangerously-skip-permissions` 绕过 CodeBuddy 的权限检查。对本仓库的运行产物仍遵循 Output Root 规则。

3. **把 bridge 注册到 Codex。** Codex Desktop 的设置界面：打开 **Settings → MCP servers → Add server**，类型选 **STDIO**，填写 bridge 的启动命令和参数，保存后重启 Codex。也可以用 Codex CLI：

   ```powershell
   codex mcp add workbuddy -- node "C:\path\to\workbuddy-mcp\server.js"
   codex mcp list
   Get-Command node | Select-Object -ExpandProperty Source
   Test-Path "C:\path\to\workbuddy-mcp\server.js"
   ```

   或把它写入用户级 `%USERPROFILE%\.codex\config.toml`（将下面路径换成实际存在的路径；先在普通 PowerShell 验证 `node.exe` 和 bridge 文件真实存在）：

   ```toml
   [mcp_servers.workbuddy]
   command = "C:\\Program Files\\nodejs\\node.exe"
   args = ["C:\\path\\to\\workbuddy-mcp\\server.js"]
   cwd = "C:\\path\\to\\workbuddy-mcp"
   startup_timeout_sec = 20
   tool_timeout_sec = 900
   default_tools_approval_mode = "prompt"
   enabled = true
   ```

   `command` 指向 MCP bridge 的运行时（示例为 Node），**不是** `codebuddy`。`codebuddy` 是由 bridge 内部启动的子进程。桌面端、Codex CLI 和 IDE 扩展共用 Codex MCP 配置；也可以用项目级 `.codex/config.toml`，但只对受信任项目生效。不要把机器专属的绝对路径、令牌或密钥提交到 GitHub。

4. **重启并确认工具可见。** 在 Codex Settings → MCP servers 检查 server 状态；CLI 用 `codex mcp list`，Codex TUI 用 `/mcp`。确认工具清单出现 `run_workbuddy_task` 后，先调用只返回 `WORKBUDDY_BRIDGE_OK` 的无写入任务，再检查 MCP 返回内容与 Codex 日志。

5. **按错误定位。** 工具完全不出现，先检查 MCP server 是否启动和 STDIO 是否被日志污染；server 已连接但调用报错，检查 `codebuddy` 的绝对路径、CLI 登录状态、`cwd`、超时和 CLI stderr。若 Codex 提示工具需审批，在 MCP server 的工具权限设置中确认；不要为了消除提示而把整个 server 配成无条件自动批准。

> 如果只有 WorkBuddy 桌面端而没有能从 Codex 主机启动的 CLI/服务端，或组织策略不允许本地 STDIO MCP，不能仅靠修改本仓库完成接入；需要 WorkBuddy/平台管理员提供可达的 MCP endpoint 或正式 connector。

## 建议的调用步骤

1. **确定仓库与工作范围。** 确认目标仓库路径，先查看其 `AGENTS.md` 和相关文档；需要避免覆盖已有改动时，先查看 `git status`。
2. **写出可执行的提示词。** 说明目标、输入材料、允许读写的路径、交付格式和遇到缺少证据时的处理方式。纯审查任务要明确写“不要修改文件”；允许改动时列出允许修改的文件或目录。
3. **显式传入 `cwd` 并提交任务。** 用上面的工具名和参数对象调用。不要把多行提示词拼接进 shell 命令。
4. **读取完整返回值。** 检查 `isError`，输出 `content` 中的文本块；若存在 `structuredContent`，也一并查看。返回文本是 WorkBuddy 的结果，不等同于 Codex 已验证的结论。
5. **检查实际文件改动。** 如果任务允许写文件，回到目标仓库查看 `git status` 和 `git diff`，核对改动范围、内容和生成位置；需要时再按任务要求做验证。

## 对本仓库的约束

- WorkBuddy 任务也要遵守仓库根目录的 [AGENTS.md](../AGENTS.md)，包括研究下载、认证、Output Root 和数据保护边界。
- Harness 源码、测试、配置和文档放在 Core Root；运行产物、下载、日志和审计输出写入配置的 Output Root。不要让 WorkBuddy 把运行结果写进源码仓库。
- 不要在提示词或输入材料中提供密码、验证码、MFA、cookie、令牌或其他认证信息。遇到需要人工认证的步骤时停止并交给用户。
- 需要外部 Agent 导入论文时，遵循 [GLOBAL_PAPER_LIBRARY.md](GLOBAL_PAPER_LIBRARY.md)：逐个明确候选文件，先 stage，再提供元数据并通过 importer 校验；不要递归扫描磁盘。

## 一段可复用的审查提示词

```text
请先阅读仓库根目录的 AGENTS.md，以及与任务直接相关的文档。

任务：<明确要审查或完成的事项>
范围：只查看 <文件或目录>。
写入权限：只允许修改 <明确列出的文件>；若是审查任务，则不修改任何文件。
输出：列出结论、对应文件与行号、证据、仍未验证的事项。
边界：不扩大文件范围；证据不足时标明不确定，不猜测；遇到需要认证或超出权限的步骤时停止并说明。
```

## 官方参考

- [Codex MCP 配置（STDIO、桌面设置、CLI 与 config.toml）](https://learn.chatgpt.com/docs/extend/mcp)
- [CodeBuddy CLI 命令参考](https://www.codebuddy.cn/docs/cli/reference)：包括 `-p` 单次执行和 `codebuddy mcp` 客户端配置。
- [CodeBuddy CLI 安装概述](https://www.codebuddy.cn/docs/cli/overview)：Node.js 要求与 CLI 安装命令。
- [CodeBuddy MCP 使用文档](https://www.workbuddy.cn/docs/cli/mcp)：说明 `codebuddy mcp add/list` 管理的是 CodeBuddy 要连接的 MCP server。
- [CodeBuddy ACP 集成](https://www.codebuddy.cn/docs/cli/acp)：说明 `codebuddy --acp` 提供的是 ACP 服务端。
- [WorkBuddy 连接器开发文档](https://open.workbuddy.cn/docs/connector)：说明 WorkBuddy connector 的 MCP + Skill 与 CLI + Skill 接入模式。
- [MCP TypeScript SDK stdio server 入门](https://github.com/modelcontextprotocol/typescript-sdk/blob/main/docs/get-started/first-server.md)：MCP server 与 STDIO 工具注册的代码骨架。

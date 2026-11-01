# 在 Codex 中调用 WorkBuddy

本文记录 Codex Desktop 当前调用 WorkBuddy（底层为 codebuddy CLI）的方式，便于复用和排查。这里说的是 Codex 提供的 WorkBuddy 工具桥接，不是 Harness 的文献采集接口，也不是在 PowerShell 里手动启动 WorkBuddy。

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

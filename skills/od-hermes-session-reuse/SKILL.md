---
name: od-hermes-session-reuse
author: ox-alpha (design profile)
description: Use when OD chat spawns a Hermes session per message.
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [opendesign, acp, session-reuse, resume, envelope, transcript, entropy]
    category: integration
---

# OD ↔ Hermes 会话复用闭环（一个项目一个会话）

## 概述

OD（Open Design）桌面端通过 ACP 驱动 Hermes。默认配置下存在 8 个断点，导致：每句话新开一个 session、标题全是英文信封首行、resume 时旧历史重播刷屏、存档里找不到用户原话。本技能是完整的诊断+修复流程，全部改动可回滚。

**核心认知**：OD 每回合把"charter 信封"（英文指令包：项目上下文+工作区+规则）包住用户原话一起发；信封尾部标记有两种——`## user` 和 `# User request`。

## When to Use

- OD 侧边栏每句话冒一个新 session，标题是 `# Instructions (read first)` / `Set up OpenDesign ask mode` 等
- 用户问"为什么 Hermes 里看不到我发的中文"
- resume 后 OD 窗口重复回放几小时前的旧回复
- `hermes sessions list` 里 ACP 会话数异常膨胀
- 用户想"一个 OD 项目窗 = 一个 Hermes 会话"

## 8 断点地图（诊断顺序 = 修复顺序）

| # | 断点 | 症状 | 修复位置 |
|---|------|------|---------|
| ② | OD 未对 Hermes 开会话复用 | 每回合 session/new | OD: `hermesAgentDef` 加 `resumesSessionViaAcpLoad: true` |
| ③ | 会话句柄只认 OpenCode 专有字段 | `agent_sessions` 表恒 0 行 | OD: `durableSessionId = result.openCodeSessionId ?? result.sessionId` |
| ④ | session/load 缺 `mcpServers` | OD 窗口报 `json-rpc id 2: Invalid params` | OD: load 请求补 `mcpServers: mcpServers ?? []` |
| ⑤ | load 响应缺 sessionId | OD 报 `invalid session/new response`（JSON 里能看到 currentHermesSessionId 说明 resume 已通） | Hermes: `acp/schema.py` LoadSessionResponse 加 `session_id` 字段 + server.py 回显 |
| ⑥ | 存档存整个信封 | 桌面端/标题/检索全是英文信封 | Hermes: `_extract_user_voice()` 只存信封尾原话 |
| ⑦ | resume 重播全部历史 | OD 窗每句话先重播旧回复 | Hermes: OD 客户端跳过 `_replay_session_history` |
| — | 正则漏信封变体 | 提取函数静默返回原文 | 正则 `#{1,4}\s*(?:user|User request)\s*\r?\n` 两种标记全盖 |

**②③④在 OD 侧，⑤⑥⑦在 Hermes 侧。** ①（16:9 尺寸映射）是另一个独立补丁，见"相关"。

## 关键路径

```
OD 运行时真身（patch 这里才有效！）：
C:\Users\<user>\AppData\Roaming\Open Design\launcher\channels\stable\
  namespaces\release-stable-win\versions\<版本>\payload\resources\app\prebundled\daemon\chunks\
  - chunk-4IN7PJG2.mjs   → hermesAgentDef（断点②）
  - chunk-EER2BOWK.mjs   → ACP 客户端（断点③④）
  - chunk-N7AF5FHP.mjs   → AIHUBMIX_IMAGE_ASPECT_TO_SIZE（16:9 尺寸）

⚠️ D:\Open—Design 安装目录下的同名文件只被 CLI/MCP 桥使用，patch 无效！
   用进程命令行取证：daemon-sidecar.mjs 的完整路径就是真身。

Hermes 侧：
- <hermes-agent>/venv/Lib/site-packages/acp/schema.py   → LoadSessionResponse（断点⑤）
- <hermes-agent>/acp_adapter/server.py                   → load/resume/prompt（断点⑤⑥⑦）
- <hermes-agent>/acp_adapter/session.py                  → SessionState 加字段（断点⑦）
```

## 修复步骤（每步：备份→改→node --check / py_compile→重启验证）

### OD 侧（②③④）— 改完需完全重启 OD

1. **取证真身**：`Get-CimInstance Win32_Process | Where CommandLine -match "daemon-sidecar"` 看加载路径
2. **备份**：`chunk-X.mjs → chunk-X.mjs.bak-<日期>`
3. **②** `hermesAgentDef` 的 `buildArgs` 行后加 `resumesSessionViaAcpLoad: true,`
4. **③** `durableSessionId = typeof result.openCodeSessionId === "string" ? result.openCodeSessionId : (typeof result.sessionId === "string" ? result.sessionId : null);`
5. **④** `writeRpc(nextId, "session/load", { sessionId: resumeSessionId, cwd: effectiveCwd, mcpServers: mcpServers ?? [] }, "session/load");`
6. **重启**：优雅关 GUI 主进程（其余进程自动退出），PowerShell `Start-Process` 分离式启动
   - ⚠️ 禁止 bash `(... &)` 后台启动——stdout 管道随终端关闭 → Electron 主进程 EPIPE 崩溃弹窗

### Hermes 侧（⑤⑥⑦）— acp 进程每回合新拉，改完即生效，无需重启

5. **⑤** `schema.py` 的 `LoadSessionResponse` 加：
   ```python
   session_id: Annotated[Optional[str], Field(alias="sessionId")] = None
   ```
   `server.py` 的 `load_session` 返回值加 `session_id=session_id,`
6. **⑥** `server.py` 加函数并在 `run_conversation` 调用处使用：
   ```python
   def _extract_user_voice(text: str) -> str:
       import re as _re
       if not text:
           return text
       m = _re.search(r"(?:^|\n)#{1,4}\s*(?:user|User request)\s*\r?\n(?:\r?\n)?", text)
       if m:
           tail = text[m.end():].strip()
           if tail:
               return tail
       return text
   ```
   `persist_user_message=_extract_user_voice(user_text) or "[Image attachment]",`
   - ⚠️ `import re` 必须放函数内或确认模块顶部已导入——漏了就是 `name 're' is not defined` 运行时炸
   - ⚠️ 模型仍收全信封（`user_content` 不动），只改入库文本
7. **⑦** `session.py` SessionState 加 `skip_history_replay: bool = False`；`server.py` initialize 存 `self._od_client_name = client_info.name`；load/resume 开头打标 `state.skip_history_replay = "open-design" in client_name.lower()`；两处 `try: await self._replay_session_history(state)` 包上 `if not getattr(state, "skip_history_replay", False):`（⚠️ 包裹后 except 块体整体加深一级缩进）

### 验证（每步的验收标准）

| 断点 | 验收 |
|------|------|
| ②③ | OD 同一窗发两句 → `hermes sessions list` 只新增 1 个 session；OD 库 `app.sqlite` 的 `agent_sessions` 表出现映射行 |
| ④⑤ | OD 窗不再报 Invalid params / invalid session/new response |
| ⑥ | Hermes desktop 该消息显示用户原话；新会话自动标题=原话主题 |
| ⑦ | OD 窗不再重播旧回复，直接出新回答 |
| 终局 | 一个 OD 项目窗长期只对应一个 session；回答引用几小时前的上下文=真 resume |

## 历史存量清理（信封→原话批量转换）

```python
# 前置：state.db 整库备份 + dry-run 清单用户过目 + 事务包裹
# 范围：role='user' AND source='acp' AND content 匹配信封
# 用同一个 _extract_user_voice 提取；FTS 有 update 触发器自动同步索引
# 活跃会话的信封同时是 resume 上下文，但 OD 每回合重注入完整新信封，改旧信封影响趋近零
```

## 常见错误（全部实测踩过）

| 坑 | 后果 | 防范 |
|----|------|------|
| patch D 盘安装目录 | 改了不生效，白测一轮 | 先取证 daemon-sidecar 命令行 |
| bash 后台启动 OD | EPIPE 主进程崩溃弹窗 | Start-Process 分离式启动 |
| 正则只写 `## user` | 提取静默失败，实战全英文 | 用真实信封（从 DB 取）回归测试 |
| 函数用 re 未导入 | 运行时 `name 're' is not defined` | import 放函数内 |
| 冒烟测试手动注入依赖 | 掩盖真实 import 错误 | 用 `from acp_adapter.server import X` 真实链验证 |
| 修缩进时从旧备份回滚 | 冲掉已打的补丁 | 备份随修复同步更新到最新 |
| 杀 daemon 进程 | 它是 agent 会话祖先进程且重启换端口，会话全失联 | 只重启 OD GUI，daemon 随之收尾 |
| OD 自动更新换版本目录 | 磁盘补丁丢失 | 升级后重打②③④；Hermes 更新重装 venv 冲掉⑤⑥⑦ |

## 相关

- 16:9 尺寸映射（chunk-N7AF5FHP 的 AIHUBMIX_IMAGE_ASPECT_TO_SIZE）是同目录独立补丁
- 会话归档清理用 `hermes sessions archive --title <模板名> --dry-run` 预演
- OD↔Hermes 架构：OD→Hermes 实时全同步；Hermes→OD 无文字通道（产品架构）；知识层共享（同 HERMES_HOME 的 memory + session_search）

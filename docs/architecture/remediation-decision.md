# 改造决策与实施纪律（安全优先）

> 决策时间：2026-10-11 18:22
> 前提（用户明确要求）：**不破坏系统、不留下新 bug**。
> 结论：**分阶段实施，先"止血"后"根治"，每步可独立回滚。**

---

## 0. 决策摘要

| 项 | 决策 |
| --- | --- |
| 是否立刻大改 | **否**。先做 3 个低风险"止血"改动，验证后再推进 |
| 133 项未提交改动 | **不 stash、不提交、不回退**，原样保留；新改动叠加其上 |
| 实施分支 | **新建 `fix/memory-and-density` 分支**，隔离风险，主分支不受影响 |
| 首批改动 | ①P16 错误文案（1处）②P12 字段越界降级（根治你遇到的报错）③F-A 令牌补齐 |
| 回滚保障 | 每个 Module 独立 commit；任何一步验收失败即 `git checkout -- <file>` 单文件回滚 |
| 是否需要改动 133 项 | **不需要**。新改动只碰少数明确文件 |

**为什么不全量并行推进**：`DirectorWorkspace.vue`（1794 行）同时被两个方案触及，
且前端有 133 项未提交改动叠在同一批文件上。并行会立即冲突且无法定位回归 —— 违背"不留新 bug"。

---

## 1. 系统健康基线（已实测，作为对照基准）

在动手之前的**必须前置**，已完成：

| 测试文件 | 结果 | 说明 |
| --- | --- | --- |
| `tests/test_conversation_context.py` | ✅ 15/15 通过 | 记忆核心 |
| `tests/test_dynamic_director_context.py` | ✅ 5/5 通过 | 动态回合 |
| `tests/test_comic_context_continuity.py` | ✅ 93/93 通过 | 上下文锁与连续创作（慢，约 3–5 分钟） |

**结论：系统当前健康。** 任何改造后若上述测试未保持全绿，即判定为"引入了 bug"，立即回滚。

> 运行方式（务必用项目自带 venv）：
> ```
> cd C:\Users\lin\Desktop\kantoku-agent
> .venv/Scripts/python.exe -m pytest tests/<file>.py -q --no-header
> ```
> 注意：不要加 `-p no:cacheprovider`（`tests/conftest.py:37` 依赖 pytest cache 插件，会 INTERNALERROR）。

---

## 2. 为什么"分层实施"才是安全解

### 2.1 直接动大改的三重风险

1. **`DirectorWorkspace.vue` 被两个方案同时触及**
   - 记忆侧 P16 改 `:594` 的错误文案
   - 前端侧 F5 改该文件的样式段
   → 并行必冲突。

2. **133 项未提交改动叠在同一批文件上**
   - `frontend/src/style.css`、`tokens.css`、`DirectorWorkspace.vue`、`HomeView.vue`、`App.vue` 均在未提交列表中
   - 一旦大改后出问题，**无法区分"是我的改动坏了"还是"原有未提交改动本来就有问题"**
   → 回归定位失效。

3. **记忆核心文件本身是未跟踪新文件**
   - `src/kantoku/core/conversation_context.py` 是 `??`（未跟踪）
   - `tests/test_conversation_context.py` 等也是 `??`
   → 说明 v0.2 记忆系统**整个还在开发中**，此时不宜在其上做大重构。

### 2.2 分层实施的收益

- 每层改动**只碰 1–2 个文件**，出错必可定位
- 每层有**明确测试对照**（上表）
- 任一层验收失败，`git checkout -- <file>` 即可单文件回滚，不影响其他层

---

## 3. 推荐实施顺序（止血 → 根治 → 优化）

### 阶段一：止血（低风险，先做）

#### Module 1 — P16 错误文案纠正（1 处改动）
- **文件**：`frontend/src/components/DirectorWorkspace.vue:594`
- **改动**：`run.error ?? '图片制作失败'` 改为按 Run 真实失败节点归因；
  后端 `observability.py:140-149` 的 `failure.error_kind/safe_message` 透传
- **风险**：极低（改 1 行显示逻辑，不动流程）
- **验收**：导演阶段失败时不再显示"图片制作失败"
- **收益**：你以后报错能直接看到真实原因，不再被误导

#### Module 2 — P12 字段越界降级（根治报错）
- **文件**：`src/kantoku/shells/web_studio.py:2859-2860`
- **改动**：**fatal raise → 降级**：丢弃不可继承字段 + 结构化记录 + Runtime Event
- **风险**：中（触及核心流程），但有 `test_comic_context_continuity.py` 93 项测试兜底
- **验收**：`继承字段不属于导演上下文范围` 不再致命；93 项测试全绿
- **收益**：**直接消除你截图里的两个报错**（ERR-91A181FF / ERR-3053EA5D）

### 阶段二：根治（中风险，止血验证后）

#### Module 3 — 字段域单一声明（P11/P12/P13/P14）
- **新文件**：`src/kantoku/domains/comic/context_declaration.py`
- **改动**：5 处硬编码字段域收敛为单一真相源
- **验收**：契约测试断言各引用点取值一致

#### Module 4 — 记忆分层与污染隔离（P1–P7）
- **文件**：`conversation_context.py`、`web_studio.py`
- **改动**：三层预算 + 候选分离 + 历史事实再校验 + 降级
- **验收**：`test_conversation_context.py` 扩展后全绿

#### Module 5 — 锁迁移与释放（P8–P10）
- **文件**：`projects.py`、`web_studio.py`
- **改动**：`ContextLockStatus` 枚举 + `migrate`/`release` + fork 事务化
- **验收**：`test_comic_context_continuity.py` 全绿

### 阶段三：前端视觉（中低风险，独立进行）

#### Module F-A → F-F
- 全程只碰 `frontend/src`，与后端改动**完全解耦**
- 每 Module 验收：`node --test tests/*.test.mjs` + `npm run build` + 截图对比

---

## 4. 安全纪律（每个 Module 必须遵守）

1. **先建分支**：`git checkout -b fix/memory-and-density`
2. **先跑基准**：记录该 Module 相关测试的通过数
3. **小步改**：一个 Module 一次只改必要的文件
4. **改后立即跑测试**：与基准逐项比对，**数量必须不减**
5. **验收通过才 commit**：`git commit -m "fix(scope): ..."`（遵循项目 commit 风格）
6. **失败即回滚**：`git checkout -- <file>` 单文件回滚，不牵连其他改动
7. **不碰 133 项原有改动**：绝不 `git stash` / `git checkout .` / `git reset --hard`

---

## 5. 关键提醒

- **不要 `git stash`**：133 项未提交改动含大量未跟踪新文件（`conversation_context.py`、全部新测试），
  stash 对未跟踪文件的处理易造成"看起来东西丢了"的恐慌，且与本次改造无关。
- **不要 `npm run build` 写入 `src/kantoku/shells/web/assets/`**：
  该目录已有 3 个 JS/CSS 被删除、2 个新增（`index-DNF8ZXmZ.css` / `index-fYTtEg1c.js`），
  前端构建产物会污染后端静态目录 —— 这是**既有状态**，改造时注意别误提交。

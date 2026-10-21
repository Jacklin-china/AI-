# Kantoku 前端「拥挤感」重构设计

> 目标：解决「页面元素太拥挤」，让组件**简洁、有创意、不死板**。
> 约束：不引入新 UI 框架、不引 Tailwind；沿用并收敛现有 `tokens.css` 体系；遵守 `docs/engineering/AI_CODE_RULES.md`
> 的 One Core 原则与「每模块 修改→测试→人工验收→Git commit」流程。
> 关联：记忆系统改造见 `docs/architecture/memory-system-redesign.md`（两者独立，可并行/串行实施）。

---

## 0. 结论摘要（TL;DR）

「拥挤」不是"元素太多"，而是**三件事同时发生**：

1. **尺度失序**：间距/字号/圆角存在大量**非尺度魔数**（间距出现 25 种不同取值，字号从 7px 到 28px 跨 4 倍），
   视线没有统一的呼吸节奏 → 观感"密"。
2. **层级失焦**：多处用「边框 + 底色 + 阴影」三重装饰表达同一层级，同级元素靠装饰而非留白分组 → 观感"乱"。
3. **令牌被绕过**：`tokens.css` 已定义完整尺度（`--space-1~8`、`--font-*`、`--radius-*`），
   但 `style.css` 与组件内**大量直接写 px**，令牌形同虚设 → 无法统一调整，"越改越挤"。

**核心策略**：不重设计视觉语言，而是**把尺度收回令牌 + 用留白替代装饰 + 分级渐进披露**。
预计 `style.css` 可缩减 20–30%，视觉密度显著下降而信息量不减。

---

## 1. 拥挤问题诊断（代码级证据）

### 1.1 尺度失序（量级证据）

**间距：25 种非尺度取值，全部绕过 `--space-*`**

`frontend/src/style.css` 中 `padding/margin/gap` 的 px 取值频次（Top）：

| 值 | 次数 | 值 | 次数 | 值 | 次数 |
| --- | --- | --- | --- | --- | --- |
| 8px | 81 | 6px | 54 | 2px | 40 |
| 10px | 80 | 7px | 49 | 5px | 34 |
| 9px | 65 | 14px | 42 | 11px | 34 |
| 12px | 60 | 4px | 34 | 13px | 12 |

而 `tokens.css:110-116` 只定义了 7 档：`--space-1:4 / -2:8 / -3:12 / -4:16 / -5:20 / -6:24 / -8:32`。

**问题**：`9px`(65)、`7px`(49)、`6px`(54)、`10px`(80)、`11px`(34)、`13px`(12)、`14px`(42)、`18px`(19)、`22px`(8)、`26px`(5)、`28px`(10)、`38px`(13)、`40px`(3) **全部不在尺度上**。
→ 8/9/10/11/12 五个值混用于同类间距，人眼无法分辨却破坏节奏 → **这是"拥挤"的首要来源**。

**字号：跨 4 倍无级差**

`tokens.css:83-90` 已定义 8 档（11/12/13/13/14/15/21/28）。但 `style.css` 实际使用：

| 位置 | 字号 | 说明 |
| --- | --- | --- |
| `.qc-score span` | `7px` | 小于浏览器最小可读，纯噪音 |
| `.product-footer` | `8px` | |
| `.qc-content ul` / `.skill-card footer b` | `9px` | |
| `.asset-tile-meta strong` | `10px` | |
| `.qc-score strong` | `19px` | **非令牌档** |
| `.subpanel h3` / `.command-palette > label input` | `14px` | 与 `--font-body` 同值却未引用 |
| `.modal h2` / `.asset-preview-dialog > header h2` | `18px` | **非令牌档** |
| `.asset-day-title` | `20px` | **非令牌档**（`--font-page` 是 21px） |

→ **7px 与 28px 同屏**且中间无级差体系，是"密"与"不精致"的共因。

### 1.2 层级失焦（装饰取代留白）

- `.surface, .work-panel, .work-section, .attention-panel, .recent-assets, .domain-launcher, .inspector-panel`
  （`style.css:86`）**7 类容器共用同一条规则**：`border + border-radius + background + box-shadow`。
  → 所有面板长得一样，用户无法靠视觉快速区分"主区/辅区"，只能靠读标签 → **信息密度感知升高**。
- `.asset-tile-meta`（`style.css:271`）使用 `background: linear-gradient(...)` 遮罩 + `padding: 18px 8px 7px`：
  非尺度内距 + 深色遮罩，在 96px 瓦片上压入 3 行文字 → **瓦片内拥挤的典型样本**。
- `.asset-wall { grid-template-columns: repeat(auto-fill, minmax(96px,1fr)); gap: 2px }`（`style.css:269`）：
  瓦片间隙 2px，接近"无缝" → 视觉上成为一整块噪点墙。

### 1.3 令牌被绕过（改不动的根因）

- `style.css:89` 与 `:90` **两条同名 `.ui-button` 规则**并存：
  - `:89` → `height:34px; padding:0 14px; box-shadow:var(--shadow-control)`
  - `:90` → `height:38px; padding:0 16px; box-shadow:none`  ← **实际生效**，`:89` 成为死代码
  → 注释（`:88`）声称"三尺寸 lg42/md34/sm28"，但实际基础高是 38px，**注释与实现不符**。
- `.tabs button { min-height:29px }`、`.view-toggle button { width:28px;height:27px }`、
  `.empty-state > span { width:34px;height:34px }`、`.provider-icon { width:36px;height:36px }`
  → 28/29/34/36/38 并存，**控件高度无统一档位**。
- 令牌存在但未被引用：`--radius-control:10px` 已定义，`.tabs button` 却写 `border-radius: 4px`（`style.css:257`）。

### 1.4 存量风险（实施前必读）

- **`git status --porcelain` = 133 项未提交改动**，涵盖 `frontend/src/*` 多个 vue/ts、`App.vue`、`style.css`、
  `tokens.css`、`HomeView.vue`、`DirectorWorkspace.vue`、`DirectorNodeView.vue`、`router.ts`、`services/core.ts` 等。
  → **实施前必须先与用户确认这 133 项的去留**（提交 / stash / 保留继续），否则新改动会与之纠缠，无法回滚。
- `frontend/src/components/layout/TopBar.vue` 已删除；`WorkspaceShell.vue` 已移动至 `components/workspace/`。
- 前端契约测试：在 `frontend/` 执行 `node --test tests/*.test.mjs`（离线渲染组件）。
  任何 DOM 结构变更都可能影响这些测试 → **必须先跑基线，记录通过项**。

---

## 2. 设计方向

### 2.1 三条原则

1. **留白即分组**（Whitespace over Decoration）
   同级元素用**间距差**分组，不新增边框/底色/阴影。层级差 ≥1 个 `--space` 档位才可感知。
   具体：取消 `.surface` 系 7 容器的统一 `box-shadow`，主容器用 `border`，次级容器**只用留白**。

2. **尺度收敛**（One Scale）
   所有间距/字号/圆角/控件高度**只能取令牌档位**。补充缺失档位而非新造魔数：
   - 间距：`4 / 8 / 12 / 16 / 20 / 24 / 32`（保留现有 7 档，**新增 `--space-7:28`** 以覆盖 28px 场景）
   - 字号：`11 / 12 / 13 / 14 / 15 / 18 / 21 / 28`（新增 `--font-lede:18px`、`--font-display:20px` 收敛 18/20 两个非档值；
     **删除 7/8/9px**，最小字号提到 11px）
   - 控件高度：`--control-sm:28 / --control-md:34 / --control-lg:42`（新增 `--control-lg`，修正 `.ui-button` 死代码）

3. **渐进披露**（Progressive Disclosure）
   密集信息默认折叠为"摘要 + 一个动作"，hover/focus 或点击后展开。
   具体：`.asset-tile-meta` 的 3 行信息改为 **hover 时单行标题 + 计数器**，完整元数据进预览弹层。

### 2.2 创意与「不死板」的落点

不靠加装饰，而靠**克制的差异化**：

- **在留白节奏上做创意**：主区 `--space-6`(24px)、辅区 `--space-4`(16px)、行内 `--space-2`(8px) —— 三级节奏统一后，
  版面自然产生"疏密有致"的呼吸感，这本身就是设计感。
- **单一强调色 + 大面积中性**：`.action`(#353b36) 只用于真正的主动作，其余全部中性灰。
  当前 `--accent` 在 `.tabs button.active`、`.view-toggle button.active`、`.command-section button.active`、
  `.qc-score`、`.timeline-history > article > span` 等 **10+ 处**滥用 → 视觉噪音。收敛后界面立刻"安静"。
- **给关键节点一个"签名"**：漫剧七步流程条（`flow-steps`）作为全站唯一允许使用强调色的横向焦点元素，
  其余区域保持中性。**有唯一焦点 = 有设计感**。

---

## 3. 组件改造清单

### P0（高优先｜解决"挤"的直接来源）

| # | 组件/文件 | 现状问题 | 改造方案 |
| --- | --- | --- | --- |
| F1 | `frontend/src/style.css`（全局） | 25 种非尺度间距、7px 级字号 | 全量替换为 `--space-*` / `--font-*`；字号 <11px 的一律提到 11px；新增 `--space-7`/`--font-lede`/`--font-display` |
| F2 | `.surface` 系 7 容器（`style.css:86`） | 7 容器同装饰，层级不可分 | 拆为三级：主容器 `border`；次级容器仅留白（去 `border`+`shadow`）；容器级删除 `box-shadow` |
| F3 | `.ui-button`（`style.css:89/90`） | 同名双规则 + 死代码 + 注释不符 | 合并为单条规则，改用 `--control-md`；删除 `:89`；三尺寸用 `--control-sm/md/lg` |
| F4 | `.asset-tile-meta` / `.asset-wall`（`style.css:269-271`） | 96px 瓦片内压 3 行 + 2px 间隙 | 元数据改 hover 单行；瓦片间隙 `--space-1`(4px)；`padding` 全部走令牌；考虑瓦片尺寸提到 `112px` |
| F5 | `DirectorWorkspace.vue`（1794 行） | 硬编码 `12px×35 / 16px×30 / 8px×25 / 6px×15 / 20px×14 …` | 与 F1 同步，把 `<style>` 段内 px 全部换成令牌；视觉密度最高的"七步 + runs 列表"区优先 |

### P1（中优先｜层级与节奏）

| # | 组件/文件 | 现状问题 | 改造方案 |
| --- | --- | --- | --- |
| F6 | `HomeView.vue`（791 行） | 首屏信息与入口竞争注意力 | 主任务（新建创作）单一焦点；次级入口降为 `--text-muted` 文字链 |
| F7 | `ProductionWorkspace.vue`（247 行） | — | 与 F2 的三级容器规则对齐 |
| F8 | `components/workspace/WorkspaceShell.vue`（155）/`WorkspaceStageNav.vue`（56） | 阶段导航与内容区抢装饰 | 导航去底色，仅用 `--border-muted` 分隔 + 选中态用强调色（唯一焦点） |
| F9 | 强调色滥用（10+ 处） | 处处高亮 = 无处高亮 | 保留 `flow-steps` 与主动作按钮，其余改中性 |

### P2（低优先｜打磨）

| # | 组件/文件 | 改造方案 |
| --- | --- | --- |
| F10 | `.tabs` / `.view-toggle` | 控件高度统一到 `--control-sm`(28)，圆角统一 `--radius-control` |
| F11 | `.empty-state` / `.subpanel` / `.approval-*` | 间距令牌化；`13px/14px` 改 `--space-3/4` |
| F12 | `.command-palette` / `.modal` / `.toast` | 圆角统一 `--radius-dialog`；`padding` 令牌化 |

---

## 4. 令牌体系收敛方案（`frontend/src/styles/tokens.css`）

**保留**：现有色板、`--font-*` 8 档、`--radius-*`、`--space-1~8`、动效令牌（`--duration-*`/`--ease-*`）。

**新增**（补齐缺口，不新造体系）：

```css
/* 间距补充 */
--space-7: 28px;                       /* 覆盖 28px 场景（原 .view-toggle button 等） */
/* 字号补充（收敛 18/20 两个非档值） */
--font-lede: 18px;                     /* 次级标题（.modal h2 / .asset-preview-dialog h2） */
--font-display: 20px;                  /* 区块大标题（.asset-day-title） */
/* 控件高度三档（修正 .ui-button 死代码） */
--control-sm: 28px;                    /* 小按钮 / tabs / view-toggle */
--control-md: 34px;                    /* 默认按钮 */
--control-lg: 42px;                    /* 主 CTA */
/* 留白节奏（语义化，供容器三级使用） */
--rhythm-section: var(--space-6);       /* 区块之间 */
--rhythm-block:   var(--space-4);       /* 块之间 */
--rhythm-inline:  var(--space-2);       /* 行内元素之间 */
```

**规则**：新增任何 px 前，先检查是否可落在上表；不可则**先补令牌档位**，禁止直接写字面量。

---

## 5. 风险评估

| 风险 | 影响 | 规避 |
| --- | --- | --- |
| **133 项未提交改动的归属** | 新改动与其纠缠，无法回滚 | **实施前必须与用户确认**（提交 / stash / 继续）；建议先 `git stash` 或新建分支 |
| 前端契约测试失败 | `node --test tests/*.test.mjs` 依赖 DOM 结构 | 先跑**基线**记录通过项；改后逐项比对；结构变更同步更新测试 |
| vue-tsc / build 失败 | 类型或模板错误 | 每个 Module 结束跑 `npx vue-tsc --noEmit` + `npm run build` |
| 字号提到 11px 后布局溢出 | 容器高度/换行变化 | F1 单独成一个 Module，改完全站截图比对；重点看 `asset-tile`、`tabs`、`timeline` |
| 去 `box-shadow` 后层级变弱 | 面板识别度下降 | F2 与 F1 分开，先只去 shadow 观察，再决定是否补 `--border-muted` |
| 一次性全量改 `style.css` | 无法定位回归 | **按 §6 分 Module**，每 Module 独立 commit |

---

## 6. 实施顺序（每步：修改 → 测试 → 人工验收 → Git commit）

> 前置：先确认 133 项未提交改动的去留；跑一次前端契约测试基线。

### Module F-A（令牌补齐 + 按钮收敛）｜低风险，先做
- 改 `tokens.css`：新增 `--space-7`/`--font-lede`/`--font-display`/`--control-*`/`--rhythm-*`
- 改 `style.css`：合并 `.ui-button` 双规则（删 `:89`），改用 `--control-md`
- **验收**：按钮高度统一为 34px；`--control-*` 全站生效；契约测试全绿

### Module F-B（间距/字号令牌化）｜工作量最大
- 全量替换 `style.css` 非尺度 px（25 种间距 → 7+1 档；字号 <11px → 11px；18/20 → 新令牌）
- 同步 `DirectorWorkspace.vue` 的 `<style>` 段
- **验收**：`grep -oE "[0-9]+px" frontend/src/style.css | sort -u` 的取值数从 ~25 降到 ≤10；
  全站截图对比，密度显著下降；`vue-tsc` + `build` 通过

### Module F-C（容器三级分层）
- 拆 `.surface` 系 7 容器为三级（主 border / 次纯留白 / 去 shadow）
- **验收**：主区/辅区在灰度截图下可区分；无面板"看起来一样"

### Module F-D（资产瓦片与密集区）
- F4 `.asset-tile-meta`/`.asset-wall`；F5 七步+runs 列表
- **验收**：瓦片 hover 单行不溢出；密集区留白可读

### Module F-E（强调色收敛 + 签名焦点）
- F9 强调色从 10+ 处收敛到 `flow-steps` + 主动作
- **验收**：全站强调色出现次数显著减少；七步流程条成为唯一视觉焦点

### Module F-F（其余组件打磨）
- F6/F7/F8/F10/F11/F12
- **验收**：全站 15 条路由截图无回归

---

## 7. 与记忆系统改造的关系

- 两份设计**完全独立**：本设计只动 `frontend/src`（样式与结构），`memory-system-redesign.md` 主要动 `src/kantoku`（后端）。
- **唯一交叉点**：`memory-system-redesign.md` 的 **P16** 修改 `frontend/src/components/DirectorWorkspace.vue:594`
  的错误文案（把导演阶段 fatal 显示为"图片制作失败"）。
  → 若两个改造同期进行，**P16 与本设计的 F5 都改 `DirectorWorkspace.vue`**，需串行或先做 P16（改动更小、更独立）。
- 建议顺序：**先做记忆侧 P16（1 处改动）→ 再做前端 Module F-A…F-F**，避免同文件并发修改冲突。

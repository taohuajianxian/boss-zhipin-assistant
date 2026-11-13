---
name: boss-zhipin-deliver
description: |
  BOSS直聘自动化投递技能 20261113 — 三层循环架构（关键词进化 + 推荐页串行投递）。
  依赖 Kimi WebBridge（端口 10086）控制已登录 Chrome，在推荐页懒加载全量岗位、Agent 逐条语义决策后投递。
  支持环境自检、投递断点续传、每日目标检测、HTML 汇报生成（授权状态 + 微信随喜区）。
  ★ 20261113: 修上限弹窗假失败——投递前 preflight 清障（popup_residue 零延时探测 + handle_popup 多层循环关闭），
         comm_limit 先清障再自动复投一次，连续 3 条 comm_limit 判定遮罩残留或真封顶并停止本批；
         user_profile 个人数据保护（随包只带脱敏模板、缺文件自动生成）。
  ★ 20261111: 知识/执行分离——网页结构、选择器、点击 JS 全部抽到云端 plan.json，本地只留编排；
         新增激活码 CLI、免费首日体验、24h 离线宽限、指令包云端同步；激活必须由脚本执行。
  ★ 20260911: 修复三大漏洞——(1)两级上限精确区分（120次提示仍可继续投，150次才为硬顶）；
         (2)流水线运行期严禁任何页面切换/刷新/导航，read 失败必须停止并提示用户而非自行动页；
         (3)全程固定独立标签页，绝不抢占用户当前页面。
  ★ 2026-09（具体日期未记录）: 多方向同权重支持（keywords 顺序即权重），新增当日必跑次要方向批的执行规则。
  ★ 20260810: 新增「脚本即权威」核心原则——禁止 Agent 绕过 CLI 步骤自行构造 URL 做页面导航。
metadata:
  version: "20261113"
---

# BOSS直聘自动化投递 Skill 20261113 版

> **版本号规则（20261113 起）**：版本号一律用发布日期命名，格式 `YYYYMMDD`（如 `20261113`），不再使用 `vX.Y` 语义版本号。
> 源码注释与变更记录中残留的 `vX.Y` 仅为历史溯源，不再新增；对外描述一律写日期版。

> 本技能加载后，Agent 将按三层循环架构执行 BOSS直聘自动化投递。
> 依赖 Kimi WebBridge（端口 10086）控制已登录 Chrome。
>
> **核心设计**：`user_profile.json` 是唯一用户数据源，skill 代码本身不含任何特定用户信息。
> 不同求职者只需修改 `user_profile.json` 即可复用整套投递流程。
>
> **首次使用（★ 20261113）**：`user_profile.json` 属个人数据，包内只带脱敏模板 `user_profile.example.json`；首次运行会自动生成并提示填写。
> 首次运行缺该文件时，`profile_loader` 会自动从模板复制生成，并报错提示填写 `target_city` / `city_code` / `search_keywords` / `min_salary_k` 后重跑——不会静默沿用模板里的示例城市。

---

## 核心原则

1. **执行脚本**：使用 `python_executor` 运行 `<SKILL_DIR>/scripts/` 下的 .py 文件
2. **脚本即权威（★ 20260810 新增）**：所有页面导航（推荐页、搜索页、关键词搜索跳转）**必须通过本 skill 的 CLI 步骤执行**（`--step init`、`--step navigate-search <kw>`、`--step navigate-recommend`），**严禁 Agent 自行构造 URL 后通过 WebBridge evaluate/window.location 绕过脚本**。skill 脚本中的 URL 常量是唯一真实来源，Agent 凭记忆或猜测的 URL 不可信——即使看起来合理也必须走脚本。违反本条 = 页面导航错误 = 任务失败。
3. **AI 决策**：岗位 APPROVE/REJECT 必须由 Agent 根据 user_profile.json 语义判断，禁止脚本硬编码匹配
4. **WORK_DIR**：默认使用 Marvis 当前会话的 output 目录，可通过 `BOSS_WORK_DIR` 环境变量覆盖
5. **Kimi WebBridge**：所有浏览器操作通过 `use_skill("kimi-webbridge")` 完成，禁止 CDP 直连
6. **运行期严禁动页（★ 20260911 新增）**：流水线运行期（init→next-batch→read→decide→evolve 全流程）**严禁任何页面切换、刷新、导航、切标签页**。推荐列表一旦被重排，read 就找不到已缓存的卡片。read 失败 = 立即停止并提示用户，**绝不自行刷新/切页/滚动重试**（此前曾误判为"懒加载问题"，方向错误）。确需恢复只能由用户确认后执行 `--step init` 重定位推荐页。
7. **独立标签页（★ 20260911 新增）**：全程固定在本 session 自己打开的独立 BOSS 标签页（`ensure_active_tab` 内部 find_tab 不传 active、导航用 new_tab+group_title），**绝不借用用户当前查看的页面**。用户切到别的标签不影响本流水线。
8. **激活必须走 CLI（★ 20261111 新增，最高优先级之一）**：用户把激活码发到对话后，Agent **必须执行** `python scripts/entitlement.py --activate <CODE>`，**严禁只口头回复「已激活」**——授权判定只认脚本落盘的 `license.json`。同理，网页结构 / 选择器 / 点击 JS 一律从 `plan.json` 读取（`cloud_client.render_js`），**严禁 Agent 凭记忆手写选择器或内联 JS 塞进 evaluate**。文件里的常量已全部移走，凭记忆写必然是过时的。

---

## 架构总览

```
scripts/
├── profile_loader.py      — 中央配置加载器（从 user_profile.json 读取）
├── env_check.py           — 环境自检 + 自动修复
├── webbridge_client.py    — WebBridge HTTP 客户端（端口 10086）
├── recommend_loader.py    — 推荐页/搜索页导航 + 分批懒加载
├── surface_cache.py       — 精简字段缓存（7 核心字段）
├── jd_reader.py           — 页面内原地点卡读 JD（选择器与 JS 全部从指令包注入）
├── cloud_client.py        — 云端指令包缓存 / 渲染 / 同步
├── entitlement.py         — 激活码 CLI、免费额度、离线宽限
├── cloud_config.json      — 服务地址与作者微信号（已预填，可按需覆盖）
├── deliver_engine.py      — 投递执行 + 目标检查
├── report_builder.py      — HTML 汇报生成
├── keyword_evolver.py     — AI 关键词进化 + 搜索导航
├── serial_loop.py         — 三层循环主控制器
```

信号文件（位于 WORK_DIR）：
- `page_state.json` — 页面级进度
- `batch_state.json` — 分批级进度
- `surface_cache.json` — 全量缓存
- `jd_to_read.json` — AI 筛选后待读取列表
- `current_jd.json` — 当前 JD（供 Agent 阅读）
- `decision.json` — Agent 决策结果
- `stream_progress.json` — 投递断点续传

授权状态文件（位于 `~/.boss_zhipin_deliver/`，跨会话保留，可用 `BOSS_LICENSE_DIR` 覆盖）：
- `license.json` — 激活后落盘的授权文件（带服务端签名，改一个字符就失效）
- `plan.json` — 云端同步下来的新版指令包
- `usage.json` — 当天投递份数计数（免费资格不靠它，靠服务端记账）
- `sync.json` — 最近一次成功同步时间

---

## 激活码与云端指令包（★ 20261111）

### 知识/执行分离

用户的核心资产是「知道怎么读、怎么点、页面长什么样」。这部分已全部抽到 `dist/plan.json`：
URL 模板、24 个选择器、7 段带占位符的 JS 模板、点击时序、两级上限、薪资字体映射、重要约束。

- 本地脚本只保留**编排、决策、执行管道**；`jd_reader.py` 里已无任何选择器字面量。
- 运行时由 `cloud_client.render_js(name, **selectors)` 把选择器注入 JS 模板再执行。
- 页面结构一变，新版指令包会自动下发，用户不用换包、不用改代码。

### 免费与激活

- **首次运行当天免费**，可完整投满平台一天上限（默认 150 份）。从第二天起需要激活码。
- 免费资格由 **服务端按设备指纹记录**，删掉本地 `usage.json` 或 `first_run_date` 不会重置。
- 激活码由作者在微信里人工发，**随喜、不标价**，经济困难直接免费送；免费码同样有到期日，到期可再找作者续。

### ★ 三道门禁（20261113 加固）

| 漏洞 | 以前 | 现在 |
|---|---|---|
| 自己编一张 `license.json` | 只看明文到期日，编个 2099 年就能一直用 | 服务端签名，客户端验签，手改立刻判「授权文件被改动过」 |
| 一个码拷给三台机器用 | 不比对设备，谁拷都能跑 | 码与设备指纹绑定，换机直接拦，只能找作者解绑 |
| 删 `usage.json` 无限刷免费 | 只看本地 `first_run_date` | 免费资格记在服务端 `free_devices` 表，删本地文件无效 |

联网节奏：配了 `signing_key` 的包 72 小时核验一次；没配的包每次 `--step init` 核验一次（更严）。断网时按本地授权继续跑，连续 7 天连不上才拦；授权到期另有 24 小时宽限。

### ★ 激活流程（硬性，Agent 必读）

1. 用户把激活码发到对话里，例如「我的激活码是 ABCD-1234-EFGH」。
2. Agent **必须执行 CLI**，不得只在对话里回一句「已激活」：
   ```
   python scripts/entitlement.py --activate ABCD-1234-EFGH
   ```
3. 脚本带匿名设备指纹（只传哈希）完成校验 → 落盘 `license.json` → 拉取新版指令包 → 本地缓存。
4. 回显示例：`激活成功 | 随喜码 | 到期 2026-12-11 | 剩余 30 天 | 指令包 20261111-cbe538cd`。
5. 之后每天直接跑，不用再输码；联网核验按上面的节奏自动做，断网也能跑。查看状态：`--status`；手动同步指令包：`python scripts/cloud_client.py --sync`。

### 门禁边界（不破坏运行期禁动页）

- 授权检查**只在 `--step init` 这一步判一次**；`next-batch → read → decide → evolve` 全程绝不弹窗、不打断、不联网阻塞。
- 已激活用户的授权文件已落盘，服务端临时不可用时照样跑本地缓存的指令包；授权到期另有 24h 宽限。
- `--step finish` 生成汇报时展示授权状态与联系方式，不中断任何投递动作。

### 可选：自定义服务配置

`scripts/cloud_config.json` 已预填服务地址与作者微信，开箱即可激活，无需改动。如需自定义，可在同目录新建 **`cloud_config.local.json`**（本地文件优先，同名字段覆盖预填值）：
```json
{
  "endpoint": "https://your-domain.example.com",
  "wechat_id": "对外展示的微信号"
}
```
两项都留空也能跑：使用随包附带的基础版指令包，汇报页不显示微信号。
也可用环境变量 `BOSS_LICENSE_ENDPOINT` 覆盖 `endpoint`。

---

## 执行流程

### 步骤 0：确定 WORK_DIR

使用 Marvis 当前会话的 output 目录，或设置环境变量：
```
BOSS_WORK_DIR = <当前会话 output 目录>
```

### 步骤 1：初始化（`--step init`）

```
python_executor: 运行 <SKILL_DIR>/scripts/serial_loop.py --step init
```

脚本先做一次授权门禁检查（免费首日 / 已激活均放行），再检查 WebBridge、导航到 BOSS 推荐页、重置状态文件。
若门禁未放行，init 直接停止并打印联系方式；Agent 不得绕过 init 自行导航或改状态文件来「跳过」。

> ⚠️ **URL 陷阱（必须遵守）**：`/web/geek/recommend` 是**已沟通/已投递历史页**，不是推荐页。页面标题为「BOSS直聘」且含「已投递」「沟通过」字样时即为错误页面。正确的推荐/职位搜索页 URL 为 **`https://www.zhipin.com/web/geek/job`**（实际跳转后为 `/web/geek/jobs`，标题为「{城市名}招聘」）。Agent 导航后必须 snapshot 验页：标题不含目标城市名 = 错页，立即修正。

**失败处理**：若 WebBridge 不可达，先调用 `use_skill("kimi-webbridge")` 确保 daemon 运行。

### 步骤 1.5：确认投递方向

> ⚠️ **必须执行**：本 skill 面向所有用户发布，Agent **严禁**跳过此步骤直接开始投递。

Agent 必须读取 user_profile.json，向用户展示下列关键筛选规则并等待用户确认后，才能进入步骤 2：

- **用户姓名**：`user_name`
- **目标城市**：`target_city`（编码: `city_code`）
- **最低薪资**：`min_salary_k`（单位：K，0 表示不限制）
- **日投递目标**：`daily_target`（份）
- **目标领域描述**：`domain_description`（若未填则展示 prefer/avoid 列表）
- **偏好岗位**：`prefer` 列表
- **排除岗位**：`avoid` 列表
- **额外规则**：是否排除大厂（`prefer_no_big_company`）、是否排除猎头（`prefer_no_headhunter`）
- **搜索关键词**：`search_keywords` 列表（前 10 个）

展示后询问用户：是使用这些默认规则，还是需要调整？用户确认后进入步骤 2。

### 步骤 2：中层循环 — 滚动加载（`--step next-batch`）

```
python_executor: 运行 <SKILL_DIR>/scripts/serial_loop.py --step next-batch
```

加载一批约 50 条岗位，追加到 `surface_cache.json`。输出岗位列表供 Agent 初筛。

**Agent 初筛**：Agent 读取输出中的岗位列表，基于 `user_profile.json` 的筛选规则（prefer/avoid/search_keywords/domain_description），将合资格岗位追加写入 `jd_to_read.json`（JSON 数组，每个元素含 page_order、jobId、title 等）。

### 步骤 3：内层循环 — 逐条读 JD + 决策 + 投递

对 `jd_to_read.json` 中每条岗位（按 page_order 顺序）：

**3a. 读取 JD**：
```
python_executor: 运行 <SKILL_DIR>/scripts/serial_loop.py --step read --index N
```
脚本将 JD 写入 `current_jd.json`。

**3b. Agent 决策**：
Agent 读取 `current_jd.json`，基于 `user_profile.json` 的筛选规则判断 APPROVE/REJECT：

- **APPROVE**：岗位匹配 prefer 列表中的目标方向，薪资 ≥ min_salary_k（若设置了最低薪资），城市匹配，不属于 avoid 列表
- **REJECT**：岗位属于 avoid 列表、domain_description 中声明的排除领域、猎头/外包（若 prefer_no_headhunter=true）、大厂（若 prefer_no_big_company=true）、或机械预筛排除项（保安/保洁/销售/主播/实习生等低相关度岗位）
- 不确定时宁可 REJECT

### ★ 硬性规则（2026-11-04 用户明确要求，优先级高于上述通用判断）
1. **保险业完全不投**：标题、公司名、行业标签任一含「保险/寿险/财险/保险经纪/保险代理/续保/车险」等，直接 REJECT，不论薪资与方向是否看似匹配。
2. **兼职岗位（时薪/日薪/按次/日结/标题含兼职）可投，但门槛远高于全职**，须同时满足三项，缺一即 REJECT：
   - 方向高度贴切：命中 `user_profile.json` 的 `core_prefer` 核心方向词（用户自定义），仅命中 `generic_prefer` 泛词不放行；
   - 公司成立时间较久：成立年限 ≥ 5 年，信息缺失视为不达标；
   - 公司背景可靠：名称含集团/股份/国企/控股，或已上市/有融资轮次/百人以上规模背书。
3. 月薪解析只认 `K`、`元/月`、`万/月`；`元/时`、`元/天`、`元/次` 一律归为兼职处理，不得套用月薪下限放行。
4. **匿名公司名（猎头代招）一律不投**（2026-11-09 用户明确要求）：公司名以「某」开头，或含「某大型/某知名/某中型/某小型/某上市/某公司/某集团/知名XX公司」等模糊指代（如「某大型知名互联网公司」「某大型日化公司」「某知名游戏公司」），均属猎头/代招发布的匿名岗位，直接 REJECT，不看薪资与方向。
   - 判定正则：`^(某|知名)|某(大型|知名|中型|小型|上市|公司|集团|企业)`
   - 理由：匿名岗位无法核实雇主真实背景与岗位真假，投递后多为简历倒卖或长期挂单，无效沟通。
5. **合作/合伙/自带人脉类一律不投**（2026-11-11 用户明确要求）：标题或 JD 含「合伙人/城市合伙人/事业合伙人/联合创始/股东/资源入股/自带资源/自带人脉/自带客户/人脉变现/事业伙伴/众筹/共创/合作」等，直接 REJECT，不看薪资与方向。
   - 理由：此类岗位本质是资源置换或自负盈亏的分成合作，不是雇佣关系，无底薪保障、不缴社保，与求职诉求不符。
   - 本节判定由 Agent 逐条执行，不依赖任何本地关键词脚本；判为 REJECT 时，须在 `decision.json` 的 `reason` 里写明命中了哪一条。

Agent 将决策写入 `decision.json`。**必须使用 shell_executor 执行 Python 一行命令覆写**，禁止使用 delete+write_file 组合（会触发确认弹窗打断循环）：

```
shell_executor: python -c "import json; json.dump({'decision':'APPROVE','reason':'...'}, open(r'WORK_DIR\decision.json','w',encoding='utf-8'), ensure_ascii=False)"
```

原因：`delete` 工具每次弹出确认卡片，`write_file` 同名文件自动重命名需要先删旧——两者组合导致每轮循环至少 1 次确认弹窗。Python `open(w)` 直接覆写，零弹窗。注意命令中路径需用正斜杠或 raw string，JSON 内双引号需转义。

**3c. 执行决策**：
```
python_executor: 运行 <SKILL_DIR>/scripts/serial_loop.py --step decide --index N
```
APPROVE 时自动投递并检查目标；脚本内置投递目标检查（DAILY_TARGET）。

**3d. 检查终止条件**：
- 投递目标达标 → **强制跳到步骤 5**
- 致命错误（每日上限）→ **强制跳到步骤 5**
- 本批处理完 → 继续步骤 2 下一批

### 步骤 4：外层循环 — 关键词进化（`--step evolve`）

页面穷尽后：
```
python_executor: 运行 <SKILL_DIR>/scripts/serial_loop.py --step evolve
```

脚本评估投递结果，获取下一个搜索关键词。Agent 确认关键词后：
```
python_executor: 运行 <SKILL_DIR>/scripts/serial_loop.py --step navigate-search --keyword "关键词"
```

回到步骤 2 继续。

**关键词穷尽（无新关键词）→ 强制跳到步骤 5**。不允许在没有新关键词时继续卡在步骤 4。

#### ★ 方向权重执行规则（2026年9月（日期未记录） 新增，必读）

- `user_profile.json` 的 `search_keywords` **顺序即权重**。若 `direction_priority` 中声明了多个同等权重方向，则这些方向的关键词应在 `search_keywords` 中**紧邻排列、同批覆盖**，不得让某一方向因排序靠后而长期轮不到执行。
- **当日开跑必须覆盖各同权重方向**：当日前 4 个执行的关键词中，至少 2 个属第二同权重方向（示例：`--step navigate-search --keyword "<该方向关键词>"`）。若 `--step evolve` 返回的首个关键词未覆盖，Agent 需主动改为执行该方向关键词，之后再回到列表顺序。
- **达标前的兜底下限**：在当日投递目标达标、准备进入步骤 5 之前，每个同权重方向必须至少完整跑完 2 个关键词；若因主方向批提前达标导致次要方向未跑，不得直接进入步骤 5，须补跑至少 1 个次要方向关键词并完成其 APPROVE 岗位投递后再生成汇报。
- **薪资口径**：以 `user_profile.json` 的 `min_salary_k` 为准，按 parsed 区间的**下限**判定（下限 < min_salary_k 即拒，上限不用于放行）；`min_salary_k = 0` 表示不限制。面议等无法解析出下限的薪资按不达标处理。兼职/日结/时薪不套用，走 `part_time_policy` 三项门槛。
- 方向语义判定口径（仅当 `prefer` 含该方向时适用）：以业务/运营/策划/内容/流程提效为主体的岗位 → APPROVE；若 `avoid` 或 `black_title` 已声明排除研发类岗位，则岗位本质为写代码/做系统/算法/前后端/全栈/测试/运维/大模型开发 → REJECT。

### 步骤 5：生成汇报 — ★ 强制执行（`--step finish`）

> **拦截性强制步骤。以下任意条件触发时必须立即执行，不得跳过：**
> - 投递目标达标
> - 致命错误（每日上限）
> - 所有页面穷尽且无新关键词
> - 用户主动要求停止投递

```
python_executor: 运行 <SKILL_DIR>/scripts/serial_loop.py --step finish
```

生成 `投递汇总.html` 到 WORK_DIR，内含授权状态、作者微信号、随喜文案与「一键复制激活指令」按钮。

> 汇报页是**纯静态文件**，页面上的输入框只负责帮用户拼一句话，**不会真的激活**。
> 真正的激活必须由 Agent 执行 `python scripts/entitlement.py --activate <CODE>`。

---

## 随时查看进度

```
python_executor: 运行 <SKILL_DIR>/scripts/serial_loop.py --step status
```

---

## 环境检查

```
python_executor: 运行 <SKILL_DIR>/scripts/env_check.py
```
包括账户匹配检测（user_profile.json 的用户名/城市 vs 实际登录账户）。

---

## 配置说明

### user_profile.json 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_name` | string | 否 | 用户姓名，用于账户匹配和报告 |
| `role_summary` | string | 否 | 一句话角色概述 |
| `experience` | string | 否 | 详细经验描述，用于 LLM 决策 |
| `domain_description` | string | 推荐 | **目标领域详细描述**（适合/不适合的岗位范围），会注入 LLM 审核 prompt。不填则自动由 prefer/avoid 拼接 |
| `target_city` | string | 是 | 目标城市名（如"北京"） |
| `city_code` | string | 是 | 城市编码（如"101010100"），从 BOSS直聘 URL 获取 |
| `min_salary_k` | number | 否 | 最低薪资（K），0 表示不限制 |
| `daily_target` | number | 是 | 每日投递目标（份） |
| `prefer` | string[] | 否 | 偏好岗位关键词列表 |
| `avoid` | string[] | 否 | 排除岗位关键词列表 |
| `prefer_no_big_company` | boolean | 否 | 是否排除大厂（已上市/2000人以上），默认 false |
| `prefer_no_headhunter` | boolean | 否 | 是否排除猎头岗位，默认 false |
| `search_keywords` | string[] | 是 | BOSS直聘搜索关键词列表（纯职位词，不含城市） |
| `c_exclude` | string[] | 否 | 自定义公司名黑名单。不填则使用内置通用默认值（排除猎头/外包/房产/保险） |
| `t_exclude` | string[] | 否 | 自定义标题排除词。不填则使用内置通用默认值（排除保安/保洁/销售/主播/实习生等） |

### 城市编码获取方式

1. 在浏览器中打开 https://www.zhipin.com/
2. 顶部选择目标城市
3. 查看 URL：`https://www.zhipin.com/web/geek/job?city=XXXXXX` → `city=` 后的数字即为 city_code

---

## 重要约束

1. **URL 红线**：`/web/geek/recommend` 是已投递历史页，禁止作为岗位来源。推荐页唯一正确 URL 是 `https://www.zhipin.com/web/geek/job`。导航后必须 snapshot 确认标题含目标城市名。
2. 绝不 navigate 到 job_detail URL（在原页面点卡读 JD）
3. 绝不点击「继续沟通」（投递后点「留在此页」）
4. 投递后必须验证实际结果
5. **两级上限 + 遮罩假失败（★ 20261113 修正）**：BOSS 沟通上限分两级——**120 次提示（仍可继续投）**与 **150 次硬顶（不可再投）**。
   - 120 次触发 comm_limit **不是封顶**：脚本会先关掉叠加弹窗、清障后**自动复投一次**，复投成功即正常计入；只有 150 次触发 fatal_limit 才终止并 finish。**严禁把 120 当硬顶提前停，也严禁 Agent 侧手动改判断逻辑或手动补投。**
   - 上限提示会叠加 2~6 层遮罩，遮罩挡住卡片点击时会表现为"点了没反应"，历史上被误记为大量 comm_limit / 失败。脚本已含投递前 preflight 清障（`popup_residue` 探测 + `handle_popup` 多层循环关闭）。
   - **连续 3 条 comm_limit（清障 + 复投均无效）= 页面被遮挡或平台真封顶**，脚本自动停止本批。此时 Agent 必须停下来向用户说明"疑似遮罩残留或已达上限"，由用户确认页面状态后再 `--step init` 续投，**禁止自行刷新页面、禁止绕过脚本硬投**。
6. **运行期禁动页（★ 20260911 强制）**：init→next-batch→read→decide→evolve 全流程**严禁任何页面切换/刷新/导航/切标签**。read 失败 = 立即停止并提示用户，严禁自行刷新、切页、滚动重试。恢复仅能经用户确认后 `--step init`。
7. 所有浏览器操作必须通过 Kimi WebBridge，不可 CDP 直连
8. **强制 finish**：投递流程无论以何种方式终止，都必须执行 `--step finish` 生成投递汇总.html。禁止跳过此步骤。
9. **激活与知识来源（★ 20261111 强制）**：激活只能由 `entitlement.py --activate` 完成，Agent 口头确认无效；所有选择器 / 点击 JS 只能来自 `plan.json`，禁止 Agent 手写或凭记忆补常量；授权门禁只在 `--step init` 判一次，运行期不得弹出任何激活提示、不得中断循环。

---

## 版本历史

| 版本（发布日期） | 变更 |
| --- | --- |
| 20261113 | 授权与稳定性加固：授权文件防篡改、绑定设备；免费体验资格由服务端记录；增加定期核验与离线保护 |
| 20261113 | 修复沟通上限弹窗造成的误判：先清除遮罩再自动复投一次，连续失败自动停批 |
| 20261111 | 知识 / 执行分离：网页结构与点击逻辑改由云端指令包下发；新增激活码、免费首日体验、离线宽限与 HTML 汇报页 |
| 20260911 | 修正两级沟通上限判定、运行期禁动页规则与独立标签页流水线 |
| 20260810 | 新增「脚本即权威」原则：所有页面导航必须经脚本执行 |
| 20260805 | 修正写入流程，消除循环确认弹窗 |
| 20260804 | 补齐各退出路径的收尾与汇报生成 |
| 20260804 | 通用发布版：去除硬编码用户画像，个人数据集中在 `user_profile.json` |
| 20260804 | 多 Agent 环境适配 |

# 执行手册（Agent 必读）

> 本文件是 `SKILL.md` 的执行细则。凡是运行本 skill，**必须先完整读一遍本文件**，
> 再按下面步骤执行。`SKILL.md` 只讲「是什么、怎么装、怎么配」，执行口径一律以本文件为准。

---

## 一、运行前提

| 依赖 | 要求 |
|---|---|
| Kimi WebBridge | 端口 10086，daemon 需运行；所有浏览器操作走 `use_skill("kimi-webbridge")`，禁止 CDP 直连 |
| Chrome | 需已登录 BOSS直聘（本人账号） |
| Python | 3.9+，依赖见 `requirements.txt` |
| 工作目录 | 默认用当前会话 output 目录，可用环境变量 `BOSS_WORK_DIR` 覆盖 |

首次使用先跑环境自检：

```
python <SKILL_DIR>/scripts/env_check.py
```

自检含账户匹配检查（`user_profile.json` 里的姓名/城市 vs 实际登录账号）。

---

## 二、核心原则

1. **执行脚本**：一律用 `python_executor` 运行 `<SKILL_DIR>/scripts/` 下的 .py 文件。
2. **脚本即权威**：所有页面导航必须经本 skill 的 CLI 步骤执行（`--step init`、`--step navigate-search <kw>`），**严禁 Agent 自行构造 URL 后用 `window.location` 或 evaluate 绕过**。URL 常量只以脚本为准，凭记忆或猜测的 URL 一律不可信。
3. **AI 决策**：岗位 APPROVE / REJECT 必须由 Agent 依据 `user_profile.json` 语义判断，不得硬编码匹配。
4. **知识来源只认云端适配数据**：页面选择器、点击动作一律取自云端同步的适配数据（本机缓存），不要手写选择器或临时拼接脚本塞进 evaluate。随包不带适配数据，首次运行先联网同步。
5. **运行期严禁动页**：`init → next-batch → read → decide → evolve` 全流程严禁页面切换、刷新、导航、切标签页。推荐列表一旦被重排，`read` 就找不到已缓存卡片。read 失败 = 立即停止并提示用户，**绝不自行刷新/切页/滚动重试**；恢复只能经用户确认后重跑 `--step init`。
6. **独立标签页 + 落点校验**：全程固定在本会话自己打开的 BOSS 标签页，绝不借用用户当前查看的页面。首次进入（`--step init`）与换关键词（`--step navigate-search`）后，脚本会清理多余的 BOSS 标签页并把当前页（连同 `session`）登记为目标页，认页时校验 `session` 是否与本轮一致（不一致视为无目标页，重新登记，不会被别的进程/安装副本写脏的台账带偏）；此后每次投递前都以「目标页 URL + 活动页 URL」比对，落点不符**直接停机**（`wrong_landing`），不会将就着在错页上投。标签页管理是脚本内部行为，Agent 侧仍禁止手动动页。
7. **激活必须走 CLI**：用户把激活码发到对话后，必须执行 `python scripts/entitlement.py --activate <CODE>`，**严禁只口头回复「已激活」**；授权判定只认脚本落盘的 `license.json`。

---

## 三、CLI 步骤总览

| 步骤 | 命令 | 作用 |
|---|---|---|
| init | `serial_loop.py --step init` | 授权门禁检查、检查 WebBridge、导航推荐页、重置状态 |
| next-batch | `serial_loop.py --step next-batch` | 滚动加载一批约 50 条岗位，追加到缓存 |
| build-queue | `serial_loop.py --step build-queue` | ·生成与当前页面等长同序的 `jd_to_read.json`（规则层自动初筛 + 索引对齐） |
| read | `serial_loop.py --step read --index N` | 读取第 N 条 JD 到 `current_jd.json` |
| decide | `serial_loop.py --step decide --index N` | 执行上一步决策：投递并检查目标 |
| evolve | `serial_loop.py --step evolve` | 评估结果，取下一个搜索关键词 |
| navigate-search | `serial_loop.py --step navigate-search --keyword "词"` | 按关键词搜索导航 |
| finish | `serial_loop.py --step finish` | 生成投递汇总 HTML（强制） |
| status | `serial_loop.py --step status` | 查看当前进度 |
| reset-failed | `serial_loop.py --step reset-failed [--include-terminal]` | 归档并清除失败记录（默认只清可重试失败），让这些岗位重新进候选；**先归档、归档写成功才动台账**，归档失败则一条不清并返回失败（重跑即可收敛）；成功记录不动 |
| ack-risk | `serial_loop.py --step init --ack-risk` | 人工确认风控页/限流已解除，清除停机标记后重新 init |

状态文件（位于 WORK_DIR）：`page_state.json`、`batch_state.json`、`surface_cache.json`、`jd_to_read.json`、`current_jd.json`、`decision.json`、`stream_progress.json`。

授权状态目录默认 `~/.boss_zhipin_deliver/`（可用 `BOSS_LICENSE_DIR` 覆盖）：`license.json`、`plan.json`、`usage.json`、`sync.json`。

---

## 四、执行流程

### 步骤 0：确定 WORK_DIR

使用当前会话 output 目录，或设置 `BOSS_WORK_DIR`。

### 步骤 1：初始化

```
python <SKILL_DIR>/scripts/serial_loop.py --step init
```

脚本先做授权门禁检查（免费首日或已激活均放行），再检查 WebBridge、导航推荐页、重置状态文件。
门禁未放行时 init 会停止并打印联系方式，**Agent 不得绕过 init 自行导航或改状态文件来跳过**。

> **URL 陷阱（必须遵守）**：`/web/geek/recommend` 是已沟通/已投递历史页，不是推荐页。页面标题为「BOSS直聘」且含「已投递」「沟通过」字样即为错页。正确的职位搜索页是 `https://www.zhipin.com/web/geek/job`（实际跳转后为 `/web/geek/jobs`，标题为「{城市名}招聘」）。导航后必须 snapshot 验页：标题不含目标城市名 = 错页，立即修正。

WebBridge 不可达时，先 `use_skill("kimi-webbridge")` 确保 daemon 运行。

### 步骤 1.5：确认投递方向（必须执行）

读取 `user_profile.json`，向用户展示以下筛选规则并等待确认，才能进入步骤 2：

- 用户姓名 `user_name`
- 目标城市 `target_city`（编码 `city_code`）
- 最低薪资 `min_salary_k`（单位 K，0 表示不限）
- 日投递目标 `daily_target`
- 目标领域描述 `domain_description`（未填则展示 prefer/avoid）
- 偏好岗位 `prefer` / 排除岗位 `avoid`
- 是否排除大厂 `prefer_no_big_company`、是否排除猎头 `prefer_no_headhunter`
- 搜索关键词 `search_keywords`（前 10 个）

展示后询问：用默认规则，还是需要调整？确认后进入步骤 2。

### 步骤 2：中层循环 — 滚动加载 + 队列生成

**2a. 滚动加载**

```
python <SKILL_DIR>/scripts/serial_loop.py --step next-batch
```

加载一批岗位追加到 `surface_cache.json`，**同时重建当前页面窗口 `page_window.json`**（取尾部 `total_cards` 条 = 页面真实卡片顺序）。输出末尾会提示下一步运行 build-queue。

**2b. 生成对齐队列（强制）**

```
python <SKILL_DIR>/scripts/serial_loop.py --step build-queue
```

生成与页面**等长同序**的 `jd_to_read.json`：index N 严格等于页面第 N 张卡片，`page_order` 取自页面窗口。规则层（`scripts/job_filter.py`）自动初筛，输出三态：

- **PASS**：命中画像核心方向（住宿/文旅/OTA/民宿/景区/电商/AI 工具等），优先 read → approve；
- **REVIEW**：规则无法断言（薪资下限低但上限达标、泛方向岗位、面议薪资、与画像无显性交集），交语义层判断；
- **REJECT**：硬排除并标记 `skip=true` 占位（餐饮/网咖/棋牌/助理/实习、行业黑名单、中介/匿名/培训机构、低价值岗、薪资上限低于门槛），read 时自动跳过、不消耗读取动作。

**硬性约束**：

- 禁止手工编写 `jd_to_read.json`；
- 禁止在逐条 read 期间再调 next-batch —— 一旦页面滚动，索引与卡片的对应关系立即失效，会出现"读到的 JD 与队列标题不符"；
- 队列穷尽后才进入步骤 4 evolve。

### 步骤 3：内层循环 — 逐条读 JD + 决策 + 投递

按 `page_order` 顺序，对 `jd_to_read.json` 中每条岗位：

**3a. 读取 JD**

```
python <SKILL_DIR>/scripts/serial_loop.py --step read --index N
```

结果写入 `current_jd.json`。

read 阶段有两类**不读 JD 的自动跳过**（均直接写 `decision.json=REJECT`，索引占位保持不变）：

- **规则排除**：该条 `skip=true`；
- **重复投递拦截**：该 `jobId` 已在累计台账 `delivery_unified.json` 中**投递成功**，或属于**终止态失败**（重复投递拦截、今日沟通上限、薪资/画像不符、职位本身已关闭/已下线/停止招聘等「再投也一样」的结果）——跨会话去重，防止按钮仍显示"立即沟通"导致重复投递。**临时性失败不算拦截**（卡片定位不到、落点错位、遮罩不可点、结果识别不出，以及「标签页已关闭 / 会话已关闭 / WebBridge 连接已关闭」这类技术性关闭）：这些岗位下次仍在候选里，会自动重读重投；不想等下次可先跑 `--step reset-failed`。

**3b. Agent 决策** — 输出 APPROVE / REJECT：

- **APPROVE**：命中 `prefer` 方向、薪资**上限** ≥ `min_salary_k`（若设了；区间如 7-9K 下限低但上限达标属可谈，不应一刀切拒绝）、城市匹配、不在 `avoid` 内
- **REJECT**：命中 `avoid`、`domain_description` 排除领域、猎头/外包（若 `prefer_no_headhunter`）、大厂（若 `prefer_no_big_company`）、或低相关岗位
- **REJECT（城市兜底，2026-11-18 新增）**：标题或 JD 写明非 `target_city` 的工作地 —— 形如「（宁波上班）」「驻上海」「工作地点：温州」「能接受温州工作」「长期派驻外地」—— 一律 REJECT，reason 标"异地工作地点"。规则层只能识别标题里的异地信号，JD 正文里的异地要求由语义层负责。仅 `target_city` 与境外驻外（新加坡/香港/澳门/日本）放行。
- **REJECT（职能岗兜底，2026-11-18 新增）**：岗位职能本身是剪辑/拍剪/后期/摄像/摄影/美工/平面设计/视觉设计/AI训练师/数据标注时，即便 JD 出现"信息流/千川/投放素材"等方向词也一律 REJECT —— 方向词出现在 JD 里不代表这是方向岗。
- 不确定时宁可 REJECT

决策写入 `decision.json`，**必须用 shell_executor 一行命令覆写**，禁止 delete + write_file 组合（每轮会弹确认框打断循环）：

```
python -c "import json; json.dump({'decision':'APPROVE','reason':'...'}, open(r'WORK_DIR/decision.json','w',encoding='utf-8'), ensure_ascii=False)"
```

**3c. 执行决策**

```
python <SKILL_DIR>/scripts/serial_loop.py --step decide --index N
```

APPROVE 时自动投递并检查目标（DAILY_TARGET）。投递后必须验证实际结果。

**3d. 终止条件**

- 投递目标达标 → 强制跳步骤 5
- 致命错误（每日上限）→ 强制跳步骤 5
- 本批处理完 → 回步骤 2 取下一批

### 步骤 4：外层循环 — 关键词进化

```
python <SKILL_DIR>/scripts/serial_loop.py --step evolve
```

取到新关键词后：

```
python <SKILL_DIR>/scripts/serial_loop.py --step navigate-search --keyword "关键词"
```

回到步骤 2。**关键词穷尽 → 强制跳步骤 5**，不允许卡在步骤 4。

### 步骤 5：生成汇报（强制，不可跳过）

```
python <SKILL_DIR>/scripts/serial_loop.py --step finish
```

下列任一条件触发时都必须立即执行：投递目标达标、致命错误、页面穷尽且无新关键词、用户要求停止。

生成 `投递汇总.html` 到 WORK_DIR。汇报页是纯静态文件，页面输入框只帮用户拼一句话，**不会真的激活**；真激活必须由 Agent 执行 `entitlement.py --activate`。

---

## 五、决策硬性规则（优先级高于通用判断）

1. **保险业完全不投**：标题、公司名、行业标签任一含「保险/寿险/财险/保险经纪/保险代理/续保/车险」→ 直接 REJECT，不看薪资与方向。
2. **兼职岗位可投但门槛远高于全职**，须同时满足三项，缺一即 REJECT：
   - 方向高度贴切：命中 `core_prefer` 核心方向词（仅命中 `generic_prefer` 泛词不放行）
   - 公司成立年限 ≥ 5 年（信息缺失视为不达标）
   - 公司背景可靠：名称含集团/股份/国企/控股，或已上市/有融资轮次/百人以上规模
3. **薪资解析**只认 `K`、`元/月`、`万/月`；`元/时`、`元/天`、`元/次` 一律按兼职处理，不套用月薪下限。
4. **匿名公司名（猎头代招）一律不投**：公司名以「某」开头，或含「某大型/某知名/某中型/某小型/某上市/某公司/某集团/知名XX公司」等模糊指代 → 直接 REJECT。
   - 判定正则：`^(某|知名)|某(大型|知名|中型|小型|上市|公司|集团|企业)`
   - 理由：匿名岗位无法核实雇主与岗位真假，投递后多为简历倒卖或长期挂单。
5. **合作/合伙/自带人脉类一律不投**：标题或 JD 含「合伙人/城市合伙人/事业合伙人/联合创始/股东/资源入股/自带资源/自带人脉/自带客户/人脉变现/事业伙伴/众筹/共创/合作」→ 直接 REJECT。
   - 理由：本质是资源置换或自负盈亏的分成合作，非雇佣关系，无底薪、不缴社保。
   - 判为 REJECT 时须在 `decision.json` 的 `reason` 写明命中哪一条。

### 方向权重规则

- `search_keywords` **顺序即权重**。`direction_priority` 中声明为同等权重的多个方向，其关键词应在 `search_keywords` 中紧邻排列、同批覆盖，不允许某方向因排序靠后长期轮不到。
- **当日开跑必须覆盖各同权重方向**：当日头 4 个执行的关键词中，至少 2 个属第二同权重方向；`--step evolve` 返回的首个关键词未覆盖时，Agent 需主动改跑该方向关键词，之后再回到列表顺序。
- **达标前的兜底下限**：进入步骤 5 之前，每个同权重方向至少完整跑完 2 个关键词；若主方向先达标导致次要方向未跑，须补跑至少 1 个次要方向关键词并完成其 APPROVE 投递后再生成汇报。
- **薪资口径（v5.8 修订，以此为唯一真源）**：以 `min_salary_k` 为准，按解析区间**上限（max_basis）**判定——上限 ≥ `min_salary_k` 即通过，下限低但上限达标属"可谈"，不得一刀切拒绝；仅当区间上限 < `min_salary_k `才拒。真值定义写在 `scripts/rules.json` 的 `salary.policy = max_basis`，代码、提示词、本文档三处必须一致，由 `tools/check_consistency.py` 强制校验。`min_salary_k = 0` 表示不限；面议等无法解析区间的按"薪资待取证"交语义层，不下结论；兼职/日结/时薪走上面第 2 条三项门槛。
  - 历史坑：v5.7 之前本文档的旧表述只看区间底部、把达标区间一律否掉，与代码 `max_basis`、与本手册 APPROVE 条件（上限 ≥ 门槛）三者互相矛盾，是被买家质疑"判定时好时坏"的直接原因之一。v5.8 已统一为 max_basis 并由检查脚本守住。
- **方向语义判定**：以业务/运营/策划/内容/流程提效为主体的岗位 → APPROVE；若 `avoid` 或 `black_title` 已排除研发类，则岗位本质为写代码/做系统/算法/前后端/全栈/测试/运维/大模型开发 → REJECT。

---

## 六、重要约束（红线）

1. **URL 红线**：`/web/geek/recommend` 是已投递历史页，禁止作岗位来源；唯一正确的职位搜索页是 `https://www.zhipin.com/web/geek/job`，导航后必须 snapshot 确认标题含目标城市名。
2. 绝不 navigate 到 job_detail URL（在原页面点卡读 JD）。
3. 绝不点击「继续沟通」（投递后点「留在此页」）。
4. 投递后必须验证实际结果。
5. **两级上限与遮罩**：BOSS 沟通上限分两级——**120 次提示（仍可继续投）**与 **150 次硬顶（不可再投）**。
   - 120 次触发 comm_limit **不是封顶**：脚本会先关掉叠加弹窗、清障后**自动复投一次**，复投成功即正常计入；只有 150 次触发 fatal_limit 才终止并 finish。**严禁把 120 当硬顶提前停，也严禁手动改判断逻辑或手动补投。**
   - 上限提示会叠加 2~6 层遮罩，挡住卡片点击时表现为「点了没反应」，历史上被误记为大量失败。脚本已含投递前 preflight 清障。
   - **连续 3 条 comm_limit（清障 + 复投均无效）= 页面被遮挡或平台真封顶**，脚本自动停批。此时必须停下向用户说明「疑似遮罩残留或已达上限」，由用户确认页面状态后再 `--step init` 续投；**禁止自行刷新、禁止绕过脚本硬投**。
6. **运行期禁动页**：见核心原则第 5 条。
7. 所有浏览器操作必须走 Kimi WebBridge，不可 CDP 直连。
8. **强制 finish**：无论以何种方式终止，都必须执行 `--step finish` 生成投递汇总.html。
9. **激活与知识来源**：激活只认 `entitlement.py --activate`；选择器与点击 JS 只能来自 `plan.json`；授权门禁只在 `--step init` 判一次，运行期不得弹激活提示、不得中断循环。
10. **风控字样红线（P0）**：任何页面出现「访问受限 / 异常行为 / 验证码 / 人机验证 / 请完成安全验证」等字样，或跳转到验证页，脚本**立即停机**并写入 `stop_reason=risk_control`。
    - 停机后 `--step decide/read/next-batch/evolve/navigate-search` 一律以**退出码 2** 拒绝执行，**禁止任何形式的自动重试、清障复投、刷新、换关键词绕过**。
    - 只能由人工到浏览器确认账号状态、完成验证后执行 `--step init --ack-risk` 清除标记再续投。
    - `jd_reader.py` 命中风控时以**退出码 3** 终止并在 stderr/stdout 打印处置指引，同样视为停机信号。
    - 停机原因码全集：`risk_control` / `auth_fail` / `daily_limit` / `comm_limit` / `selector_broken` / `consecutive_unknown` / `target_reached`，落库字段为 `stop_reason` / `stop_label` / `stop_detail` / `stop_manual` / `stop_at`，`--step status` 与汇总页均会展示。

---

## 七、故障处理

| 现象 | 处理 |
|---|---|
| WebBridge 不可达 | `use_skill("kimi-webbridge")` 启动 daemon 后重试 |
| `--step read` 失败 | 立即停止并告知用户；不得刷新或切页重试；经确认后 `--step init` |
| 连续 3 条 comm_limit | 按红线第 5 条处理，停下等用户确认 |
| 页面出现风控字样（访问受限/异常行为/验证码/人机验证） | 已自动停机并落 `stop_reason=risk_control`；**禁止重试/清障/刷新**，等人工完成验证后 `--step init --ack-risk` |
| `--step decide` 返回退出码 2 并提示「流水线处于停机状态」 | 说明存在停机标记，先看 `--step status` 的原因码与建议，按提示人工处置后再 `--ack-risk` |
| `jd_reader.py` 退出码 3 | 页面已被风控拦截，不得继续读 JD 或投递，按 `risk_control` 流程处置 |
| 门禁未放行 | 按 init 打印的提示激活；不得绕过 |
| 账户不匹配（env_check 报警） | 提示用户确认 Chrome 登录账号与 `user_profile.json` 一致 |
| 状态文件 0 字节 / 报 `JSONDecodeError` | 旧版本非原子写留下的残骸；把该状态文件移出目录后重跑对应步骤。当前版本已改原子写，中断不会再损坏 |
| 长任务输出被截断后进程消失 | 禁止用 `\| Select-Object -First` / `\| head -n` 截断长任务输出（会终止 python）；改为重定向到文件再读尾部 |
| `--index N` 点开的岗位与队列第 N 条不符 | 说明队列与页面不同序：重跑 `--step next-batch` 重建窗口（以 DOM 实卡数为准），并检查 `page_window.json` 的 `total_cards` 是否等于页面卡片数 |

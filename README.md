# boss-zhipin-deliver（通用脱敏版）

BOSS直聘自动化投递技能包 20261120——三层循环架构（关键词进化 + 推荐页串行投递），由 AI Agent 逐条语义决策后投递，支持环境自检、断点续传、每日目标检测与 HTML 汇报。

**20261111 起采用「知识上云 / 执行留本地」**：网页结构、选择器、点击 JS 全部在云端下发，本地只留编排；免费首日体验与激活都需首次联网，之后由服务端同步到本机缓存。20261120 起随包不再附带明文内容。

> 本包为**脱敏通用版**：不含任何原使用者个人信息、求职方向偏好、城市、薪资门槛、运行记录。所有个性化项集中在 `user_profile.json`，改一处即可整套复用。

---

## 1. 前置依赖

| 依赖 | 说明 |
|---|---|
| Marvis（或同类支持 Skill / python_executor / shell_executor 的 Agent 环境） | 运行 Agent，加载本技能 |
| Python 3.9+ | 运行 `scripts/*.py` |
| Chrome 浏览器 + 已登录 BOSS直聘 | 投递所用浏览器，需保持登录态 |
| Kimi WebBridge（本地服务，端口 10086） | 控制已登录 Chrome 的桥接层，`scripts/webbridge_client.py` 依赖它 |

WebBridge 启动：

在 Agent 里执行 `use_skill("kimi-webbridge")` 启动本地服务；也可手动启动你已安装的 Kimi WebBridge 程序，确认本地端口 10086 可达即可。

连通性自检：

```bash
python scripts/env_check.py
```

---

## 2. 快速开始

1. 把本目录放到技能目录下（如 `<SKILL_DIR>/boss-zhipin-deliver/`）。
2. 复制 `user_profile.example.json` 为 `user_profile.json`，至少填 `target_city`、`city_code`、`search_keywords`、`prefer`、`min_salary_k`（`user_profile.json` 属个人数据，不会随包发布；首次运行若缺失会自动从模板生成并提示填写）。
3. 服务地址与联系方式**已预填**在 `scripts/cloud_config.json` 里，无需改动；留空则只能用基础版、汇报页不显示联系方式。
4. 运行环境自检：`python scripts/env_check.py`。
5. 按 `SKILL.md` 的「执行步骤」走：`--step init` → `--step evolve` → `--step navigate-search` → `--step next-batch` → `read` / `decide` 循环 → `--step finish` 出汇报。
6. 免费首日：装好即可直接跑，首次运行自动登记，当天可投满上限。
7. 拿到激活码后，**必须**执行 `python scripts/entitlement.py --activate <CODE>`（Agent 口头确认无效）；`--status` 看授权。适配数据不用手动同步：每次启动会自动补一次（已激活用户 24 小时内最多一次，失败静默沿用本地版本），只有需要立刻强制刷新时才用 `python scripts/cloud_client.py --sync`。

> 城市码可在 BOSS直聘城市页 URL 中取得（形如 `101010100` = 北京）。

---

## 3. 目录结构

```
boss-zhipin-deliver/
├── SKILL.md                     # 技能主文档（执行步骤、硬性规则、CLI 用法）
├── README.md
├── user_profile.example.json    # 用户画像空模板（首次运行自动复制生成 user_profile.json）
├── requirements.txt             # 唯一外部依赖 certifi
├── dist/
│   └── plan.json                # 云端适配数据（选择器 / JS / 时序）
└── scripts/
    ├── serial_loop.py           # 主编排 CLI（init/evolve/navigate-search/next-batch/read/decide/finish）
    ├── deliver_engine.py        # 投递执行引擎
    ├── jd_reader.py             # 岗位详情读取（选择器全部取自适配数据）
    ├── cloud_client.py          # 适配数据缓存 / 渲染 / 云端同步
    ├── entitlement.py           # 激活码 CLI、免费额度、离线宽限
    ├── cloud_config.json        # 服务地址与联系方式
    ├── surface_cache.py         # 列表卡片缓存
    ├── recommend_loader.py      # 推荐页懒加载与采集
    ├── keyword_evolver.py       # 关键词进化
    ├── profile_loader.py        # 用户画像加载 / LLM 提示词生成
    ├── report_builder.py        # HTML 汇报生成（授权状态 + 联系方式区）
    ├── qr_data.py               # 汇报页内嵌二维码数据
    ├── webbridge_client.py      # WebBridge 客户端与薪资解析
    ├── atomic_io.py             # 原子写（临时文件+fsync+os.replace），所有状态文件统一走这里
    ├── run_lock.py              # 单进程运行锁（防两个进程同时改台账互相覆盖）
    ├── stats_core.py            # 统计口径单一真源（status/HTML/目标判定共用）
    ├── job_filter.py            # 规则层初筛（薪资/地点/岗类/黑名单）
    ├── stop_codes.py            # 停机原因码
    ├── plan_guard.py            # 适配数据核对
    ├── decision_schema.py       # 决策记录结构
    ├── selfcheck.py             # 自检
    └── env_check.py             # 环境自检
```

运行期会在同目录生成 `user_profile.json`、各类 `*_progress.json` 状态文件、`logs/` 与投递汇总 HTML，均为本地数据，不影响技能本体。

---

## 4. 配置项速查（user_profile.json）

| 字段 | 类型 | 说明 |
|---|---|---|
| `target_city` / `city_code` | string | 目标城市及其城市码（必填） |
| `min_salary_k` | number | 月薪**下限**门槛（K）；`0` = 不限制 |
| `daily_target` | number | 每日投递目标份数 |
| `prefer` / `avoid` | string[] | 目标方向词 / 排除表述（AI 语义判断依据） |
| `search_keywords` | string[] | 搜索关键词，**顺序即权重** |
| `direction_priority` | string | 多方向权重说明（选填） |
| `core_prefer` | string[] | 核心方向词，兼职「方向高度贴切」判定用 |
| `generic_prefer` | string[] | 泛方向词，全职可放行、兼职不放行 |
| `black_title` / `black_industry` | string[] | 标题 / 行业黑名单（AI 判定依据，命中即不投） |
| `part_time_policy` | object | 兼职三项门槛策略 |
| `insurance_forbidden` / `anon_company_forbidden` | bool | 保险业 / 匿名公司名（猎头代招）一律不投 |
| `salary_rule` | string | 薪资判定口径说明 |
| `c_exclude` / `t_exclude` | string[] | 选填：自定义公司名 / 标题黑名单，覆盖内置默认值 |

---

## 5. 使用注意

1. **运行期严禁动页**：`init → next-batch → read → decide → evolve` 全过程禁止人为或 Agent 侧切换/刷新/导航/切标签页，否则列表重排会导致 read 找不到卡片。read 失败应停止并提示用户，不要自行刷新重试。技能自身在进入页面和每次投递前会做标签页校验与清理，属于内部行为，不需要你配合。
2. **独立标签页 + 落点校验**：流水线固定在自己打开的 BOSS 标签页内运行，不抢占用户当前页面。首次进入与换关键词后会关闭多余的 BOSS 标签页并登记目标页（台账同时记录 `session`，认页时校验，避免别的进程/别的安装副本写脏台账把本轮带到陌生页面）；投递前比对目标页与当前活动页，落点不符直接停机（`wrong_landing`），不会在错页上凑合投递。
   - **失败重试口径**：临时性失败（卡片定位不到、落点错位、遮罩不可点、结果识别不出）不锁死岗位，下次仍会重投；**标签页已关闭 / 会话已关闭 / WebBridge 连接已关闭**这类技术性「已关闭」同样按临时失败处理（26.11.26 前会被误判为终局）；只有重复投递拦截、沟通上限、薪资/画像不符、职位本身已关闭下线这类「再投也一样」的结果才算终局。想让历史临时失败立刻重来：`python scripts/serial_loop.py --step reset-failed`（先归档、归档成功才清台账，归档失败则一条不清并提示重跑）。
3. **沟通上限两级**：约 120 次触发提示（仍可继续投），约 150 次为平台硬顶（必须停）。脚本已区分，不要手动提前终止。
4. **薪资只认月薪形态**：`K` / `元/月` / `万/月`；`元/时`、`元/天`、`元/次` 归兼职，走 `part_time_policy`。
5. **激活只认脚本**：Agent 收到激活码后必须执行 `entitlement.py --activate`，口头回复「已激活」无效；门禁只在 `--step init` 判一次，运行期不打断。
6. **知识来源只认云端数据**：选择器与点击 JS 一律从 `plan.json` 取用，禁止手写；页面结构变化后新版会自动下发，用户端无需任何操作。
7. **授权文件不可手改**：`license.json` 手改或拷到别的电脑，授权会失效，需重新激活。
8. **合规自担**：自动化投递存在被平台风控的风险，使用前请自行评估；本包仅提供技术实现，不承诺投递效果与账号安全。
9. **风控即停**：页面一旦出现「访问受限 / 异常行为 / 验证码 / 人机验证」等字样或跳转验证页，脚本**立即停机**并记录 `stop_reason=risk_control`，**不重试、不清障复投、不换关键词绕过**；停机后除 `status`/`finish` 外所有步骤拒绝执行，须人工完成验证后 `python scripts/serial_loop.py --step init --ack-risk` 放行。其余停机原因码见 `SKILL.md`。
10. **中断安全**：状态文件已改原子写（临时文件 + fsync + `os.replace`），进程被杀、超时、`Ctrl+C` 都不会留下空文件或半截 JSON，直接重跑即可续投；跑长任务**不要**用 `| Select-Object -First N` / `| head -n N` 截断输出（管道提前关闭会终止进程），要限行请重定向到文件再读尾部。
11. **索引即页面卡片位**：`--index N` 点的是页面第 N 张卡。队列与页面必须等长同序，`--step next-batch` 会按当前 DOM 实卡数重建窗口；若发现点开的岗位与队列不符，重跑 `--step next-batch` 再继续（不要自行刷新页面）。
12. **同一时刻只跑一轮**：动作类步骤启动会抢 `run.lock`，已被占用则打印占用者并以退出码 2 拒绝执行（两个进程同时写台账会互相覆盖，导致已投递记录丢失、重复投递）。同一把锁也覆盖能单独运行的写台账脚本（`deliver_engine` / `jd_reader` / `report_builder` / `surface_cache` / `keyword_evolver` / `recommend_loader`），手工单跑同样会被挡。`--step status` 不加锁，跑任务时照常能查；崩溃留下的残留锁下次自动接管，不必手工清。查占用者 `python scripts/run_lock.py --info`；`--clear` 只在无人在跑时有效，持锁进程还活着会拒绝，需加 `--force` 才强拆。
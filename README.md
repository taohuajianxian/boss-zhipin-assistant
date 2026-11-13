# boss-zhipin-deliver（通用脱敏版）

BOSS直聘自动化投递技能包 20261113——三层循环架构（关键词进化 + 推荐页串行投递），由 AI Agent 逐条语义决策后投递，支持环境自检、断点续传、每日目标检测与 HTML 汇报。

**20261111 起采用「知识上云 / 执行留本地」**：网页结构、选择器、点击 JS 抽成 `dist/plan.json`，本地只留编排；免费首日体验，之后凭激活码从服务端同步新版指令包。

> 本包为**脱敏通用版**：不含任何原使用者个人信息、求职方向偏好、城市、薪资门槛、运行记录。所有个性化项集中在 `user_profile.json`，改一处即可整套复用。

---

## 1. 前置依赖

| 依赖 | 说明 |
|---|---|
| Marvis（或同类支持 Skill / python_executor / shell_executor 的 Agent 环境） | 运行 Agent，加载本技能 |
| Python 3.9+ | 运行 `scripts/*.py` |
| Chrome 浏览器 + 已登录 BOSS直聘 | 投递所用浏览器，需保持登录态 |
| Kimi WebBridge（本地服务，端口 10086） | 控制已登录 Chrome 的桥接层，`scripts/webbridge_client.py` 依赖它 |

WebBridge 启动（Windows，路径按实际安装位置）：

```powershell
Start-Process "$env:USERPROFILE\.kimi-webbridge\bin\kimi-webbridge.exe" -ArgumentList "start"
```

连通性自检：

```bash
python scripts/env_check.py
```

---

## 2. 快速开始

1. 把本目录放到技能目录下（如 `<SKILL_DIR>/boss-zhipin-deliver/`）。
2. 复制 `user_profile.example.json` 为 `user_profile.json`，至少填 `target_city`、`city_code`、`search_keywords`、`prefer`、`min_salary_k`（`user_profile.json` 属个人数据，不会随包发布；首次运行若缺失会自动从模板生成并提示填写）。
3. 服务地址与联系方式**已预填**在 `scripts/cloud_config.json` 里，无需改动；留空则只能用随包基础版指令包、汇报页不显示联系方式。
4. 运行环境自检：`python scripts/env_check.py`。
5. 按 `SKILL.md` 的「执行步骤」走：`--step init` → `--step evolve` → `--step navigate-search` → `--step next-batch` → `read` / `decide` 循环 → `--step finish` 出汇报。
6. 免费首日：装好即可直接跑，首次运行自动登记，当天可投满上限。
7. 拿到激活码后，**必须**执行 `python scripts/entitlement.py --activate <CODE>`（Agent 口头确认无效）；`--status` 看授权，`python scripts/cloud_client.py --sync` 手动同步指令包。

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
│   └── plan.json                # 云端指令包（选择器 / JS / 时序）
└── scripts/
    ├── serial_loop.py           # 主编排 CLI（init/evolve/navigate-search/next-batch/read/decide/finish）
    ├── deliver_engine.py        # 投递执行引擎
    ├── jd_reader.py             # 岗位详情读取（选择器全部从指令包注入）
    ├── cloud_client.py          # 指令包缓存 / 渲染 / 云端同步
    ├── entitlement.py           # 激活码 CLI、免费额度、离线宽限
    ├── cloud_config.json        # 服务地址与联系方式
    ├── surface_cache.py         # 列表卡片缓存
    ├── recommend_loader.py      # 推荐页懒加载与采集
    ├── keyword_evolver.py       # 关键词进化
    ├── profile_loader.py        # 用户画像加载 / LLM 提示词生成
    ├── report_builder.py        # HTML 汇报生成（授权状态 + 联系方式区）
    ├── qr_data.py               # 汇报页内嵌二维码数据
    ├── webbridge_client.py      # WebBridge 客户端与薪资解析
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

1. **运行期严禁动页**：`init → next-batch → read → decide → evolve` 全过程禁止切换/刷新/导航/切标签页，否则列表重排会导致 read 找不到卡片。read 失败应停止并提示用户，不要自行刷新重试。
2. **独立标签页**：流水线固定在自己打开的 BOSS 标签页内运行，不抢占用户当前页面。
3. **沟通上限两级**：约 120 次触发提示（仍可继续投），约 150 次为平台硬顶（必须停）。脚本已区分，不要手动提前终止。
4. **薪资只认月薪形态**：`K` / `元/月` / `万/月`；`元/时`、`元/天`、`元/次` 归兼职，走 `part_time_policy`。
5. **激活只认脚本**：Agent 收到激活码后必须执行 `entitlement.py --activate`，口头回复「已激活」无效；门禁只在 `--step init` 判一次，运行期不打断。
6. **知识来源只认指令包**：选择器与点击 JS 一律从 `plan.json` 注入，禁止手写；页面结构变化后新版指令包会自动下发，用户端无需任何操作。
7. **授权文件不可手改**：`license.json` 带服务端签名并与设备指纹绑定——手改到期日会在本地验签阶段判「授权文件被改动过」，拷到别的电脑会判「已绑定其他设备」；免费资格记在服务端，删本地 `usage.json` 不会重置免费期。
8. **合规自担**：自动化投递存在被平台风控的风险，使用前请自行评估；本包仅提供技术实现，不承诺投递效果与账号安全。

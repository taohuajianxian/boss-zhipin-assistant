---
name: boss-zhipin-deliver
slug: boss-zhipin-assistant
displayName: BOSS直聘自动化投递
version: 26.11.26-b
description: BOSS直聘自动化投递技能。在已登录的 Chrome 上接管职位搜索与推荐页，按你填写的求职方向、城市、薪资下限与岗位画像逐条读取 JD 并做语义判断，符合的自动投递并跟踪结果，结束时生成 HTML 投递汇总。浏览器操作依赖 Kimi WebBridge（端口 10086），全程固定在本会话独立标签页（按标签页 ID 绑定、读 JD 与每次投递前校验落点、多余 BOSS 标签页自动清理），不抢占你正在看的页面。
summary: BOSS直聘自动化投递：在你已登录的 Chrome 上按求职方向、城市、薪资下限与岗位画像逐条读取 JD 做语义判断，符合的自动投递并跟踪结果，结束时生成 HTML 投递汇总。全程固定独立标签页并校验落点，不抢占你正在看的页面。
metadata:
  version: "26.11.26-b"
---

# BOSS直聘自动化投递 Skill

把「刷职位、读 JD、判断合不合适、投递并留痕」这一段重复劳动交给 Agent 跑。你只需要填一份求职画像，剩下的逐条筛选与投递由 skill 驱动 Chrome 完成，结束时给你一份汇总。

## 能做什么

- 按关键词搜索职位、按页滚动加载岗位列表
- 逐条打开岗位读取 JD，按你设定的方向 / 薪资 / 排除规则判断投不投
- 自动投递已沟通并跟踪结果，命中沟通上限时自动停批
- 支持多求职方向同批覆盖，避免只跑单一方向
- 结束时生成 `投递汇总.html`，含投递明细与统计

## 环境要求

| 项 | 要求 |
|---|---|
| 浏览器 | Chrome，需已登录你自己的 BOSS直聘账号 |
| Kimi WebBridge | 本地端口 10086，需 daemon 运行（`use_skill("kimi-webbridge")`） |
| Python | 3.9+ |
| 网络 | 首次运行与定期核验需要联网；断网时按本机已有状态继续 |

## 安装

1. 解压到你的 Agent skill 目录（如 `~/.marvis/skills/custom/boss-zhipin-deliver`）
2. `pip install -r requirements.txt`
3. 复制 `user_profile.example.json` 为 `user_profile.json` 并填写（首次运行缺该文件时脚本会自动生成模板、打印必填项后以退出码 2 结束，不会再抛 Python 堆栈；填好后重跑即可）
4. Chrome 登录 BOSS直聘、启动 Kimi WebBridge，跑一次环境自检：
   ```
   python scripts/env_check.py
   ```

## 配置：user_profile.json

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `target_city` | string | 是 | 目标城市名，如「杭州」 |
| `city_code` | string | 是 | 城市编码，如 `101210100`，从 BOSS直聘 URL 的 `city=` 后获取 |
| `search_keywords` | string[] | 是 | 搜索关键词列表，顺序即权重（纯职位词，不含城市） |
| `daily_target` | number | 是 | 每日投递目标份数 |
| `min_salary_k` | number | 否 | 最低薪资（K），0 表示不限，按薪资区间下限判定 |
| `prefer` | string[] | 否 | 偏好岗位 / 方向关键词 |
| `avoid` | string[] | 否 | 排除岗位关键词 |
| `domain_description` | string | 推荐 | 目标领域详细描述，用于判断边界，比 prefer/avoid 更准 |
| `user_name` | string | 否 | 姓名，用于账户匹配与汇报 |
| `role_summary` / `experience` | string | 否 | 角色概述与经验，供判断参考 |
| `prefer_no_big_company` | boolean | 否 | 是否排除大厂（已上市 / 2000 人以上） |
| `prefer_no_headhunter` | boolean | 否 | 是否排除猎头岗位 |
| `c_exclude` / `t_exclude` | string[] | 否 | 自定义公司名 / 标题排除词，不填走内置通用默认 |

城市编码获取：打开 `https://www.zhipin.com/`，顶部切换目标城市，地址栏 `...?city=XXXXXX` 中的数字即为 `city_code`。

## 使用

装好后直接对 Agent 说一句，例如「按我的画像开始投递」，Agent 会按下面的顺序跑：

```
python scripts/serial_loop.py --step init          # 授权检查 + 打开职位页
python scripts/serial_loop.py --step next-batch    # 加载一批岗位 + 重建页面窗口
python scripts/serial_loop.py --step build-queue   # 生成与页面等长同序的队列（规则层自动初筛）
python scripts/serial_loop.py --step read --index N    # 读第 N 条 JD
python scripts/serial_loop.py --step decide --index N  # 投或不投
python scripts/serial_loop.py --step evolve        # 换关键词继续
python scripts/serial_loop.py --step finish        # 生成投递汇总.html
python scripts/serial_loop.py --step reset-failed  # 清掉可重试失败，让这些岗位重新进候选
```

查看进度：`python scripts/serial_loop.py --step status`

`status` 与 `投递汇总.html` **同一统计口径**（`scripts/stats_core.py`：以 jobId 为唯一键合并会话与历史并去重），不会出现两套数字；两源状态冲突条数会在输出中标注。

### 去重口径（26.11.26 起）

- **算完成、不再重投**：投递成功；以及**终止态失败**——重复投递拦截、今日沟通上限、薪资/画像不符、
  **职位本身**已关闭/已下线/停止招聘这类「再投也一样」的结果。
- **不算完成、下次自动重试**：临时性失败——卡片定位不到（`selector_broken`）、落点错位（`wrong_landing`）、
  按钮/遮罩暂不可点、结果识别不出（`consecutive_unknown`）；
  以及**技术性「已关闭」**：标签页已关闭、会话已关闭、WebBridge 连接已关闭、evaluate 失败等。
  这类只是页面/通道抖了一下，岗位还开着，下一轮照常重投（26.11.26 前会被误判成终止态而永久锁死）。
- 想让历史临时失败立刻重新进候选：`--step reset-failed`；连终止态一起清：`--step reset-failed --include-terminal`。
  顺序固定为**先归档、归档写成功才清台账**：归档失败则一条都不清并返回失败，重跑即可，不会出现「数据清了、证据没了」。
  成功记录一律不受影响。

命中风控字样会**立即停机**（不重试、不清障复投），停机原因码写入 `serial_progress.json` 并在 `--step status` / 汇总页展示。人工确认页面恢复正常后清除停机标记：

```
python scripts/serial_loop.py --step init --ack-risk
```

停机原因码：

| 原因码 | 触发条件 | 处置 |
|---|---|---|
| `risk_control` | 页面出现访问受限 / 异常行为 / 验证码 / 人机验证等字样，或跳转验证页 | 立即停机，人工到浏览器确认账号状态，验证通过后 `--ack-risk`；**禁止任何自动重试** |
| `auth_fail` | 登录态失效 | 人工重新登录后 `--step init` |
| `daily_limit` | 触发平台 150 次/日硬顶 | 次日再投 |
| `comm_limit` | 连续 3 条沟通上限、清障复投均无效 | 人工确认页面状态后 `--step init` |
| `selector_broken` | 连续 3 次关键按钮/面板找不到 | 多为页面改版，等适配信息更新后再跑 |
| `consecutive_unknown` | 连续 3 次无法识别投递结果 | 人工核对页面 |
| `target_reached` | 当日投递目标已达成 | 正常结束，不再续投 |

> **给 Agent**：执行前必须通读 `references/execution-manual.md`。判断规则、两级沟通上限处理、运行期禁动页等全部执行口径都在那份文件里，以它为准。

## 运行稳定性
- 所有状态文件原子写（临时文件 + fsync + os.replace），中断不会留空文件/半截 JSON，重跑即可续投；
- **同一时刻只跑一轮**：动作类步骤启动时会抢一把运行锁（`run.lock`），已被占用就当场拒绝并退出（退出码 2）。
  并行的两次投递会互相覆盖进度台账，表现是已投递记录凭空消失、老岗位被重复投递。
  只想看进度不受影响：`--step status` 不加锁，跑任务时照常可查。
  同一把锁也覆盖所有会单独跑起来的写台账脚本（`serial_loop` / `deliver_engine` / `jd_reader` / `report_builder` / `surface_cache` / `keyword_evolver` / `recommend_loader`），
  所以手工调试单个脚本时同样会被挡住（这是对的：它和正在跑的那轮会互相覆盖台账）。只读的 `stats_core.py` 不加锁。
  查谁占着：`python scripts/run_lock.py --info`；确认无人在跑仍提示占用：`python scripts/run_lock.py --clear`。
  `--clear` 遇持锁进程还活着会拒绝（退出码 3）——删锁等于允许两个进程同时写台账；确认要强行拆互斥才加 `--force`。
  崩溃/强杀留下的残留锁不必手工清，下次动作类步骤会自动接管（持锁进程已退出、或 pid 已被系统复用给别的进程，都视为残留）；
- 页面对齐以当前 DOM 实卡数为唯一真源（`--step next-batch` 重建窗口），队列与页面等长同序，`read/decide` 的 `--index` 即页面卡片位；
- 跑长任务禁止用管道截断输出（`| Select-Object -First N`、`| head -n N`），管道提前关闭会终止 python 进程；

## 免费体验与激活

- **首次运行当天免费**，可投满平台一天的上限。从第二天起需要激活码。
- 免费资格由服务端统一记录，删除本地文件不会重置。
- 激活：把激活码发给 Agent，由脚本执行
  ```
  python scripts/entitlement.py --activate <激活码>
  ```
  激活后本机记录授权状态，之后每天直接跑；查看状态 `--status`。
- 作者联系方式显示在技能生成的汇报页上；需要激活码或续期时按页面上的方式联系。

## 页面改版自动适配

平台页面会不定期改版。技能联网时会自动更新适配信息，拿不到或更新失败就沿用本机已有的版本，不影响使用；断网时直接用本机版本。

当前状态可用 `python scripts/cloud_client.py --status` 查看。

## 目录结构

```
SKILL.md                       # 本文件：定位、安装、配置
README.md                      # 快速上手
references/execution-manual.md # 执行手册（Agent 必读的细则）
scripts/                       # 全部执行脚本
dist/                          # 运行期资源
user_profile.example.json      # 画像模板
```

## 注意

自动化投递属于对平台页面的批量操作，存在账号风控风险，请自行评估后使用，建议把日投递量控制在合理范围。本 skill 仅在本机操作你自己的账号，不上传任何个人数据。

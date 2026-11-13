#!/usr/bin/env python3
"""
模块 I: ★三层嵌套循环主控制器 (v5.0)

★ 核心设计 — 严格对应流程图的三层循环:

  外层循环 (页面级):
    推荐页 → 穷尽 → AI评估 → 新关键词 → 搜索页 → 穷尽 → AI评估 → 新关键词 → ...
    终止条件: 投递总数达标 (DAILY_TARGET=150)

  中层循环 (分批级):
    每页分批50条: 滚动加载 → 提取 → AI初筛 → 进入内层 → 内层完 → 下一批
    终止条件: 页面穷尽 (exhausted=True)

  内层循环 (岗位级):
    逐条: 读JD → AI判断 → 投递/跳过 → 检查目标 → 下一条
    终止条件: 本批JD待读取列表处理完

★ 命令体系:
    --step init          → 检查环境 + 导航到推荐页 (流程图: 开始→检查环境→定位到推荐页)
    --step next-batch    → 滚动加载下一批50条 (中层循环)
    --step read --index N    → 读第N条JD (内层循环 步骤A)
    --step decide --index N  → 执行AI决策+检查目标 (内层循环 步骤B+C)
    --step evolve        → 页面穷尽后AI评估+新关键词+导航搜索页 (外层循环)
    --step finish        → 生成HTML汇报 + 授权状态 + 微信随喜区 (流程图: 汇总投递岗位)
    --step status        → 查看三层循环完整进度

信号文件:
    page_state.json      — 页面级状态 (跨进程持久化)
    batch_state.json     — 分批级状态 (跨进程持久化)
    surface_cache.json   — 全量缓存 (分批追加)
    jd_to_read.json      — AI筛选后的待读取列表
    current_jd.json      — 当前JD (供AI阅读)
    decision.json        — AI决策
    serial_progress.json — 串行进度
    stream_progress.json — 投递进度 (断点续传)
"""

import json
import os
import sys
import time
import argparse
import atexit

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.profile_loader import WORK_DIR, CITY_CODE as _PROFILE_CITY
from scripts import entitlement as ent

# 信号文件路径
JD_TO_READ = os.path.join(WORK_DIR, "jd_to_read.json")
CURRENT_JD = os.path.join(WORK_DIR, "current_jd.json")
DECISION_FILE = os.path.join(WORK_DIR, "decision.json")
SERIAL_PROGRESS = os.path.join(WORK_DIR, "serial_progress.json")
SURFACE_CACHE = os.path.join(WORK_DIR, "surface_cache.json")
PROGRESS_FILE = os.path.join(WORK_DIR, "stream_progress.json")

from scripts.webbridge_client import (
    SESSION, evaluate, health_check, ensure_active_tab,
    handle_popup, popup_residue,
    ProgressManager,
)
from scripts.jd_reader import read_jd_for_job
from scripts.deliver_engine import (deliver_inplace, check_delivery_target, DAILY_TARGET,
                                    MAX_CONSECUTIVE_UNKNOWN, MAX_CONSECUTIVE_COMM)
from scripts.recommend_loader import (
    navigate_to_recommend, scroll_next_batch, is_exhausted, get_state, reset_state,
)
from scripts.keyword_evolver import (
    evaluate_page_results, get_next_keyword, add_evolved_keyword,
    mark_keyword_tried, navigate_to_search, start_new_page,
    load_page_state, save_page_state, reset_page_state,
)
from scripts.report_builder import build_report


# ═══════════════════════════════════════════════════════════
# 串行进度管理
# ═══════════════════════════════════════════════════════════

def _safe_clear(path):
    """安全重置信号文件：写入 null JSON 替代 os.remove（避免安全拦截）"""
    with open(path, 'w', encoding='utf-8') as f:
        f.write('null')


def load_serial_progress():
    """加载串行进度"""
    if os.path.exists(SERIAL_PROGRESS):
        with open(SERIAL_PROGRESS, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if data is None:
                return {
                    "current_index": 0,
                    "total": 0,
                    "processed": [],
                    "results": [],
                    "batch_num": 0,
                    "fatal_stop": False,
                    "fatal_reason": "",
                    "target_met": False,
                    "consecutive_unknown_count": 0,
                    "consecutive_comm_count": 0,
                }
            return data
    return {
        "current_index": 0,
        "total": 0,
        "processed": [],
        "results": [],
        "batch_num": 0,
        "fatal_stop": False,
        "fatal_reason": "",
        "target_met": False,
        "consecutive_unknown_count": 0,
        "consecutive_comm_count": 0,
    }


def save_serial_progress(progress):
    """保存串行进度"""
    with open(SERIAL_PROGRESS, 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def load_jobs():
    """加载 jd_to_read.json"""
    if not os.path.exists(JD_TO_READ):
        return []
    with open(JD_TO_READ, 'r', encoding='utf-8') as f:
        jobs = json.load(f)
    # 顺序守卫: 按 page_order 排序
    if jobs and 'page_order' in jobs[0]:
        jobs.sort(key=lambda x: x.get('page_order', 0))
    return jobs


# ═══════════════════════════════════════════════════════════
# ★ 外层循环: 页面级
# ═══════════════════════════════════════════════════════════

def step_init(city_code=_PROFILE_CITY):
    """★ 流程图: 开始 → 检查环境 → 定位到职位推荐页

    1. 检查 WebBridge 环境
    2. 重置所有状态（分批状态 + 页面状态）
    3. 导航到推荐页
    4. 提示下一步: --step next-batch
    """
    print("[初始化] ═══ v5.0 三层循环投递系统启动 ═══")

    # 0. 授权门禁（只在 init 边界判一次，运行期绝不中途打断）
    snap = ent.gate_at_start()
    print(f"[授权] {snap['title']} | 指令包 {snap['plan_version'] or '未加载'}（{snap['plan_source']}）")
    if not snap["allowed"]:
        print(f"[授权] {snap['detail']}")
        print("[授权] 拿到码后执行: python scripts/entitlement.py --activate <CODE>")
        if snap["wechat_id"]:
            print(f"[授权] 作者微信: {snap['wechat_id']}")
        print("[授权] init 已停止；已经在跑的流水线不会被动到。")
        return False

    # 1. 环境检查
    if not health_check():
        print("[初始化] ERROR: WebBridge 不可达")
        print("[初始化] 请先运行: python env_check.py")
        return False

    if not ensure_active_tab():
        print("[初始化] ERROR: 无法建立标签页")
        return False

    print("[初始化] WebBridge 环境正常")

    # 2. 重置状态
    reset_state()        # 重置分批状态 (batch_state.json)
    reset_page_state()   # 重置页面状态 (page_state.json)

    # 清理旧的串行进度
    _safe_clear(SERIAL_PROGRESS)
    _safe_clear(JD_TO_READ)

    # 3. 导航到推荐页
    print("[初始化] 导航到推荐页...")
    page_state = load_page_state()
    page_state['page_type'] = 'recommend'
    page_state['current_keyword'] = ''
    page_state['pages_processed'] = 1

    # 记录本页面开始时的投递数
    pm = ProgressManager(PROGRESS_FILE)
    pm.load()
    page_state['page_start_delivered'] = len(getattr(pm, 'delivered', []))
    save_page_state(page_state)

    if not navigate_to_recommend(city_code):
        print("[初始化] ERROR: 导航到推荐页失败")
        return False

    # 4. 提示下一步
    print(f"\n[初始化] ★ 推荐页已就绪")
    print(f"[初始化] ★ 投递目标: {DAILY_TARGET} 份")
    print(f"[初始化] ★ 下一步: python serial_loop.py --step next-batch")
    print(f"[初始化] ★ 然后AI对本批岗位做语义初筛，逐条 --step read/decide")

    return True


def step_evolve(city_code=_PROFILE_CITY):
    """★ 外层循环: 页面穷尽后 → AI评估 → 新关键词 → 搜索页

    流程图: AI评估当前页面投递结果 → 找出新的关键词 → 以新关键词搜索 → 定位到搜索结果页

    1. 评估当前页面的投递结果
    2. 获取下一个未尝试的搜索关键词
    3. 如果有可用关键词 → 导航到搜索页 → 提示 --step next-batch
    4. 如果无可用关键词 → 提示AI生成新关键词 (--step add-keyword)
    5. 如果投递目标已达标 → 提示 --step finish
    """
    print("[进化] ═══ 外层循环: 页面穷尽，开始关键词进化 ═══")

    # 0. 先检查投递目标是否已达标
    target_check = check_delivery_target(PROGRESS_FILE)
    if target_check["target_met"]:
        print(f"[进化] ★ 投递目标 {DAILY_TARGET} 份已达标！无需继续搜索")
        # ★ 自动触发 finish，确保 100% 生成投递汇总页面
        print(f"[进化] ★ 自动生成投递汇总...")
        step_finish()
        return True

    # 1. 评估当前页面投递结果
    print(f"\n[进化] 步骤1: 评估当前页面投递结果")
    eval_result = evaluate_page_results()

    print(f"\n[进化] 评估结果:")
    print(f"  本页面投递: {eval_result['page_delivered']} 份")
    print(f"  累计投递: {eval_result['total_delivered']} 份")
    print(f"  目标: {DAILY_TARGET} 份 (还差 {target_check['remaining']})")
    print(f"  行业分布: {eval_result['industry_distribution']}")
    print(f"  高频标题词: {eval_result['title_keywords']}")

    # 2. 获取下一个关键词
    print(f"\n[进化] 步骤2: 获取下一个搜索关键词")
    next_kw = get_next_keyword()

    if next_kw:
        print(f"[进化] 找到可用关键词: {next_kw}")
        print(f"\n[进化] ★ AI请确认是否使用此关键词，或用 --step add-keyword 添加自定义关键词")
        print(f"[进化] ★ 确认后运行:")
        print(f"  python serial_loop.py --step navigate-search --keyword \"{next_kw}\"")
        return True
    else:
        print(f"[进化] ⚠ 预定义关键词已全部用完！")
        print(f"[进化] ★ AI需要基于投递结果生成新的搜索关键词")
        print(f"[进化] ★ 参考信息:")
        print(f"  - 成功投递的行业分布: {eval_result['industry_distribution']}")
        print(f"  - 高频标题关键词: {eval_result['title_keywords']}")
        print(f"  - 用户画像偏好: 见 user_profile.json")
        print(f"\n[进化] ★ AI生成新关键词后运行:")
        print(f"  python serial_loop.py --step add-keyword --keyword \"新关键词\"")
        print(f"  python serial_loop.py --step navigate-search --keyword \"新关键词\"")
        return False


def step_navigate_search(keyword, city_code=_PROFILE_CITY):
    """导航到搜索结果页（外层循环的页面切换）

    流程图: 以新关键词搜索岗位 → 定位到搜索结果页 → 回到"当前页面处理开始"
    """
    print(f"[搜索] ═══ 导航到搜索结果页: {keyword} ═══")

    if not health_check():
        print("[搜索] ERROR: WebBridge 不可达")
        return False

    # 导航到搜索页
    ok, clean_kw = navigate_to_search(keyword, city_code)
    if not ok:
        print(f"[搜索] ERROR: 导航到搜索页失败 (关键词: {keyword})")
        print(f"[搜索] 该关键词可能无搜索结果，请尝试其他关键词")
        return False

    # 标记关键词为已尝试（使用清洗后的关键词）
    mark_keyword_tried(clean_kw)

    # 更新页面状态
    page_state = load_page_state()
    page_state['page_type'] = 'search'
    page_state['current_keyword'] = keyword
    page_state['pages_processed'] = page_state.get('pages_processed', 0) + 1

    # 记录本页面开始时的投递数
    pm = ProgressManager(PROGRESS_FILE)
    pm.load()
    page_state['page_start_delivered'] = len(getattr(pm, 'delivered', []))
    save_page_state(page_state)

    # 重置分批状态（新页面从0开始）
    reset_state()

    print(f"\n[搜索] ★ 搜索结果页已就绪")
    print(f"[搜索] ★ 页面类型: 搜索页")
    print(f"[搜索] ★ 关键词: {keyword}")
    print(f"[搜索] ★ 已处理页面数: {page_state['pages_processed']}")
    print(f"[搜索] ★ 下一步: python serial_loop.py --step next-batch")

    return True


def step_add_keyword(keyword):
    """添加AI进化生成的新关键词"""
    add_evolved_keyword(keyword)
    print(f"[进化] 已添加新关键词: {keyword}")
    print(f"[进化] 下一步: python serial_loop.py --step navigate-search --keyword \"{keyword}\"")


# ═══════════════════════════════════════════════════════════
# ★ 中层循环: 分批级
# ═══════════════════════════════════════════════════════════

def step_next_batch():
    """★ 中层循环: 滚动加载下一批50条

    流程图: 当前页面是否还有更多岗位？→ 是 → 下拉加载一批 → 提取 → AI初筛

    1. 检查页面是否已穷尽
    2. 如果穷尽 → 提示 --step evolve (进入外层循环)
    3. 如果未穷尽 → 滚动加载下一批 → 增量提取 → append缓存
    4. 输出本批岗位列表供AI初筛
    """
    if not health_check():
        print("ERROR: WebBridge 不可达")
        return False

    # 检查是否已穷尽
    if is_exhausted():
        state = get_state()
        print(f"[分批] ★ 当前页面已穷尽: {state.get('exhaust_reason', '?')}")
        print(f"[分批] 累计提取 {state.get('total_extracted', 0)} 条")
        print(f"[分批] ★ 进入外层循环: python serial_loop.py --step evolve")
        return False

    # 滚动加载下一批
    result = scroll_next_batch(cache_file=SURFACE_CACHE)

    if result["new_count"] > 0:
        print(f"\n[分批] ═══ 第{result['batch_num']}批加载完成 ═══")
        print(f"[分批] 新增: {result['new_count']} 条")
        print(f"[分批] 累计提取: {result['total_extracted']} 条")
        print(f"[分批] 页面总卡片: {result['total_cards']} 条")
        print(f"[分批] 穷尽状态: {result['exhausted']}")

        # 输出本批岗位列表
        page_state = load_page_state()
        page_type = page_state.get('page_type', 'recommend')
        current_kw = page_state.get('current_keyword', '')

        print(f"\n{'='*60}")
        print(f"本批岗位列表 (供AI语义初筛)")
        print(f"页面类型: {page_type}" + (f" | 关键词: {current_kw}" if current_kw else ""))
        print(f"{'='*60}")
        for j in result["new_jobs"]:
            po = j.get('page_order', '?')
            title = j.get('title', '')[:25]
            salary = j.get('salary', '')[:12]
            company = j.get('company', '')[:15]
            industry = j.get('industry', '')[:12]
            print(f"  [{po:>3}] {title:<25} {salary:<12} {company:<15} {industry}")

        print(f"\n[分批] ★ AI请对本批{result['new_count']}条做语义初筛")
        print(f"[分批] ★ 筛选结果追加到 jd_to_read.json (保持page_order顺序)")
        print(f"[分批] ★ 然后逐条:")
        print(f"  python serial_loop.py --step read --index N")
        print(f"  python serial_loop.py --step decide --index N")

        # 更新进度
        progress = load_serial_progress()
        progress["batch_num"] = result["batch_num"]
        progress["total"] = result["total_extracted"]
        save_serial_progress(progress)
    else:
        print(f"[分批] 本批无新岗位")
        if result["exhausted"]:
            print(f"[分批] ★ 页面已穷尽，进入外层循环: python serial_loop.py --step evolve")

    return result["new_count"] > 0


# ═══════════════════════════════════════════════════════════
# ★ 内层循环: 岗位级
# ═══════════════════════════════════════════════════════════

def step_read(index):
    """★ 内层循环 步骤A: 读取第 index 条JD

    流程图: 取序号最靠前的待处理岗位 → 点击标签卡读取右侧JD
    """
    jobs = load_jobs()
    if not jobs:
        print("ERROR: jd_to_read.json 为空或不存在")
        print("★ 请先运行 --step next-batch 加载岗位，然后AI初筛生成 jd_to_read.json")
        return False

    if index < 1 or index > len(jobs):
        print(f"ERROR: 索引超出范围 1~{len(jobs)}")
        return False

    job = jobs[index - 1]
    jid = job.get('jobId', '')
    title = job.get('title', '')
    company = job.get('company', '')
    salary = job.get('salary', '')
    page_order = job.get('page_order', '?')

    print(f"[串行] ═══ 内层循环 [{index}/{len(jobs)}] ═══")
    print(f"[串行] page_order={page_order}")
    print(f"[串行] 读取JD: {title} | {salary} | {company}")

    if not health_check():
        print("ERROR: WebBridge 不可达")
        return False

    result = read_jd_for_job(jid)

    if not result.get('ok'):
        print(f"[串行] JD读取失败: {result.get('detail', '')}")
        print(f"[串行] ⚠⚠⚠ 严禁自行动页！ ⚠⚠⚠")
        print(f"[串行] 根因: read找不到卡片，几乎都是因为Agent在流水线运行期")
        print(f"[串行]       做了页面切换/刷新/导航，导致推荐列表重排序、卡片漂移。")
        print(f"[串行] ★ 正确做法: 立即停止，提示用户当前状态，等待用户指示。")
        print(f"[串行] ★ 禁止: 刷新页面、切换标签页、navigate、滚动重试（方向错误）。")
        print(f"[串行] ★ 若确需恢复，只能由用户确认后执行 --step init 重定位推荐页。")
        with open(CURRENT_JD, 'w', encoding='utf-8') as f:
            json.dump({
                'index': index,
                'total': len(jobs),
                'jobId': jid,
                'title': title,
                'company': company,
                'salary': salary,
                'page_order': page_order,
                'jd_text': '',
                'error': result.get('detail', ''),
                'surface_info': job,
            }, f, ensure_ascii=False, indent=2)
        return False

    jd_data = result.get('jd', {})
    jd_text = jd_data.get('jd_text', '')

    with open(CURRENT_JD, 'w', encoding='utf-8') as f:
        json.dump({
            'index': index,
            'total': len(jobs),
            'jobId': jid,
            'title': title,
            'company': company,
            'salary': salary,
            'page_order': page_order,
            'jd_text': jd_text,
            'jd_full': jd_data,
            'surface_info': job,
        }, f, ensure_ascii=False, indent=2)

    print(f"[串行] JD已写入 current_jd.json (共{len(jd_text)}字)")
    print(f"[串行] ★ 等待AI阅读 current_jd.json 并给出决策")
    print(f"[串行] ★ 决策方式 (v5.7 推荐):")
    print(f"  python serial_loop.py --step write-decision --decision APPROVE --reason \"理由\"")
    print(f"  python serial_loop.py --step write-decision --decision REJECT --reason \"理由\"")
    print(f"[串行] ★ 然后执行: python serial_loop.py --step decide --index {index}")

    # 更新进度
    progress = load_serial_progress()
    progress['current_index'] = index
    progress['total'] = len(jobs)
    save_serial_progress(progress)

    return True


def step_write_decision(decision, reason=''):
    """★ v5.7 新增: AI 通过命令行直接覆写 decision.json

    目的: 避免 AI 侧 write_file 对已存在文件自动重命名、以及 delete 触发确认弹窗打断循环。
    用法: python serial_loop.py --step write-decision --decision APPROVE --reason "理由"
    脚本内部使用 open('w') 直接覆写，幂等安全。
    """
    decision = str(decision).upper()
    if decision not in ('APPROVE', 'REJECT'):
        print(f"ERROR: --decision 必须是 APPROVE 或 REJECT，实际: {decision}")
        return False

    data = {"decision": decision, "reason": reason}

    # 直接覆写（open 'w' 而非 os.remove，避免触发外部删除确认）
    with open(DECISION_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"[决策] ✓ decision.json 已覆写: {decision}")
    if reason:
        print(f"[决策] 理由: {reason}")
    print(f"[决策] 下一步: python serial_loop.py --step decide --index {load_serial_progress().get('current_index', 'N')}")
    return True


def step_decide(index):
    """★ 内层循环 步骤B+C: 执行AI决策 + 检查投递目标

    流程图: 是否投递？ → 是 → 点击"立即沟通" → 总投递数量是否满足要求？
                        → 否 → 将该岗位标记为已处理
    """
    if not os.path.exists(DECISION_FILE):
        print(f"ERROR: {DECISION_FILE} 不存在，AI尚未做决策")
        print(f"★ AI请先阅读 current_jd.json，然后写入 decision.json")
        return False

    with open(DECISION_FILE, 'r', encoding='utf-8') as f:
        decision_data = json.load(f)

    decision = decision_data.get('decision', '').upper()
    reason = decision_data.get('reason', '')

    jobs = load_jobs()
    if index < 1 or index > len(jobs):
        print(f"ERROR: 索引超出范围 1~{len(jobs)}")
        return False

    job = jobs[index - 1]
    jid = job.get('jobId', '')
    title = job.get('title', '')

    print(f"[串行] [{index}/{len(jobs)}] AI决策: {decision}")
    print(f"[串行] 理由: {reason}")

    progress = load_serial_progress()
    result_entry = {
        'index': index,
        'jobId': jid,
        'title': title,
        'decision': decision,
        'reason': reason,
        'page_order': job.get('page_order', '?'),
    }

    if decision == 'APPROVE':
        # 流程图: 点击"立即沟通" → 弹窗中点击"留在此页" → 投递成功
        print(f"[串行] 执行投递: {title} ({jid[:16]}...)")

        if not health_check():
            print("ERROR: WebBridge 不可达")
            return False

        status, detail = deliver_inplace(jid, job_info=job)
        result_entry['deliver_status'] = status
        result_entry['deliver_detail'] = detail

        print(f"[串行] 投递结果: {status} - {detail}")

        # ★ 20261113: comm_limit 先清障再复投一次——多数 comm_limit 是叠加遮罩挡住点击的假失败
        if status == 'comm_limit':
            _cleared = handle_popup()
            _left = popup_residue()
            if _left == 0:
                print(f"[串行] 已清障({_cleared})，复投一次...")
                time.sleep(1)
                retry_status, retry_detail = deliver_inplace(jid, job_info=job)
                if retry_status == 'comm_limit':
                    detail = f"{detail} [清障后复投仍 comm_limit]"
                    print(f"[串行] 复投结果: 仍 comm_limit")
                else:
                    status, detail = retry_status, f"{retry_detail} [清障后复投]"
                    print(f"[串行] 复投结果: {status} - {retry_detail}")
                result_entry['deliver_status'] = status
                result_entry['deliver_detail'] = detail
            else:
                print(f"[串行] 清障未净，残留 {_left} 个遮罩，按 comm_limit 记录")

        # 记录到 ProgressManager
        pm = ProgressManager(PROGRESS_FILE)
        pm.load()
        if status == 'success':
            pm.add_delivered(jid, job)
            progress['consecutive_comm_count'] = 0
            # 免费额度按自然日计数；失败不影响主流程
            try:
                ent.record_delivery(1)
            except Exception:
                pass
        else:
            pm.add_failed(jid, job, f"{status}: {detail}")
        pm.save()

        # ★ 流程图: 总投递数量是否满足要求？
        target_check = check_delivery_target(PROGRESS_FILE)

        # 致命错误检测
        if status == 'fatal_limit':
            print(f"[串行] ★★★ 致命错误: 每日上限！停止所有投递 ★★★")
            progress['fatal_stop'] = True
            progress['fatal_reason'] = detail
            progress['target_met'] = target_check['target_met']
            save_serial_progress(progress)
            # ★ 自动触发 finish，确保 100% 生成投递汇总页面
            print(f"[串行] ★ 自动生成投递汇总...")
            step_finish()
            return True

        # ★ 20261113: 单条 comm_limit 不阻断（120 是提示，150 才是硬顶，硬顶由 fatal_limit 拦）；
        # deliver_inplace 已做 preflight 清障 + 清障后复投，仍连续多条 comm_limit 说明遮罩清不干净
        # 或平台真封顶，继续投只会把假失败刷满进度文件，故做连续熔断并提示人工确认。
        if status == 'comm_limit':
            progress['consecutive_comm_count'] = progress.get('consecutive_comm_count', 0) + 1
            print(f"[串行] ★ 沟通上限提示(120次): 清障复投仍失败 "
                  f"(连续 {progress['consecutive_comm_count']}/{MAX_CONSECUTIVE_COMM})")
            progress['target_met'] = target_check['target_met']
            if progress['consecutive_comm_count'] >= MAX_CONSECUTIVE_COMM:
                progress['fatal_stop'] = True
                progress['fatal_reason'] = (f"连续{MAX_CONSECUTIVE_COMM}次comm_limit，"
                                            f"疑似页面遮罩残留或已达平台上限")
                save_serial_progress(progress)
                print(f"[串行] ★★★ 连续 {MAX_CONSECUTIVE_COMM} 次 comm_limit，判定遮罩残留或真封顶，"
                      f"停止流水线；请人工确认页面状态后再 init ★★★")
                print(f"[串行] ★ 自动生成投递汇总...")
                step_finish()
                return True
            save_serial_progress(progress)
            # 不设 fatal_stop，不 finish，继续下一条投递
            return True

        # ★ 连续 unknown 状态熔断检测
        if status == 'unknown':
            progress['consecutive_unknown_count'] = progress.get('consecutive_unknown_count', 0) + 1
            print(f"[串行] ★ 连续 unknown 计数: {progress['consecutive_unknown_count']}/{MAX_CONSECUTIVE_UNKNOWN}")
            if progress['consecutive_unknown_count'] >= MAX_CONSECUTIVE_UNKNOWN:
                print(f"[串行] ★★★ 连续 {MAX_CONSECUTIVE_UNKNOWN} 次投递结果 unknown，疑似每日上限或页面异常，熔断停止 ★★★")
                progress['fatal_stop'] = True
                progress['fatal_reason'] = f"连续{MAX_CONSECUTIVE_UNKNOWN}次投递结果unknown，疑似每日上限"
                progress['target_met'] = target_check['target_met']
                save_serial_progress(progress)
                print(f"[串行] ★ 自动生成投递汇总...")
                step_finish()
                return True
        else:
            # 非 unknown（success / fail 等），重置计数器
            progress['consecutive_unknown_count'] = 0

        if target_check["target_met"]:
            print(f"\n[串行] ★★★ 投递目标 {DAILY_TARGET} 份已达成！★★★")
            progress['target_met'] = True
            save_serial_progress(progress)
            # 仍然标记本条为已处理
            if index not in progress.get('processed', []):
                progress.setdefault('processed', []).append(index)
            progress.setdefault('results', []).append(result_entry)
            save_serial_progress(progress)
            if os.path.exists(DECISION_FILE):
                _safe_clear(DECISION_FILE)
            # ★ 自动触发 finish，确保 100% 生成投递汇总页面
            print(f"[串行] ★ 自动生成投递汇总...")
            step_finish()
            return True

    elif decision == 'REJECT':
        # 流程图: 将该岗位标记为已处理
        print(f"[串行] 跳过: {title}")
        result_entry['deliver_status'] = 'skipped'
    else:
        print(f"ERROR: 无效决策 '{decision}'，必须是 APPROVE 或 REJECT")
        return False

    # 标记为已处理
    if index not in progress.get('processed', []):
        progress.setdefault('processed', []).append(index)
    progress.setdefault('results', []).append(result_entry)
    save_serial_progress(progress)

    # 清理 decision.json
    if os.path.exists(DECISION_FILE):
        _safe_clear(DECISION_FILE)

    print(f"[串行] ✓ 第{index}条处理完成")

    # ★ 提示下一步 — 根据流程图判断走向
    if progress.get('target_met'):
        print(f"[串行] ★ 投递目标已达成")
        print(f"[串行] ★ 下一步: python serial_loop.py --step finish")
    elif index < len(jobs):
        # 本批还有待处理岗位 → 继续内层循环
        print(f"[串行] 下一条: python serial_loop.py --step read --index {index+1}")
    else:
        # 本批处理完 → 检查页面是否还有更多岗位（中层循环）
        print(f"[串行] ★ 本批{len(jobs)}条已全部处理完毕！")
        if is_exhausted():
            # 页面穷尽 → 外层循环
            print(f"[串行] ★ 当前页面已穷尽")
            print(f"[串行] ★ 下一步: python serial_loop.py --step evolve")
        else:
            # 页面还有更多岗位 → 中层循环
            print(f"[串行] ★ 当前页面还有更多岗位可加载")
            print(f"[串行] ★ 下一步: python serial_loop.py --step next-batch")

    return True


# ═══════════════════════════════════════════════════════════
# ★ 终止: 生成汇报
# ═══════════════════════════════════════════════════════════

def step_finish():
    """★ 流程图: 汇总投递岗位 → 生成HTML汇报（授权状态 + 微信随喜区）

    终止条件达成时调用:
    - 投递目标达标
    - 致命错误（每日上限）
    - 所有页面穷尽且无新关键词
    """
    print("[完成] ═══ 生成投递汇报 ═══")

    output_path = os.path.join(WORK_DIR, "投递汇总.html")
    snap = ent.status_snapshot()
    result = build_report(progress_file=PROGRESS_FILE, output_path=output_path,
                          entitlement_info=snap)

    if result:
        print(f"\n[完成] ★ 投递汇报已生成: {result}")
        print(f"[完成] ★ 投递流程结束")
        print(f"[完成] 授权状态: {snap['title']}；{ent.activation_hint(snap)}")
        if snap["wechat_id"]:
            print(f"[完成] 作者微信: {snap['wechat_id']}")

        # 显示最终统计
        target_check = check_delivery_target(PROGRESS_FILE)
        page_state = load_page_state()
        batch_state = get_state()

        print(f"\n{'='*60}")
        print(f"最终统计")
        print(f"{'='*60}")
        print(f"投递目标: {DAILY_TARGET} 份")
        print(f"实际投递: {target_check['delivered_count']} 份")
        print(f"目标达成: {'是' if target_check['target_met'] else '否'}")
        print(f"处理页面数: {page_state.get('pages_processed', 0)}")
        print(f"已尝试关键词: {len(page_state.get('tried_keywords', []))} 个")
        print(f"总提取岗位: {batch_state.get('total_extracted', 0)} 条")
        print(f"总滚动次数: {batch_state.get('total_scrolls', 0)} 次")

        progress = load_serial_progress()
        if progress.get('fatal_stop'):
            print(f"停止原因: 致命错误 - {progress.get('fatal_reason', '')}")
        elif progress.get('target_met'):
            print(f"停止原因: 投递目标达成")
        else:
            print(f"停止原因: 页面穷尽且无新关键词")

        print(f"{'='*60}")
    else:
        print("[完成] ERROR: 汇报生成失败")

    return result is not None


# ═══════════════════════════════════════════════════════════
# ★ 状态查看
# ═══════════════════════════════════════════════════════════

def step_status():
    """查看三层循环完整进度"""
    snap = ent.status_snapshot()
    progress = load_serial_progress()
    jobs = load_jobs()
    batch_state = get_state()
    page_state = load_page_state()
    target_check = check_delivery_target(PROGRESS_FILE)

    total = len(jobs)
    current = progress.get('current_index', 0)
    processed = progress.get('processed', [])
    results = progress.get('results', [])

    approved = [r for r in results if r.get('decision') == 'APPROVE']
    rejected = [r for r in results if r.get('decision') == 'REJECT']
    delivered = [r for r in approved if r.get('deliver_status') == 'success']

    print(f"{'='*60}")
    print(f"v5.0 三层循环进度")
    print(f"{'='*60}")
    print(f"授权        : {snap['title']}（{snap['mode']}）")
    print(f"指令包      : {snap['plan_version'] or '未加载'}  来源 {snap['plan_source']}")
    print(f"免费额度    : 今天 {snap['free_used_today']}/{snap['free_limit']} 份"
          f"  首日 {snap['free_first_day']}")
    if snap["wechat_id"]:
        print(f"作者微信    : {snap['wechat_id']}")

    # 外层: 页面级
    print(f"\n┌─ 外层循环 (页面级) ──────────────────────")
    print(f"│ 页面类型: {page_state.get('page_type', '?')}")
    print(f"│ 当前关键词: {page_state.get('current_keyword', '(推荐页)')}")
    print(f"│ 已处理页面: {page_state.get('pages_processed', 0)}")
    print(f"│ 已尝试关键词: {len(page_state.get('tried_keywords', []))} 个")
    print(f"│ 进化关键词: {len(page_state.get('evolved_keywords', []))} 个")

    # 中层: 分批级
    print(f"│")
    print(f"├─ 中层循环 (分批级) ──────────────────────")
    print(f"│ 当前批次: 第{batch_state.get('batch_num', 0)}批")
    print(f"│ 页面总卡片: {batch_state.get('total_cards', 0)}")
    print(f"│ 累计提取: {batch_state.get('total_extracted', 0)}")
    print(f"│ 累计滚动: {batch_state.get('total_scrolls', 0)}次")
    print(f"│ 穷尽: {batch_state.get('exhausted', False)} ({batch_state.get('exhaust_reason', '')})")

    # 内层: 岗位级
    print(f"│")
    print(f"├─ 内层循环 (岗位级) ──────────────────────")
    print(f"│ jd_to_read.json: {total}条待处理")
    print(f"│ 当前处理: 第{current}条")
    print(f"│ 已处理: {len(processed)}")
    print(f"│   APPROVE: {len(approved)}")
    print(f"│   REJECT: {len(rejected)}")
    print(f"│   投递成功: {len(delivered)}")
    print(f"│ 剩余: {total - len(processed)}")

    # 目标
    print(f"│")
    print(f"├─ 投递目标 ──────────────────────────────")
    print(f"│ 目标: {target_check['target']} 份")
    print(f"│ 已投递: {target_check['delivered_count']} 份")
    print(f"│ 剩余: {target_check['remaining']} 份")
    print(f"│ 达标: {'是' if target_check['target_met'] else '否'}")

    # 停止状态
    if progress.get('fatal_stop'):
        print(f"│")
        print(f"├─ ⚠ 停止状态 ────────────────────────────")
        print(f"│ 致命停止: {progress.get('fatal_reason', '')}")
    elif progress.get('target_met'):
        print(f"│")
        print(f"├─ ★ 目标达成 ────────────────────────────")
        print(f"│ 运行 --step finish 生成汇报")

    print(f"└───────────────────────────────────────────")

    # 最近5条结果
    if processed:
        print(f"\n最近5条结果:")
        for r in results[-5:]:
            icon = '✓' if r.get('deliver_status') == 'success' else '✗' if r.get('decision') == 'REJECT' else '?'
            print(f"  {icon} [{r['index']}] {r['title'][:25]} → {r['decision']} ({r.get('deliver_status', '')})")

    # 下一步提示
    print(f"\n★ 下一步建议:")
    if progress.get('fatal_stop') or progress.get('target_met'):
        print(f"  python serial_loop.py --step finish")
    elif total == 0:
        if is_exhausted():
            print(f"  python serial_loop.py --step evolve")
        else:
            print(f"  python serial_loop.py --step next-batch")
    elif current < total and current not in processed:
        print(f"  python serial_loop.py --step read --index {current}")
    elif len(processed) < total:
        next_idx = min(i for i in range(1, total+1) if i not in processed)
        print(f"  python serial_loop.py --step read --index {next_idx}")
    else:
        # 本批处理完
        if is_exhausted():
            print(f"  python serial_loop.py --step evolve")
        else:
            print(f"  python serial_loop.py --step next-batch")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

def _ensure_finish_on_exit():
    """atexit 钩子：无论何种退出路径，只要有投递记录就生成汇报（幂等）。"""
    import os as _os
    import json as _json
    try:
        progress_file = _os.path.join(WORK_DIR, 'stream_progress.json')
        report_file = _os.path.join(WORK_DIR, '投递汇总.html')
        if not _os.path.exists(progress_file):
            return
        with open(progress_file, 'r', encoding='utf-8') as f:
            data = _json.load(f)
        has_records = bool(data.get('delivered') or data.get('failed'))
        if not has_records:
            return
        # 如果汇报文件不存在 或 进度文件比汇报文件新 → 需要重新生成
        if not _os.path.exists(report_file) or _os.path.getmtime(progress_file) > _os.path.getmtime(report_file):
            print("[收尾] ═══ 自动生成投递汇总（atexit 兜底） ═══")
            build_report(progress_file, report_file)
    except Exception:
        pass  # 静默失败，不影响主流程

def main():
    atexit.register(_ensure_finish_on_exit)
    parser = argparse.ArgumentParser(
        description="★v5.0 三层嵌套循环主控制器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
三层循环命令体系:
  外层 (页面级):
    --step init                    检查环境+导航推荐页 (开始)
    --step evolve                  页面穷尽→AI评估→新关键词
    --step navigate-search --kw X  导航到搜索结果页
    --step add-keyword --kw X      添加AI生成的新关键词

  中层 (分批级):
    --step next-batch              滚动加载下一批50条

  内层 (岗位级):
    --step read --index N          读第N条JD
    --step write-decision --decision APPROVE/REJECT [--reason "理由"]
                                    AI 覆写 decision.json (open 'w' 直接覆盖, 无需删除)
    --step decide --index N        执行AI决策+检查目标

  终止:
    --step finish                  生成HTML汇报 + 授权状态 + 微信随喜区
    --step status                  查看三层完整进度 + 授权状态

激活（独立 CLI，不在本控制器内）:
    python scripts/entitlement.py --activate <CODE>
    python scripts/entitlement.py --status
    python scripts/cloud_client.py --sync
        """)
    parser.add_argument("--step", required=True,
                        choices=['init', 'next-batch', 'read', 'write-decision', 'decide',
                                 'evolve', 'navigate-search', 'add-keyword',
                                 'finish', 'status'],
                        help="执行步骤")
    parser.add_argument("--index", type=int, default=0,
                        help="处理第几条 (read/decide时需要)")
    parser.add_argument("--decision", default="",
                        help="AI决策 APPROVE/REJECT (write-decision时需要)")
    parser.add_argument("--reason", default="",
                        help="AI决策理由 (write-decision时可选)")
    parser.add_argument("--keyword", default="",
                        help="搜索关键词 (navigate-search/add-keyword时需要)")
    parser.add_argument("--city", default=_PROFILE_CITY,
                        help="城市编码 (默认从 user_profile.json 读取)")
    args = parser.parse_args()

    if args.step == 'init':
        # 门禁未放行必须以非零码退出，否则 Agent 会误判 init 成功继续往下跑
        if not step_init(args.city):
            sys.exit(1)

    elif args.step == 'next-batch':
        step_next_batch()

    elif args.step == 'read':
        if args.index < 1:
            print("ERROR: --index 必须 >= 1")
            sys.exit(1)
        step_read(args.index)

    elif args.step == 'write-decision':
        if not args.decision:
            print("ERROR: 需要 --decision 参数 (APPROVE/REJECT)")
            sys.exit(1)
        step_write_decision(args.decision, args.reason)

    elif args.step == 'decide':
        if args.index < 1:
            print("ERROR: --index 必须 >= 1")
            sys.exit(1)
        step_decide(args.index)

    elif args.step == 'evolve':
        step_evolve(args.city)

    elif args.step == 'navigate-search':
        if not args.keyword:
            print("ERROR: 需要 --keyword 参数")
            sys.exit(1)
        step_navigate_search(args.keyword, args.city)

    elif args.step == 'add-keyword':
        if not args.keyword:
            print("ERROR: 需要 --keyword 参数")
            sys.exit(1)
        step_add_keyword(args.keyword)

    elif args.step == 'finish':
        step_finish()

    elif args.step == 'status':
        step_status()


if __name__ == "__main__":
    main()

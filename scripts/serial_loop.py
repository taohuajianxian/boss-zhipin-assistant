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
    --step finish        → 生成HTML汇报 + 授权状态 + 联系方式区 (流程图: 汇总投递岗位)
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
from scripts import cloud_client as cc
from scripts import job_filter
from scripts import stats_core
from scripts import decision_schema  # v5.8 取证门（结构+evidence 校验）

# 信号文件路径
JD_TO_READ = os.path.join(WORK_DIR, "jd_to_read.json")
CURRENT_JD = os.path.join(WORK_DIR, "current_jd.json")
DECISION_FILE = os.path.join(WORK_DIR, "decision.json")
SERIAL_PROGRESS = os.path.join(WORK_DIR, "serial_progress.json")
SURFACE_CACHE = os.path.join(WORK_DIR, "surface_cache.json")
PROGRESS_FILE = os.path.join(WORK_DIR, "stream_progress.json")
PAGE_WINDOW = os.path.join(WORK_DIR, "page_window.json")

from scripts.webbridge_client import (
    SESSION, evaluate, health_check, ensure_active_tab,
    handle_popup, popup_residue,
    ProgressManager,
)
from scripts.jd_reader import read_jd_for_job
from scripts.deliver_engine import (deliver_inplace, check_delivery_target, DAILY_TARGET,
                                    MAX_CONSECUTIVE_UNKNOWN, MAX_CONSECUTIVE_COMM,
                                    MAX_CONSECUTIVE_SELECTOR)
from scripts.recommend_loader import (
    navigate_to_recommend, scroll_next_batch, is_exhausted, get_state, reset_state,
)
from scripts.keyword_evolver import (
    evaluate_page_results, get_next_keyword, add_evolved_keyword,
    mark_keyword_tried, navigate_to_search, start_new_page,
    load_page_state, save_page_state, reset_page_state,
)
from scripts.report_builder import build_report
# P0-1: 停机原因码统一枚举与渲染（risk_control / comm_limit / daily_limit ...）
from scripts.stop_codes import (
    describe as stop_describe,
    hint_of as stop_hint,
    is_manual as stop_is_manual,
    new_stop_record,
    STOP_CODES,
)


# ═══════════════════════════════════════════════════════════
# 串行进度管理
# ═══════════════════════════════════════════════════════════

def _safe_clear(path):
    """安全重置信号文件：写入 null JSON 完成状态重置。"""
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
                    "stop_reason": "",
                    "stop_label": "",
                    "stop_detail": "",
                    "stop_manual": False,
                    "stop_at": "",
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
        "stop_reason": "",
        "stop_label": "",
        "stop_detail": "",
        "stop_manual": False,
        "stop_at": "",
    }



# ★ 跨会话去重缓存：统一台账中的 jobId 集合（进程内只算一次）
_DELIVERED_IDS_CACHE = None


def _delivered_ids():
    """返回累计台账里已投递成功/已失败的 jobId 集合（跨会话）"""
    global _DELIVERED_IDS_CACHE
    if _DELIVERED_IDS_CACHE is not None:
        return _DELIVERED_IDS_CACHE
    ids = set()
    try:
        from scripts import stats_core as _sc
        _st = _sc.compute(WORK_DIR)
        for _r in (_st.get("delivered") or []) + (_st.get("failed") or []):
            _jid = _r.get("job_id") or _r.get("jobId")
            if _jid:
                ids.add(_jid)
    except Exception as _e:
        print(f"[串行] 去重台账读取失败(降级为无去重): {_e}")
    _DELIVERED_IDS_CACHE = ids
    return ids


def invalidate_delivered_cache():
    """投递成功后调用，令下一条岗位能看到最新台账"""
    global _DELIVERED_IDS_CACHE
    _DELIVERED_IDS_CACHE = None


def save_serial_progress(progress):
    """保存串行进度"""
    with open(SERIAL_PROGRESS, 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
# P0-1 停机原因码：落库 + 停机闸门（禁止自动重试）
# ═══════════════════════════════════════════════════════════

def _halt(code, detail='', progress=None, fatal=True):
    """落库停机原因码并打印人话

    参数:
        code: stop_codes.STOP_CODES 中的原因码
        detail: 细节（命中词 / URL / 连续次数）
        progress: 已加载的进度 dict，不传则自行加载
        fatal: 是否同时置 fatal_stop（投递目标达成时为 False）
    """
    prog = progress if progress is not None else load_serial_progress()
    prog.update(new_stop_record(code, detail))
    if fatal:
        prog['fatal_stop'] = True
        prog['fatal_reason'] = detail
    save_serial_progress(prog)
    print(f"[停机] ★ 原因码: {stop_describe(code, detail)}")
    _hint = stop_hint(code)
    if _hint:
        print(f"[停机] 处置建议: {_hint}")
    if stop_is_manual(code):
        print("[停机] 该原因码需人工介入：确认页面/账号正常后执行"
              " python serial_loop.py --step init --ack-risk")
    return prog


def _halt_state():
    """读取停机状态

    返回 (blocked, code, detail)；blocked=True 时后续步骤必须被拦下
    """
    prog = load_serial_progress()
    code = prog.get('stop_reason') or ''
    if not code:
        if prog.get('fatal_stop'):
            return True, '', prog.get('fatal_reason', '')
        return False, '', ''
    if prog.get('stop_manual') or stop_is_manual(code) or prog.get('fatal_stop'):
        return True, code, prog.get('stop_detail', '')
    return False, code, prog.get('stop_detail', '')


def _halt_guard(step_name=''):
    """流水线闸门：已停机则拒绝执行当前步骤

    返回 True = 已停机，调用方必须立即返回（禁止任何自动重试）
    """
    blocked, code, detail = _halt_state()
    if not blocked:
        return False
    print(f"[停机] ⛔ 流水线处于停机状态，拒绝执行 {step_name or '当前步骤'}")
    if code:
        print(f"[停机] 原因码: {stop_describe(code, detail)}")
        _hint = stop_hint(code)
        if _hint:
            print(f"[停机] 处置建议: {_hint}")
    else:
        print(f"[停机] 历史停机原因: {detail or '未记录'}")
    print("[停机] 人工确认页面/账号正常后执行: "
          "python serial_loop.py --step init --ack-risk")
    return True


def ack_stop(note=''):
    """人工确认后清除停机标记（只由 --ack-risk 触发）"""
    prog = load_serial_progress()
    old = prog.get('stop_reason') or ''
    if not old and not prog.get('fatal_stop'):
        print("[停机] 当前无停机标记，无需清除")
        return False
    prog['fatal_stop'] = False
    prog['fatal_reason'] = ''
    for _k in ('stop_reason', 'stop_label', 'stop_detail', 'stop_at'):
        prog[_k] = ''
    prog['stop_manual'] = False
    prog['ack_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    prog['ack_note'] = note
    save_serial_progress(prog)
    print(f"[停机] 已人工确认，清除停机标记（原原因码: {old or '未记录'}）")
    return True


def load_jobs():
    """加载 jd_to_read.json"""
    if not os.path.exists(JD_TO_READ):
        return []
    with open(JD_TO_READ, 'r', encoding='utf-8') as f:
        jobs = json.load(f)
    # 空值守卫: 文件内容为 null / 非列表结构时一律降级为空列表，避免下游 len() 崩溃
    if isinstance(jobs, dict):
        jobs = jobs.get('jobs') or jobs.get('data') or []
    if not isinstance(jobs, list):
        return []
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
            print(f"[授权] 联系方式: {snap['wechat_id']}")
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
    # P1 统一口径：起点同样以 stats_core 累计成功数为准（旧口径读 delivered 列表，
    # 会让 page_delivered 恒等于累计总数，换词决策失真）
    try:
        from scripts import stats_core as _sc_base
        _base0 = int(_sc_base.compute()['cumulative']['success'])
    except Exception:
        _base0 = len(getattr(pm, 'delivered', []))
    page_state['page_start_delivered'] = _base0
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
    # P1 统一口径：起点同样以 stats_core 累计成功数为准（旧口径读 delivered 列表，
    # 会让 page_delivered 恒等于累计总数，换词决策失真）
    try:
        from scripts import stats_core as _sc_base
        _base0 = int(_sc_base.compute()['cumulative']['success'])
    except Exception:
        _base0 = len(getattr(pm, 'delivered', []))
    page_state['page_start_delivered'] = _base0
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

def build_page_window(total_cards=None):
    """★ 索引对齐核心: 从 surface_cache 重建"当前页面顺序"窗口

    原理:
        BOSS 推荐页左侧卡片按 DOM 顺序排列, surface_cache 按滚动顺序累计追加。
        最近 total_cards 条即"当前页面从头到尾的全部卡片", 其序位 1..N 就是
        read --index N 实际点击的卡片位置。

    约束:
        jd_to_read.json 必须与该窗口**等长同序**（被规则排除的条目留占位标 skip）,
        否则序号与卡片错位, 表现为点错岗位 / 右侧面板校验失败 / no_link_match。

    返回:
        dict: {"total_cards": n, "generated_at": str, "jobs": [...]} 或 None
    """
    cache = []
    if os.path.exists(SURFACE_CACHE):
        try:
            with open(SURFACE_CACHE, "r", encoding="utf-8") as f:
                cache = json.load(f) or []
        except Exception:
            cache = []
    if not cache:
        return None

    if total_cards is None:
        try:
            total_cards = int(get_state().get("total_cards") or 0)
        except Exception:
            total_cards = 0
    if not total_cards or total_cards > len(cache):
        total_cards = len(cache)

    tail = cache[-total_cards:]
    jobs = []
    for pos, j in enumerate(tail, start=1):
        jobs.append({
            "page_index": pos,
            "jobId": j.get("jobId", ""),
            "title": j.get("title", ""),
            "salary": j.get("salary", ""),
            "salary_max_k": j.get("salary_max_k") or job_filter.parse_salary_max_k(j.get("salary", "")),
            "company": j.get("company", ""),
            "industry": j.get("industry", ""),
            "city": j.get("city", ""),
        })

    window = {
        "total_cards": len(jobs),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "jobs": jobs,
    }
    with open(PAGE_WINDOW, "w", encoding="utf-8") as f:
        json.dump(window, f, ensure_ascii=False, indent=2)
    return window


def step_next_batch():
    """★ 中层循环: 滚动加载下一批50条 + 重建页面对齐窗口

    1. 检查页面是否已穷尽（穷尽 -> 提示 --step evolve）
    2. 未穷尽 -> 滚动加载一批 -> 增量提取 -> append 缓存
    3. ★ 滚动后立即用 surface_cache 尾部 total_cards 条重建页面对齐窗口
    4. 输出窗口清单（索引 = 页面卡片位置），提示 --step build-queue 生成对齐队列
    """
    if not health_check():
        print("ERROR: WebBridge 不可达")
        return False

    if is_exhausted():
        state = get_state()
        print(f"[分批] ★ 当前页面已穷尽: {state.get('exhaust_reason', '?')}")
        print(f"[分批] 累计提取 {state.get('total_extracted', 0)} 条")
        print(f"[分批] ★ 进入外层循环: python serial_loop.py --step evolve")
        return False

    result = scroll_next_batch(cache_file=SURFACE_CACHE)

    if result["new_count"] > 0:
        print(f"\n[分批] ═══ 第{result['batch_num']}批加载完成 ═══")
        print(f"[分批] 新增: {result['new_count']} 条 | 累计提取: {result['total_extracted']} 条")
        print(f"[分批] 页面总卡片: {result['total_cards']} 条 | 穷尽: {result['exhausted']}")

        page_state = load_page_state()
        page_type = page_state.get('page_type', 'recommend')
        current_kw = page_state.get('current_keyword', '')

        # ★ 索引对齐：滚动后立即重建窗口
        window = build_page_window(result["total_cards"])
        if window:
            print(f"\n{'='*62}")
            print(f"页面对齐窗口 (索引 = 页面卡片位置, 共{window['total_cards']}条)")
            print(f"页面类型: {page_type}" + (f" | 关键词: {current_kw}" if current_kw else ""))
            print(f"{'='*62}")
            for j in window["jobs"]:
                print(f"  [{j['page_index']:>3}] {j['title'][:25]:<25} {j['salary'][:12]:<12} "
                      f"{j['company'][:15]:<15} {j['industry'][:12]}")
            print(f"\n[分批] ★ 窗口已写入 page_window.json（索引对齐基准）")
        else:
            print(f"\n[分批] ⚠ 缓存为空，无法建立页面对齐窗口，请重新 --step init")

        print(f"\n[分批] ★ 下一步: python serial_loop.py --step build-queue")
        print(f"[分批]   （自动生成与页面等长同序的 jd_to_read.json，含规则层粗筛）")

        progress = load_serial_progress()
        progress["batch_num"] = result["batch_num"]
        progress["total"] = result["total_extracted"]
        save_serial_progress(progress)
    else:
        print(f"[分批] 本批无新岗位")
        if result["exhausted"]:
            print(f"[分批] ★ 页面已穷尽，进入外层循环: python serial_loop.py --step evolve")

    return result["new_count"] > 0


def step_build_queue(limit=0):
    """★ 生成页面对齐队列 jd_to_read.json（规则层粗筛 + 等长占位）

    关键设计:
        队列长度 == 窗口长度, 规则层 REJECT 的条目不清除, 只标 skip + 理由。
        => jd_to_read.json 的第 N 条永远等于页面第 N 张卡片, 索引不再错位。

    输出:
        jd_to_read.json  每条含 page_index / page_order / rule_verdict / rule_reason / skip
    """
    window = None
    if os.path.exists(PAGE_WINDOW):
        try:
            with open(PAGE_WINDOW, "r", encoding="utf-8") as f:
                window = json.load(f)
        except Exception:
            window = None
    if not window or not window.get("jobs"):
        window = build_page_window()
    if not window or not window.get("jobs"):
        print("ERROR: 无法建立页面对齐窗口（page_window.json 为空）")
        print("★ 请先运行 --step next-batch 采集当前页面")
        return False

    queue, stats = [], {"PASS": 0, "REVIEW": 0, "REJECT": 0}
    rejects, reviews = [], []
    for j in window["jobs"]:
        r = job_filter.rule_screen(j)
        stats[r["verdict"]] = stats.get(r["verdict"], 0) + 1
        item = dict(j)
        item["page_order"] = j["page_index"] - 1
        item["rule_verdict"] = r["verdict"]
        item["rule_reason"] = r["reason"]
        item["rule_name"] = r["rule"]
        item["skip"] = (r["verdict"] == "REJECT")
        queue.append(item)
        if r["verdict"] == "REJECT":
            rejects.append(item)
        elif r["verdict"] == "REVIEW":
            reviews.append(item)

    if limit and limit > 0:
        queue = queue[:limit]

    with open(JD_TO_READ, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

    print(f"\n[队列] 页面对齐队列已生成: {len(queue)} 条（与页面卡片等长同序）")
    print(f"[队列] 规则层结果: PASS {stats['PASS']} | REVIEW {stats['REVIEW']} | REJECT {stats['REJECT']}")
    print(f"[队列] ★ 索引 N 已锁定为页面第 N 张卡片，逐条 read 期间禁止再调 next-batch")

    if rejects:
        print(f"\n[队列] 规则硬排除（skip，read 时自动跳过，不消耗读取动作）:")
        for it in rejects[:12]:
            print(f"  [{it['page_index']:>3}] {it['title'][:24]:<24} <- {it['rule_reason']}")
        if len(rejects) > 12:
            print(f"  ... 共 {len(rejects)} 条")

    if reviews:
        print(f"\n[队列] 需语义层判定（规则无法断言，AI 逐条 read 后决定）:")
        for it in reviews[:15]:
            print(f"  [{it['page_index']:>3}] {it['title'][:24]:<24} {it['industry'][:10]:<10} <- {it['rule_reason']}")
        if len(reviews) > 15:
            print(f"  ... 共 {len(reviews)} 条")

    print(f"\n[队列] ★ 下一步: python serial_loop.py --step read --index 1")
    return True



# ═══════════════════════════════════════════════════════════
# ★ 内层循环: 岗位级
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
# ★ 读取前索引对齐（P0 治本：页面漂移不靠序号硬点）
# ═══════════════════════════════════════════════════════════

def _dom_cards():
    """取当前推荐页真实卡片顺序；失败返回 []"""
    try:
        from scripts.webbridge_client import extract_jobs as _ej
        return _ej() or []
    except Exception as e:
        print(f"[对齐] 警告: 当前页面快照失败({e})，跳过对齐校验")
        return []


def _sync_queue_positions(jobs):
    """把校正后的 page_index/page_order 回写 jd_to_read.json 与 page_window.json"""
    try:
        with open(JD_TO_READ, "w", encoding="utf-8") as f:
            json.dump(jobs, f, ensure_ascii=False, indent=2)
        win = {"total_cards": len(jobs),
               "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "jobs": [{k: j.get(k, "") for k in
                         ("page_index", "jobId", "title", "salary", "company", "industry", "city")}
                        for j in jobs]}
        with open(PAGE_WINDOW, "w", encoding="utf-8") as f:
            json.dump(win, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[对齐] 警告: 队列回写失败 {e}")


def align_queue_index(jobs, index):
    """读取前按当前 DOM 真实卡片顺序校正队列索引

    规范：
      1) 队列第 N 条的 jobId 在当前页面命中 → 把 page_index 校正为真实卡片位
      2) jobId 未命中但标题+公司可对上 → 用当前卡片的 jobId 回填并重定位
      3) 完全失联 → 标 stale，调用方落 REJECT 跳过，绝不猜测、绝不误投
    返回: (ok: bool, note: str)
    """
    if index < 1 or index > len(jobs):
        return True, ""
    cur = jobs[index - 1]
    jid = cur.get("jobId", "")
    cards = _dom_cards()
    if not cards:
        return True, ""
    ids = [c.get("jobId", "") for c in cards]
    if jid and jid in ids:
        pos = ids.index(jid) + 1
        if cur.get("page_index") != pos:
            cur["page_index"] = pos
            cur["page_order"] = pos - 1
            _sync_queue_positions(jobs)
            return True, f"索引校正: 第 {index} 条实位于页面第 {pos} 卡，队列已重写"
        return True, ""
    title = (cur.get("title") or "").strip()
    company = (cur.get("company") or "").strip()
    for pos, c in enumerate(cards, start=1):
        if title and (c.get("title") or "").strip() == title and \
           (not company or (c.get("company") or "").strip() == company):
            cur["jobId"] = c.get("jobId", "")
            cur["page_index"] = pos
            cur["page_order"] = pos - 1
            if not cur.get("salary"):
                cur["salary"] = c.get("salary", "")
            if not cur.get("city"):
                cur["city"] = c.get("city", "")
            _sync_queue_positions(jobs)
            return True, f"★ 漂移重定位: 「{title[:16]}」jobId 已回填为当前第 {pos} 卡"
    cur["stale"] = True
    return False, f"★ 岗位失联: 「{title[:16]}」不在当前页面（已被剔除/重排），跳过不猜测"


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

    # ★ P0 治本：读取前用当前 DOM 对齐索引（页面漂移不再靠序号硬点）
    _align_ok, _align_note = align_queue_index(jobs, index)
    if _align_note:
        print(f"[对齐] {_align_note}")
    if not _align_ok:
        with open(DECISION_FILE, 'w', encoding='utf-8') as _f:
            json.dump({'decision': 'REJECT', 'reason': '岗位已不在当前页面（漂移失联），跳过不猜测'},
                      _f, ensure_ascii=False, indent=2)
        _pg = load_serial_progress()
        _pg['current_index'] = index
        _pg['total'] = len(jobs)
        save_serial_progress(_pg)
        print("★ 已自动写入 decision.json = REJECT（岗位失联）")
        print(f"★ 下一步: python serial_loop.py --step decide --index {index}")
        return True
    jid = job.get('jobId', '')
    title = job.get('title', '')
    company = job.get('company', '')
    salary = job.get('salary', '')
    page_order = job.get('page_order', '?')

    # ★ 跨会话去重：统一台账（jobId 去重）里已投递成功的岗位不再重复投递
    if jid and jid in _delivered_ids():
        print(f"[串行] 已投递过，跳过重复投递: {title} | {company}")
        with open(DECISION_FILE, 'w', encoding='utf-8') as _f:
            json.dump({'decision': 'REJECT', 'reason': '重复投递拦截: 该 jobId 已在累计台账中投递成功'},
                      _f, ensure_ascii=False, indent=2)
        _pg = load_serial_progress()
        _pg['current_index'] = index
        _pg['total'] = len(jobs)
        save_serial_progress(_pg)
        print("★ 已自动写入 decision.json = REJECT（重复投递拦截）")
        print(f"★ 下一步: python serial_loop.py --step decide --index {index}")
        return True

    # ★ 规则层已硬排除的岗位：不读 JD，直接落 REJECT（索引占位保持对齐）
    if job.get('skip'):
        _why = job.get('rule_reason') or '规则层硬排除'
        print(f"[串行] 规则排除，跳过读取: {title} | {company}")
        print(f"[串行] 排除理由: {_why}")
        with open(DECISION_FILE, 'w', encoding='utf-8') as _f:
            json.dump({'decision': 'REJECT', 'reason': f'规则排除: {_why}'},
                      _f, ensure_ascii=False, indent=2)
        _pg = load_serial_progress()
        _pg['current_index'] = index
        _pg['total'] = len(jobs)
        save_serial_progress(_pg)
        print(f"★ 已自动写入 decision.json = REJECT")
        print(f"★ 下一步: python serial_loop.py --step decide --index {index}")
        return True

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


def step_write_decision(decision, reason='', sub_json='', evidence_json='', sub_file='', evidence_file=''):
    """★ v5.7 新增: AI 通过命令行直接覆写 decision.json

    目的: 统一由脚本覆写 decision.json，保证内容始终是最新一轮决策结果。
    用法: python serial_loop.py --step write-decision --decision APPROVE --reason "理由"
    脚本内部使用 open('w') 直接覆写，幂等安全。
    """
    decision = str(decision).upper()
    if decision not in ('APPROVE', 'REJECT'):
        print(f"ERROR: --decision 必须是 APPROVE 或 REJECT，实际: {decision}")
        return False

    # ★ v5.8 结构化决策：固定结构 + 四项子判定 + 证据 + 提示词版本
    # 结构固定后，取证门才能逐项比对 evidence 与 JD 原文；缺字段不再靠自由文本猜
    data = {"decision": decision, "reason": reason}

    def _read_json_file(path, label):
        if not path:
            return ''
        try:
            with open(path, "r", encoding="utf-8") as _f:
                raw = _f.read()
            json.loads(raw)  # 合法性预检
            return raw
        except Exception as _e:
            print(f"[决策] ⚠ {label} 文件不可用({path}): {_e}")
            return ""

    def _parse_json_arg(raw, label):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception as _e:
            print(f"[决策] ⚠ {label} 不是合法 JSON，已忽略: {_e}")
            return None

    # 文件方式优先：Windows PowerShell 下内联 JSON 的双引号会被吞，
    # 曾表现为"静默降级成无结构决策"，因此提供 --sub-file / --evidence-file
    _sub = _parse_json_arg(_read_json_file(sub_file, '--sub-file') or sub_json, '--sub-json')
    _ev = _parse_json_arg(_read_json_file(evidence_file, '--evidence-file') or evidence_json, '--evidence-json')
    try:
        from scripts import rules_loader as _rl
        data['prompt_version'] = _rl.prompt_version()
        data['rules_version'] = _rl.status().get('version', '')
    except Exception:
        data['prompt_version'] = 'unknown'
        data['rules_version'] = 'unknown'
    data['decided_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    if _sub:
        data['sub'] = _sub
    if _ev:
        data['evidence'] = _ev

    # 写入前做一次结构预检（只提示，不阻断：shadow 阶段先看数据质量）
    try:
        _missing = decision_schema.missing_fields(data)
        if _missing:
            print(f"[决策] · 结构预检: 缺字段 {_missing}（取证门将记为结构不完整）")
    except Exception:
        pass

    # 覆写 decision.json，写入本轮决策
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

    # ★ v5.8 取证门：结构校验 + evidence 与 JD 原文比对
    # shadow（默认）：只记录不拦截，产出一周「无证据 APPROVE 占比」；enforce：证据不合格强制改判
    try:
        _jd_text = ''
        if os.path.exists(CURRENT_JD):
            with open(CURRENT_JD, 'r', encoding='utf-8') as _jf:
                _jd_text = (json.load(_jf) or {}).get('jd_text', '') or ''
        _gate = decision_schema.validate(
            decision_data, _jd_text,
            audit_dir=WORK_DIR,
            job_id=str(decision_data.get('jobId', '') or ''),
        )
        if not _gate['ok']:
            _errs = '; '.join(_gate['errors'])[:160]
            print(f"[串行] ⚠ 取证门未通过({_gate['mode']}): {_errs}")
            if _gate.get('forced_decision'):
                decision = _gate['forced_decision']
                reason = f"取证门拦截: {'; '.join(_gate['errors'])[:120]}"
                decision_data['decision'] = decision
                decision_data['reason'] = reason
                with open(DECISION_FILE, 'w', encoding='utf-8') as _f:
                    json.dump(decision_data, _f, ensure_ascii=False, indent=2)
                print("[串行] ⚠ APPROVE 被取证门强制改判 REJECT")
        elif _gate.get('warnings'):
            print(f"[串行] · 取证门观察{len(_gate['warnings'])}项: {_gate['warnings'][0][:120]}")
    except Exception as _e:
        print(f"[串行] WARN 取证门异常(不影响主流程): {_e}")

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
    # ★ 去重第三层：该 jobId 已在累计台账中投递成功，禁止二次投递
    if decision == 'APPROVE' and jid and jid in _delivered_ids():
        print("[串行] ⚠ 去重拦截: 该岗位已在累计台账中投递成功，禁止重复投递")
        decision = 'REJECT'
        reason = '重复投递拦截: 累计台账已存在该 jobId 的投递记录'
        with open(DECISION_FILE, 'w', encoding='utf-8') as _f:
            json.dump({'decision': 'REJECT', 'reason': reason}, _f, ensure_ascii=False, indent=2)

    # ★ 判定精度第二层：规则硬排除的岗位，AI 无权放行
    if decision == 'APPROVE':
        _r = job_filter.rule_screen(job)
        if _r['verdict'] == 'REJECT':
            print(f"[串行] ⚠ 规则拦截: {_r['reason']}")
            print(f"[串行] ⚠ APPROVE 被强制改判 REJECT（规则层优先）")
            decision = 'REJECT'
            reason = f"规则拦截: {_r['reason']}"
            with open(DECISION_FILE, 'w', encoding='utf-8') as _f:
                json.dump({'decision': 'REJECT', 'reason': reason}, _f, ensure_ascii=False, indent=2)

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
        invalidate_delivered_cache()

        # ★ P0-1 风控命中 / 登录失效：立即停机，禁止清障复投与任何自动重试
        if status in ('risk_control', 'login_expired'):
            _code = 'risk_control' if status == 'risk_control' else 'auth_fail'
            pm0 = ProgressManager(PROGRESS_FILE)
            pm0.load()
            pm0.add_failed(jid, job, f"{status}: {detail}")
            pm0.save()
            if index not in progress.get('processed', []):
                progress.setdefault('processed', []).append(index)
            progress.setdefault('results', []).append(result_entry)
            progress['target_met'] = check_delivery_target(PROGRESS_FILE)['target_met']
            print(f"[串行] ★★★ 命中 {status}，立即停机（不做清障复投，不自动重试）★★★")
            _halt(_code, detail, progress)
            print(f"[串行] ★ 自动生成投递汇总...")
            step_finish()
            return True

        # comm_limit 先清障再复投一次——多数 comm_limit 是叠加遮罩挡住点击的假失败
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
            progress['target_met'] = target_check['target_met']
            # P0-1: 停机原因码落库
            _halt('daily_limit', detail, progress)
            # ★ 自动触发 finish，确保 100% 生成投递汇总页面
            print(f"[串行] ★ 自动生成投递汇总...")
            step_finish()
            return True

        # 单条 comm_limit 不阻断（120 是提示，150 才是硬顶，硬顶由 fatal_limit 拦）；
        # deliver_inplace 已做 preflight 清障 + 清障后复投，仍连续多条 comm_limit 说明遮罩清不干净
        # 或平台真封顶，继续投只会把假失败刷满进度文件，故做连续熔断并提示人工确认。
        if status == 'comm_limit':
            progress['consecutive_comm_count'] = progress.get('consecutive_comm_count', 0) + 1
            print(f"[串行] ★ 沟通上限提示(120次): 清障复投仍失败 "
                  f"(连续 {progress['consecutive_comm_count']}/{MAX_CONSECUTIVE_COMM})")
            progress['target_met'] = target_check['target_met']
            if progress['consecutive_comm_count'] >= MAX_CONSECUTIVE_COMM:
                # P0-1: 停机原因码落库（人工介入类）
                _halt('comm_limit',
                      f"连续{MAX_CONSECUTIVE_COMM}次comm_limit，疑似页面遮罩残留或已达平台上限",
                      progress)
                print(f"[串行] ★★★ 连续 {MAX_CONSECUTIVE_COMM} 次 comm_limit，判定遮罩残留或真封顶，"
                      f"停止流水线；请人工确认页面状态后再 init ★★★")
                print(f"[串行] ★ 自动生成投递汇总...")
                step_finish()
                return True
            save_serial_progress(progress)
            # 不设 fatal_stop，不 finish，继续下一条投递
            return True

        # ★ 页面结构异常熔断（按钮/卡片定位失败，多为 BOSS 改版）
        if status == 'selector_broken':
            progress['consecutive_selector_count'] = progress.get('consecutive_selector_count', 0) + 1
            print(f"[串行] ★ 页面结构异常计数: "
                  f"{progress['consecutive_selector_count']}/{MAX_CONSECUTIVE_SELECTOR}")
            progress['target_met'] = target_check['target_met']
            if progress['consecutive_selector_count'] >= MAX_CONSECUTIVE_SELECTOR:
                _halt('selector_broken',
                      f"连续{MAX_CONSECUTIVE_SELECTOR}次找不到沟通按钮/职位卡片，疑似BOSS改版",
                      progress)
                print(f"[串行] ★ 自动生成投递汇总...")
                step_finish()
                return True
            save_serial_progress(progress)
            return True
        else:
            progress['consecutive_selector_count'] = 0

        # ★ 连续 unknown 状态熔断检测
        if status == 'unknown':
            progress['consecutive_unknown_count'] = progress.get('consecutive_unknown_count', 0) + 1
            print(f"[串行] ★ 连续 unknown 计数: {progress['consecutive_unknown_count']}/{MAX_CONSECUTIVE_UNKNOWN}")
            if progress['consecutive_unknown_count'] >= MAX_CONSECUTIVE_UNKNOWN:
                print(f"[串行] ★★★ 连续 {MAX_CONSECUTIVE_UNKNOWN} 次投递结果 unknown，疑似每日上限或页面异常，熔断停止 ★★★")
                progress['target_met'] = target_check['target_met']
                # P0-1: 停机原因码落库
                _halt('consecutive_unknown',
                      f"连续{MAX_CONSECUTIVE_UNKNOWN}次投递结果unknown，疑似每日上限", progress)
                print(f"[串行] ★ 自动生成投递汇总...")
                step_finish()
                return True
        else:
            # 非 unknown（success / fail 等），重置计数器
            progress['consecutive_unknown_count'] = 0

        if target_check["target_met"]:
            print(f"\n[串行] ★★★ 投递目标 {DAILY_TARGET} 份已达成！★★★")
            progress['target_met'] = True
            # P0-1: 目标达成同样落原因码（非故障，fatal=False）
            _halt('target_reached',
                  f"已投递 {target_check.get('delivered_count', DAILY_TARGET)} 份"
                  f"，达成目标 {DAILY_TARGET}", progress, fatal=False)
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
    """★ 流程图: 汇总投递岗位 → 生成HTML汇报（授权状态 + 联系方式区）

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
            print(f"[完成] 联系方式: {snap['wechat_id']}")

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
        # P0-1: 优先展示停机原因码（代码 + 人话 + 处置建议）
        if progress.get('stop_reason'):
            print(f"停机原因码: {progress['stop_reason']}")
            print(f"停止原因: {stop_describe(progress['stop_reason'], progress.get('stop_detail', ''))}")
            _hint = stop_hint(progress['stop_reason'])
            if _hint:
                print(f"处置建议: {_hint}")
        elif progress.get('fatal_stop'):
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
    jobs = load_jobs() or []
    batch_state = get_state()
    page_state = load_page_state()
    target_check = check_delivery_target(PROGRESS_FILE)
    # ★ 统一统计口径：所有对外数字（status / HTML / finish）只从 stats_core 出
    _stats = stats_core.compute(WORK_DIR)
    stats_core.save_unified(_stats)
    _cum = _stats["cumulative"]
    _ses = _stats["session"]

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
        print(f"联系方式    : {snap['wechat_id']}")

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
    _rej_rule = sum(1 for r in rejected
                    if str(r.get('reason', '')).startswith(('规则排除', '规则拦截'))
                    or '重复投递' in str(r.get('reason', '')))
    _rej_sem = len(rejected) - _rej_rule
    print(f"│   REJECT: {len(rejected)}（规则层自动排除 {_rej_rule} / 语义层判定拒绝 {_rej_sem}）")
    print(f"│   本次会话投递成功: {_ses['success']}")
    print(f"│ 剩余: {total - len(processed)}")

    # 目标
    print(f"│")
    print(f"├─ 投递目标 ──────────────────────────────")
    print(f"│ 目标: {target_check['target']} 份")
    print(f"│ 已投递(累计去重): {_cum['success']} 份  <- 与 投递汇总.html 同源")
    print(f"│ 本次会话: {_ses['success']} 份")
    print(f"│ 失败(累计): {_cum['failed']} 份")
    print(f"│ 排除(累计): {_cum['rejected']} 份")
    print(f"│ 剩余: {max(0, target_check['target'] - _cum['success'])} 份")
    print(f"│ 达标: {'是' if _cum['success'] >= target_check['target'] else '否'}")
    if _stats['conflicts']:
        print(f"│ 口径提示: {_stats['conflicts']} 条在两源状态冲突，已按 success>failed 取优")
    print(f"│ 统一台账: {stats_core.UNIFIED_FILE}")

    # 停止状态
    if progress.get('stop_reason'):
        print(f"│")
        print(f"├─ ⛔ 停机状态 ────────────────────────────")
        print(f"│ 原因码: {progress['stop_reason']}")
        print(f"│ 说明: {stop_describe(progress['stop_reason'], progress.get('stop_detail', ''))}")
        _hint = stop_hint(progress['stop_reason'])
        if _hint:
            print(f"│ 建议: {_hint}")
        if stop_is_manual(progress['stop_reason']):
            print(f"│ 清除标记: python serial_loop.py --step init --ack-risk")
    elif progress.get('fatal_stop'):
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
    --step finish                  生成HTML汇报 + 授权状态 + 联系方式区
    --step status                  查看三层完整进度 + 授权状态

激活（独立 CLI，不在本控制器内）:
    python scripts/entitlement.py --activate <CODE>
    python scripts/entitlement.py --status
    python scripts/cloud_client.py --sync
        """)
    # ★ v5.8 启动自检：离线评测代码不得进入生产链路；规则真源必须可加载
    try:
        from scripts import selfcheck as _sc
        _sc.assert_clean()
    except Exception as _e:
        print(f"[自检] ⚠ {_e}")

    # ★ 26.11.19 启动静默补同步：已激活用户每 24h 最多尝试一次。
    #   只有「真的换到新版本」才出声；断网 / 云端未更新 / 校验不通过一律静默沿用本地，
    #   不给买家制造无意义告警（授权状态里的 degraded 提示仍会兜底提醒）。
    try:
        _ar = cc.auto_sync_if_stale()
        if _ar.get("changed"):
            print(f"[指令包] 云端已更新 {_ar['from']} → {_ar['to']}")
    except Exception:
        pass

    parser.add_argument("--step", required=True,
                        choices=['init', 'next-batch', 'build-queue', 'read', 'write-decision',
                                 'decide', 'evolve', 'navigate-search', 'add-keyword',
                                 'finish', 'status'],
                        help="执行步骤")
    parser.add_argument("--index", type=int, default=0,
                        help="处理第几条 (read/decide时需要)")
    parser.add_argument("--decision", default="",
                        help="AI决策 APPROVE/REJECT (write-decision时需要)")
    parser.add_argument("--reason", default="",
                        help="AI决策理由 (write-decision时可选)")
    parser.add_argument("--sub-json", default="", dest="sub_json",
                        help='四项子判定 JSON，如 {"location":{"verdict":"PASS","evidence":"..."},...}')
    parser.add_argument("--evidence-json", default="", dest="evidence_json",
                        help='证据映射 JSON，如 {"salary":"JD原文片段",...}')
    parser.add_argument("--sub-file", default="", dest="sub_file",
                        help='四项子判定 JSON 文件路径（Windows 下推荐，避免引号被吞）')
    parser.add_argument("--evidence-file", default="", dest="evidence_file",
                        help='证据映射 JSON 文件路径（Windows 下推荐）')
    parser.add_argument("--keyword", default="",
                        help="搜索关键词 (navigate-search/add-keyword时需要)")
    parser.add_argument("--city", default=_PROFILE_CITY,
                        help="城市编码 (默认从 user_profile.json 读取)")
    parser.add_argument("--ack-risk", action="store_true", dest="ack_risk",
                        help="人工确认风控页/限流已解除，清除停机标记（仅 --step init 生效）")
    args = parser.parse_args()

    # ★ P0-1 停机闸门：停机后除 finish/status 外一律拒绝执行；
    # 只有人工显式 --ack-risk 才放行，杜绝脚本自动重试风控账号
    if args.step not in ('finish', 'status'):
        if args.step == 'init' and args.ack_risk:
            ack_stop('--ack-risk 人工确认')
        elif _halt_guard(f"--step {args.step}"):
            sys.exit(2)

    if args.step == 'init':
        # 门禁未放行必须以非零码退出，否则 Agent 会误判 init 成功继续往下跑
        if not step_init(args.city):
            sys.exit(1)

    elif args.step == 'next-batch':
        step_next_batch()

    elif args.step == 'build-queue':
        step_build_queue(limit=args.index if args.index > 0 else 0)

    elif args.step == 'read':
        if args.index < 1:
            print("ERROR: --index 必须 >= 1")
            sys.exit(1)
        step_read(args.index)

    elif args.step == 'write-decision':
        if not args.decision:
            print("ERROR: 需要 --decision 参数 (APPROVE/REJECT)")
            sys.exit(1)
        step_write_decision(args.decision, args.reason,
                            getattr(args, 'sub_json', ''), getattr(args, 'evidence_json', ''),
                            getattr(args, 'sub_file', ''), getattr(args, 'evidence_file', ''))

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

#!/usr/bin/env python3
"""
模块 C: 推荐页导航 + 分批懒加载

核心改动 — 分批滚动+提取:
  不再一口气滚到底再提取，而是分批处理:
  1. 每批滚动加载约 BATCH_SIZE 条岗位（约2-3次滚动）
  2. 立刻增量提取本批新岗位 → append 到 surface_cache.json
  3. 输出本批岗位列表供 AI 筛选 + 串行投递
  4. 自动继续下一批滚动，直到穷尽或达到每日投递上限

  ★ 分批不等于批量投递 — 每批内的投递仍然严格串行（serial_loop.py 控制）

状态持久化 — 写死连续滚动规则:
  serial_loop.py 每批以独立 CLI 进程调用本模块，模块级 _state 会重置。
  必须将分批进度持久化到 batch_state.json，否则第二批会从 page_order=0
  重新开始，等于"第一批结束后就忘了向下还能滚动"。
  → _load_state() 在每次操作前恢复进度
  → _save_state() 在每次修改后持久化

优势:
  - 快进快出: 第一批50条2分钟内即可进入筛选投递
  - 提前止损: 如果投到每日上限，直接停手不再滚动
  - 页面稳定: 每批卡片在视口内，不会因虚拟滚动消失
  - 数据量可控: AI每次只分析50-80条
  - ★ 跨进程记忆: batch_state.json 确保第二批从第一批结束处继续

独立可运行:
    python recommend_loader.py --city 101210100

也可被其他模块 import:
    from scripts.recommend_loader import navigate_to_recommend, scroll_next_batch, is_exhausted
    navigate_to_recommend("101210100")
    batch = scroll_next_batch()  # 返回本批新岗位列表
"""

import json
import time
import random
import sys
import os

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.profile_loader import WORK_DIR, CITY_CODE as _PROFILE_CITY

from scripts.webbridge_client import (
    SESSION, navigate, evaluate, scroll_page,
    ensure_active_tab, health_check, extract_jobs, parse_salary_max,
)
from scripts.surface_cache import KEEP_FIELDS, append_cache

# ═══════════════════════════════════════════════════════════
# 配置常量
# ═══════════════════════════════════════════════════════════

RECOMMEND_URL = "https://www.zhipin.com/web/geek/jobs?ka=header-jobs"

# 分批参数
BATCH_SIZE = 50           # 每批目标岗位数（达到此数量即停止滚动，进入提取）
MAX_SCROLLS_PER_BATCH = 8 # 每批最多滚动次数
RENDER_WAIT = 3           # 滚动后等待渲染秒数

# 穷尽检测
MAX_TOTAL_SCROLLS = 80    # 全局最大滚动次数（安全上限）
STABLE_THRESHOLD = 4      # 连续 N 次无新增 → 穷尽

# 跨进程状态持久化文件
#   serial_loop.py 每次以独立 CLI 进程调用，模块级 _state 会重置。
#   必须将分批进度持久化到文件，否则第二批会从 page_order=0 重新开始。
BATCH_STATE_FILE = os.path.join(WORK_DIR, "batch_state.json")

# ═══════════════════════════════════════════════════════════
# 模块级状态 — 跟踪分批进度
# ═══════════════════════════════════════════════════════════

_state = {
    "total_cards": 0,       # 当前页面总卡片数
    "total_extracted": 0,   # 已提取的岗位数
    "total_scrolls": 0,     # 累计滚动次数
    "exhausted": False,     # 是否已穷尽
    "exhaust_reason": "",   # 穷尽原因
    "batch_num": 0,         # 当前批次号
}


def _load_state():
    """★ v4.4 从文件加载分批状态（跨进程持久化）

    serial_loop.py 每批以独立进程调用本模块，模块级 _state 会重置为默认值。
    必须在每次操作前从 batch_state.json 恢复进度，否则:
      - 第二批会从 page_order=0 重新提取（重复）
      - exhausted 标志丢失（已穷尽的页面会再次尝试滚动）
      - total_scrolls 重置（全局上限保护失效）
    """
    global _state
    if os.path.exists(BATCH_STATE_FILE):
        try:
            with open(BATCH_STATE_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                _state.update(saved)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[状态] 加载 batch_state.json 失败: {e}，使用默认状态")
    return _state


def _save_state():
    """★ v4.4 保存分批状态到文件（跨进程持久化）

    每次修改 _state 后必须调用，确保下一批进程能恢复进度。
    """
    try:
        with open(BATCH_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f"[状态] 保存 batch_state.json 失败: {e}")


def navigate_to_recommend(city_code=_PROFILE_CITY):
    """导航到 BOSS直聘 推荐页

    参数:
        city_code: 城市编码 (如 101010100 = 北京)

    返回:
        bool: 导航是否成功
    """
    url = RECOMMEND_URL
    # 导航前先确保独立标签页（不抢用户当前页面）
    if not ensure_active_tab():
        print(f"[导航] ERROR: 无法建立独立 BOSS 标签页")
        return False
    print(f"[导航] {url}")
    navigate(url, wait_range=(8, 10))

    current_url = evaluate("window.location.href") or ""
    if "jobs" not in current_url and "ka=header-jobs" not in current_url:
        print(f"[警告] 当前URL可能不在推荐页: {current_url}")
        return False

    time.sleep(2)

    card_count = _count_cards()
    if card_count == 0:
        print("[警告] 推荐页无岗位卡片，可能页面未渲染完成")
        time.sleep(3)
        card_count = _count_cards()

    print(f"[导航] 推荐页已加载，初始卡片数: {card_count}")
    _state["total_cards"] = card_count
    _save_state()  # ★ 持久化
    return card_count > 0


def _count_cards():
    """统计当前页面岗位卡片数量"""
    result = evaluate("""
    (function(){
        var cards = document.querySelectorAll('.job-card-wrap');
        return cards.length.toString();
    })()
    """) or "0"
    try:
        return int(result)
    except (ValueError, TypeError):
        return 0


def _check_end_marker():
    """检测页面底部的"没有更多了"等文案"""
    result = evaluate("""
    (function(){
        var bodyText = document.body.innerText || '';
        var markers = [
            '没有更多了', '已经到底', '没有更多', '到底了',
            '已加载全部', '暂无更多', 'No more'
        ];
        for (var i = 0; i < markers.length; i++) {
            if (bodyText.indexOf(markers[i]) >= 0) return 'true';
        }
        return 'false';
    })()
    """) or "false"
    return result == "true"


def _extract_new_jobs(from_index):
    """增量提取新岗位（从 from_index 开始的岗位）

    参数:
        from_index: 之前已提取的数量，新岗位从此索引开始

    返回:
        list[dict]: 新岗位列表（精简字段 + page_order）
    """
    jobs = extract_jobs()

    # 去重
    seen = set()
    unique_jobs = []
    for job in jobs:
        jid = job.get('jobId', '')
        if jid and jid not in seen:
            seen.add(jid)
            unique_jobs.append(job)

    # 计算薪资上限
    for job in unique_jobs:
        salary_str = job.get('salary', '')
        job['salary_max_k'] = parse_salary_max(salary_str)

    _state["total_cards"] = len(unique_jobs)

    # 只取 from_index 之后的新岗位
    if len(unique_jobs) <= from_index:
        return []

    new_jobs = unique_jobs[from_index:]
    for i, job in enumerate(new_jobs):
        job['page_order'] = from_index + i

    # 精简字段
    slim_jobs = []
    for job in new_jobs:
        slim = {k: job.get(k, '') for k in KEEP_FIELDS}
        slim_jobs.append(slim)

    return slim_jobs


def scroll_next_batch(cache_file="surface_cache.json"):
    """滚动加载下一批岗位并增量提取

    ★ 分批核心函数:
      1. 滚动直到新增岗位数 >= BATCH_SIZE 或达到穷尽/上限
      2. 增量提取新岗位
      3. append 到缓存文件
      4. 返回本批岗位列表

    跨进程安全:
      进入函数时先 _load_state() 恢复上一批的进度，
      确保第二批从第一批结束的 page_order 继续向下滚动。

    参数:
        cache_file: 缓存文件路径（增量追加）

    返回:
        dict: {
            "batch_num": int,        # 批次号
            "new_jobs": list[dict],  # 本批新岗位
            "new_count": int,        # 本批新岗位数
            "total_extracted": int,  # 累计提取数
            "total_cards": int,      # 页面总卡片数
            "exhausted": bool,       # 是否已穷尽
            "exhaust_reason": str,   # 穷尽原因
        }
    """
    # 跨进程恢复 — 从文件加载上一批的进度
    _load_state()

    if _state["exhausted"]:
        print(f"[分批] 已穷尽({_state['exhaust_reason']})，不再滚动")
        print(f"[分批] 累计提取 {_state['total_extracted']} 条，总滚动 {_state['total_scrolls']} 次")
        return {
            "batch_num": _state["batch_num"],
            "new_jobs": [],
            "new_count": 0,
            "total_extracted": _state["total_extracted"],
            "total_cards": _state["total_cards"],
            "exhausted": True,
            "exhaust_reason": _state["exhaust_reason"],
        }

    _state["batch_num"] += 1
    batch_num = _state["batch_num"]
    from_index = _state["total_extracted"]

    print(f"\n[分批] === 第{batch_num}批 === 从 page_order={from_index} 开始")
    print(f"[分批] 目标: 新增 {BATCH_SIZE} 条 (最多滚动 {MAX_SCROLLS_PER_BATCH} 次)")
    print(f"[分批] 累计进度: 已提取{_state['total_extracted']}条, 已滚动{_state['total_scrolls']}次")

    stable_count = 0
    batch_scrolls = 0

    while batch_scrolls < MAX_SCROLLS_PER_BATCH:
        # 检查全局上限
        if _state["total_scrolls"] >= MAX_TOTAL_SCROLLS:
            _state["exhausted"] = True
            _state["exhaust_reason"] = f"达到全局滚动上限{MAX_TOTAL_SCROLLS}次"
            print(f"[分批] {_state['exhaust_reason']}")
            _save_state()  # ★ 持久化
            break

        # 检查底部标记
        if _check_end_marker():
            _state["exhausted"] = True
            _state["exhaust_reason"] = "检测到底部标记"
            print(f"[分批] {_state['exhaust_reason']}")
            _save_state()  # ★ 持久化
            break

        # 滚动一次
        scroll_page()
        _state["total_scrolls"] += 1
        batch_scrolls += 1
        time.sleep(random.uniform(0.8, 1.5))

        # 等待渲染后计数
        if batch_scrolls % 2 == 0:  # 每滚2次检查一次
            time.sleep(RENDER_WAIT)
            current_cards = _count_cards()
            new_since_scroll = current_cards - _state["total_cards"]

            if new_since_scroll > 0:
                print(f"[分批] 滚动{_state['total_scrolls']}: 卡片{current_cards} (本批新增{new_since_scroll})")
                _state["total_cards"] = current_cards
                stable_count = 0
            else:
                stable_count += 1
                print(f"[分批] 滚动{_state['total_scrolls']}: 卡片{current_cards} (无新增, 稳定{stable_count}/{STABLE_THRESHOLD})")

            # 连续无新增 → 穷尽
            if stable_count >= STABLE_THRESHOLD:
                _state["exhausted"] = True
                _state["exhaust_reason"] = f"连续{STABLE_THRESHOLD}次无新增"
                print(f"[分批] {_state['exhaust_reason']}")
                _save_state()  # ★ 持久化
                break

            # 本批新增达到目标 → 停止滚动
            new_total = current_cards - from_index
            if new_total >= BATCH_SIZE:
                print(f"[分批] 本批已新增{new_total}条，达到目标{BATCH_SIZE}，停止滚动")
                break

    # 增量提取新岗位
    new_jobs = _extract_new_jobs(from_index)

    if new_jobs:
        # 追加到缓存文件
        append_cache(new_jobs, cache_file)
        _state["total_extracted"] += len(new_jobs)
        print(f"[分批] 第{batch_num}批提取 {len(new_jobs)} 条，累计 {_state['total_extracted']} 条")

        # 打印前5条预览
        for i, j in enumerate(new_jobs[:5]):
            print(f"  [{j.get('page_order','?')}] {j.get('title','')[:25]} | {j.get('salary','')} | {j.get('company','')[:15]}")
        if len(new_jobs) > 5:
            print(f"  ... 共{len(new_jobs)}条")
    else:
        print(f"[分批] 第{batch_num}批无新岗位")
        if not _state["exhausted"]:
            _state["exhausted"] = True
            _state["exhaust_reason"] = "本批无新岗位"

    # 持久化 — 保存本批进度供下一批进程恢复
    _save_state()

    # ★ 提示连续滚动 — 写死规则，不让学生忘了还有下一批
    if not _state["exhausted"]:
        print(f"\n[分批] ★ 本批处理完后，必须继续运行 --step next-batch 加载第{batch_num + 1}批")
        print(f"[分批] ★ 当前页面还有更多岗位可向下滚动，不要停在第一批！")
    else:
        print(f"\n[分批] ★ 已穷尽: {_state['exhaust_reason']}")
        print(f"[分批] ★ 总计 {_state['total_extracted']} 条岗位，{_state['total_scrolls']} 次滚动")

    return {
        "batch_num": batch_num,
        "new_jobs": new_jobs,
        "new_count": len(new_jobs),
        "total_extracted": _state["total_extracted"],
        "total_cards": _state["total_cards"],
        "exhausted": _state["exhausted"],
        "exhaust_reason": _state["exhaust_reason"],
    }


def is_exhausted():
    """是否已穷尽（所有岗位都加载完了）"""
    _load_state()  # 跨进程恢复
    return _state["exhausted"]


def get_state():
    """获取当前分批状态"""
    _load_state()  # 跨进程恢复
    return dict(_state)


def reset_state():
    """重置分批状态（新一轮开始时调用）"""
    _state.update({
        "total_cards": 0,
        "total_extracted": 0,
        "total_scrolls": 0,
        "exhausted": False,
        "exhaust_reason": "",
        "batch_num": 0,
    })
    _save_state()  # 持久化重置后的状态
    print("[状态] 已重置分批进度 (batch_state.json 已清零)")


# ═══════════════════════════════════════════════════════════
# 兼容旧API — exhaust_lazy_load 保留但标记为废弃
# ═══════════════════════════════════════════════════════════

def exhaust_lazy_load(max_scrolls=MAX_TOTAL_SCROLLS, stable_threshold=STABLE_THRESHOLD,
                      progress_callback=None):
    """[已废弃] 一口气下拉到底 — v4.4 改为分批模式，请用 scroll_next_batch()

    保留仅为向后兼容，内部循环调用 scroll_next_batch。
    """
    print("[穷尽] exhaust_lazy_load 已废弃，建议使用 scroll_next_batch() 分批模式")
    print("-" * 50)

    while not _state["exhausted"] and _state["total_scrolls"] < max_scrolls:
        result = scroll_next_batch()
        if progress_callback:
            progress_callback(_state["total_scrolls"], result["total_cards"], result["new_count"])

    return {
        "total_cards": _state["total_cards"],
        "total_scrolls": _state["total_scrolls"],
        "exhausted": _state["exhausted"],
        "reason": _state["exhaust_reason"],
    }


def main():
    """CLI 入口: 导航到推荐页 + 分批加载

    CLI模式会一次性跑完所有批次（仅用于测试）。
    正式投递时应该由上层编排逐批调用 scroll_next_batch()。
    """
    global BATCH_SIZE
    import argparse
    parser = argparse.ArgumentParser(description="推荐页导航 + 分批懒加载")
    parser.add_argument("--city", default=_PROFILE_CITY, help="城市编码 (从user_profile读取)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="每批目标岗位数")
    parser.add_argument("--reset", action="store_true", help="重置分批状态（新一轮开始时用）")
    args = parser.parse_args()

    BATCH_SIZE = args.batch_size

    if args.reset:
        reset_state()
        print("[CLI] 已重置状态，准备从头开始")

    if not health_check():
        print("WebBridge 不可达，请先运行 env_check.py")
        sys.exit(1)

    if not ensure_active_tab():
        print("无法建立标签页")
        sys.exit(1)

    # 1. 导航到推荐页
    if not navigate_to_recommend(args.city):
        print("导航到推荐页失败")
        sys.exit(1)

    # 2. 分批加载（CLI测试模式：一次性跑完）
    print(f"\n[CLI测试] 开始分批加载 (每批 {BATCH_SIZE} 条)")
    print("=" * 60)

    while not is_exhausted():
        result = scroll_next_batch()
        print(f"\n[批次{result['batch_num']}] 新增{result['new_count']}条, 累计{result['total_extracted']}条, 穷尽={result['exhausted']}")

    # 3. 汇总
    print("\n" + "=" * 60)
    print(f"分批加载完成:")
    print(f"  总批次: {_state['batch_num']}")
    print(f"  总卡片数: {_state['total_cards']}")
    print(f"  总提取数: {_state['total_extracted']}")
    print(f"  总滚动次数: {_state['total_scrolls']}")
    print(f"  是否穷尽: {_state['exhausted']}")
    print(f"  穷尽原因: {_state['exhaust_reason']}")
    print("=" * 60)


if __name__ == "__main__":
    main()

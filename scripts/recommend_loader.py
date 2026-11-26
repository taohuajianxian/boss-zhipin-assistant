#!/usr/bin/env python3



































import json
try:
    from scripts.atomic_io import atomic_dump_json
except ImportError:
    from atomic_io import atomic_dump_json

import time
import random
import sys
import os

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.profile_loader import WORK_DIR, CITY_CODE as _PROFILE_CITY

from scripts.webbridge_client import (
    SESSION, navigate, evaluate, scroll_page,
    ensure_active_tab, ensure_active_tab_ex, set_target_tab, get_target_tab,
    health_check, extract_jobs, parse_salary_max,
)
from scripts.surface_cache import KEEP_FIELDS, append_cache





RECOMMEND_URL = "https://www.zhipin.com/web/geek/jobs?ka=header-jobs"


BATCH_SIZE = 50
MAX_SCROLLS_PER_BATCH = 8
RENDER_WAIT = 3


MAX_TOTAL_SCROLLS = 80
STABLE_THRESHOLD = 4




BATCH_STATE_FILE = os.path.join(WORK_DIR, "batch_state.json")





_state = {
    "total_cards": 0,
    "total_extracted": 0,
    "total_scrolls": 0,
    "exhausted": False,
    "exhaust_reason": "",
    "batch_num": 0,
}


def _load_state():








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




    try:
        atomic_dump_json(BATCH_STATE_FILE, _state, indent=2)
    except IOError as e:
        print(f"[状态] 保存 batch_state.json 失败: {e}")


def navigate_to_recommend(city_code=_PROFILE_CITY):








    url = RECOMMEND_URL

    ok, _tab_id, _tab_url, _detail = ensure_active_tab_ex(cleanup=True)
    if not ok:
        print(f"[导航] ERROR: 无法建立独立 BOSS 标签页 ({_detail})")
        return False
    print(f"[导航] {url}")
    navigate(url, wait_range=(8, 10))

    _landing = evaluate("window.location.href") or ""
    if _landing:
        set_target_tab(_landing)

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
    _save_state()
    return card_count > 0


def _count_cards():

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








    jobs = extract_jobs()


    seen = set()
    unique_jobs = []
    for job in jobs:
        jid = job.get('jobId', '')
        if jid and jid not in seen:
            seen.add(jid)
            unique_jobs.append(job)


    for job in unique_jobs:
        salary_str = job.get('salary', '')
        job['salary_max_k'] = parse_salary_max(salary_str)

    _state["total_cards"] = len(unique_jobs)


    if len(unique_jobs) <= from_index:
        return []

    new_jobs = unique_jobs[from_index:]
    for i, job in enumerate(new_jobs):
        job['page_order'] = from_index + i


    slim_jobs = []
    for job in new_jobs:
        slim = {k: job.get(k, '') for k in KEEP_FIELDS}
        slim_jobs.append(slim)

    return slim_jobs


def scroll_next_batch(cache_file="surface_cache.json"):



























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

        if _state["total_scrolls"] >= MAX_TOTAL_SCROLLS:
            _state["exhausted"] = True
            _state["exhaust_reason"] = f"达到全局滚动上限{MAX_TOTAL_SCROLLS}次"
            print(f"[分批] {_state['exhaust_reason']}")
            _save_state()
            break


        if _check_end_marker():
            _state["exhausted"] = True
            _state["exhaust_reason"] = "检测到底部标记"
            print(f"[分批] {_state['exhaust_reason']}")
            _save_state()
            break


        scroll_page()
        _state["total_scrolls"] += 1
        batch_scrolls += 1
        time.sleep(random.uniform(0.8, 1.5))


        if batch_scrolls % 2 == 0:
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


            if stable_count >= STABLE_THRESHOLD:
                _state["exhausted"] = True
                _state["exhaust_reason"] = f"连续{STABLE_THRESHOLD}次无新增"
                print(f"[分批] {_state['exhaust_reason']}")
                _save_state()
                break


            new_total = current_cards - from_index
            if new_total >= BATCH_SIZE:
                print(f"[分批] 本批已新增{new_total}条，达到目标{BATCH_SIZE}，停止滚动")
                break


    new_jobs = _extract_new_jobs(from_index)

    if new_jobs:

        append_cache(new_jobs, cache_file)
        _state["total_extracted"] += len(new_jobs)
        print(f"[分批] 第{batch_num}批提取 {len(new_jobs)} 条，累计 {_state['total_extracted']} 条")


        for i, j in enumerate(new_jobs[:5]):
            print(f"  [{j.get('page_order','?')}] {j.get('title','')[:25]} | {j.get('salary','')} | {j.get('company','')[:15]}")
        if len(new_jobs) > 5:
            print(f"  ... 共{len(new_jobs)}条")
    else:
        print(f"[分批] 第{batch_num}批无新岗位")
        if not _state["exhausted"]:
            _state["exhausted"] = True
            _state["exhaust_reason"] = "本批无新岗位"


    _save_state()


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

    _load_state()
    return _state["exhausted"]


def get_state():

    _load_state()
    return dict(_state)


def reset_state():

    _state.update({
        "total_cards": 0,
        "total_extracted": 0,
        "total_scrolls": 0,
        "exhausted": False,
        "exhaust_reason": "",
        "batch_num": 0,
    })
    _save_state()
    print("[状态] 已重置分批进度 (batch_state.json 已清零)")






def exhaust_lazy_load(max_scrolls=MAX_TOTAL_SCROLLS, stable_threshold=STABLE_THRESHOLD,
                      progress_callback=None):




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


    if not navigate_to_recommend(args.city):
        print("导航到推荐页失败")
        sys.exit(1)


    print(f"\n[CLI测试] 开始分批加载 (每批 {BATCH_SIZE} 条)")
    print("=" * 60)

    while not is_exhausted():
        result = scroll_next_batch()
        print(f"\n[批次{result['batch_num']}] 新增{result['new_count']}条, 累计{result['total_extracted']}条, 穷尽={result['exhausted']}")


    print("\n" + "=" * 60)
    print(f"分批加载完成:")
    print(f"  总批次: {_state['batch_num']}")
    print(f"  总卡片数: {_state['total_cards']}")
    print(f"  总提取数: {_state['total_extracted']}")
    print(f"  总滚动次数: {_state['total_scrolls']}")
    print(f"  是否穷尽: {_state['exhausted']}")
    print(f"  穷尽原因: {_state['exhaust_reason']}")
    print("=" * 60)


def _cli_entry():







    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from scripts import run_lock as _rl
    lock, holder = _rl.acquire("recommend", note="CLI 独立运行")
    if lock is None:
        print("[锁] 另一轮正在运行: {0}".format(_rl.describe(holder)))
        print("      台账写盘会冲突，本次不执行；查看: python scripts/run_lock.py --info")
        return 2
    try:
        rc = main()
    finally:
        lock.release()
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(_cli_entry())
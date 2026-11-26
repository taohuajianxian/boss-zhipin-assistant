#!/usr/bin/env python3























import json
try:
    from scripts.atomic_io import atomic_dump_json
except ImportError:
    from atomic_io import atomic_dump_json

import os
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.profile_loader import WORK_DIR, CITY_CODE as _PROFILE_CITY, TARGET_CITY as _PROFILE_CITY_NAME

from scripts.webbridge_client import (
    SESSION, navigate, evaluate, health_check, ensure_active_tab,
    ensure_active_tab_ex, set_target_tab,
    ProgressManager,
)


PAGE_STATE_FILE = os.path.join(WORK_DIR, "page_state.json")
USER_PROFILE_FILE = os.path.join(SKILL_DIR, "user_profile.json")
PROGRESS_FILE = os.path.join(WORK_DIR, "stream_progress.json")


SEARCH_URL_TEMPLATE = "https://www.zhipin.com/web/geek/job?query={keyword}&city={city}"






def _default_page_state():

    return {
        "page_type": "recommend",
        "current_keyword": "",
        "tried_keywords": [],
        "pages_processed": 0,
        "page_delivered_count": 0,
        "page_start_delivered": 0,
        "evolved_keywords": [],
    }


def load_page_state():

    if os.path.exists(PAGE_STATE_FILE):
        try:
            with open(PAGE_STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)

                default = _default_page_state()
                default.update(state)
                return default
        except (json.JSONDecodeError, IOError):
            pass
    return _default_page_state()


def save_page_state(state):

    atomic_dump_json(PAGE_STATE_FILE, state, indent=2)


def reset_page_state():

    state = _default_page_state()
    save_page_state(state)
    print("[页面状态] 已重置")
    return state






def evaluate_page_results():














    state = load_page_state()


    pm = ProgressManager(PROGRESS_FILE)
    pm.load()

    delivered = getattr(pm, 'delivered', [])
    total_delivered = len(delivered)


    try:
        from scripts import stats_core as _sc
        _st = _sc.compute()
        total_delivered = int(_st["cumulative"]["success"])
    except Exception as _e:
        print(f"[进化] 警告: stats_core 不可用({_e})，回退 stream_progress 计数")


    page_start = state.get('page_start_delivered', 0)
    page_delivered_jobs = delivered[page_start:]
    page_delivered_count = len(page_delivered_jobs)
    if not delivered and total_delivered:
        page_delivered_count = max(0, total_delivered - int(page_start or 0))

    print(f"\n[进化] ═══ 本页面投递结果评估 ═══")
    print(f"[进化] 本页面投递成功: {page_delivered_count} 份")
    print(f"[进化] 累计投递成功: {total_delivered} 份")


    industry_dist = {}
    for job in page_delivered_jobs:
        industry = job.get('industry', '未知') or '未知'
        industry_dist[industry] = industry_dist.get(industry, 0) + 1

    print(f"[进化] 行业分布:")
    for ind, count in sorted(industry_dist.items(), key=lambda x: -x[1]):
        print(f"  {ind}: {count}")


    title_words = {}
    for job in page_delivered_jobs:
        title = job.get('title', '')

        import re
        words = re.findall(r'[\u4e00-\u9fff]+', title)
        for w in words:
            if len(w) >= 2:
                title_words[w] = title_words.get(w, 0) + 1


    top_keywords = sorted(title_words.items(), key=lambda x: -x[1])[:10]
    print(f"[进化] 高频标题词:")
    for word, count in top_keywords:
        print(f"  {word}: {count}")


    user_profile = {}
    if os.path.exists(USER_PROFILE_FILE):
        with open(USER_PROFILE_FILE, 'r', encoding='utf-8') as f:
            user_profile = json.load(f)

    predefined_keywords = user_profile.get('search_keywords', [])
    tried_keywords = state.get('tried_keywords', [])


    untried_predefined = [k for k in predefined_keywords if k not in tried_keywords]

    print(f"[进化] 预定义关键词: {len(predefined_keywords)} 个")
    print(f"[进化] 已尝试: {len(tried_keywords)} 个")
    print(f"[进化] 未尝试: {len(untried_predefined)} 个")

    if untried_predefined:
        print(f"[进化] 下一个可用的预定义关键词:")
        for k in untried_predefined[:5]:
            print(f"  → {k}")
    else:
        print(f"[进化] ⚠ 预定义关键词已全部用完")
        print(f"[进化] ★ AI需要基于投递结果生成新的搜索关键词")
        print(f"[进化] ★ 参考: 高频行业={list(industry_dist.keys())[:3]}, 高频标题词={[w[0] for w in top_keywords[:5]]}")


    result = {
        "page_delivered": page_delivered_count,
        "total_delivered": total_delivered,
        "industry_distribution": industry_dist,
        "title_keywords": [w[0] for w in top_keywords],
        "suggested_keywords": untried_predefined[:10],
        "all_predefined_used": len(untried_predefined) == 0,
    }

    return result


def get_next_keyword():








    state = load_page_state()
    tried = state.get('tried_keywords', [])


    user_profile = {}
    if os.path.exists(USER_PROFILE_FILE):
        with open(USER_PROFILE_FILE, 'r', encoding='utf-8') as f:
            user_profile = json.load(f)

    predefined = user_profile.get('search_keywords', [])
    evolved = state.get('evolved_keywords', [])


    all_candidates = predefined + evolved

    for kw in all_candidates:
        if kw not in tried:
            return kw

    return None


def add_evolved_keyword(keyword):





    state = load_page_state()
    if keyword not in state.get('evolved_keywords', []):
        state.setdefault('evolved_keywords', []).append(keyword)
        save_page_state(state)
        print(f"[进化] 已添加新关键词: {keyword}")


def mark_keyword_tried(keyword):





    state = load_page_state()
    if keyword not in state.get('tried_keywords', []):
        state.setdefault('tried_keywords', []).append(keyword)
    state['current_keyword'] = keyword
    state['page_type'] = 'search'
    save_page_state(state)
    print(f"[进化] 关键词已标记为已尝试: {keyword}")






def navigate_to_search(keyword, city_code=_PROFILE_CITY):














    import re
    city_name = _PROFILE_CITY_NAME.strip()
    if city_name:
        keyword = re.sub(r'\s*' + re.escape(city_name) + r'\s*$', '', keyword.strip()).strip()

    if not health_check():
        print("ERROR: WebBridge 不可达")
        return False, keyword


    from urllib.parse import quote
    encoded_kw = quote(keyword)
    url = SEARCH_URL_TEMPLATE.format(keyword=encoded_kw, city=city_code)

    print(f"[搜索] 导航到搜索结果页: {keyword}")
    print(f"[搜索] URL: {url}")


    _ok, _tid, _turl, _detail = ensure_active_tab_ex(cleanup=True)
    if not _ok:
        print(f"[搜索] ERROR: 无法建立独立 BOSS 标签页 ({_detail})")
        return False, keyword

    navigate(url, wait_range=(8, 10))


    _landing = evaluate("window.location.href") or ""
    if _landing:
        set_target_tab(_landing)


    current_url = evaluate("window.location.href") or ""
    if "query=" not in current_url and "job" not in current_url:
        print(f"[搜索] 警告: 当前URL可能不在搜索页: {current_url}")
        return False, keyword

    time.sleep(2)


    result = evaluate("""
    (function(){
        var cards = document.querySelectorAll('.job-card-wrap');
        var noResult = document.querySelector('.search-no-result, .empty');
        return JSON.stringify({
            card_count: cards.length,
            has_no_result: noResult ? true : false
        });
    })()
    """) or "{}"

    try:
        data = json.loads(result)
        card_count = data.get('card_count', 0)
        has_no_result = data.get('has_no_result', False)

        if has_no_result or card_count == 0:
            print(f"[搜索] 无搜索结果 (cards={card_count}, no_result={has_no_result})")
            return False, keyword

        print(f"[搜索] 搜索结果页已加载，初始卡片数: {card_count}")

        try:
            from scripts import stats_core as _sc_nav
            _st_nav = load_page_state()
            _st_nav['page_start_delivered'] = int(_sc_nav.compute()['cumulative']['success'])
            _st_nav['page_delivered_count'] = 0
            save_page_state(_st_nav)
        except Exception:
            pass
        return True, keyword

    except (json.JSONDecodeError, TypeError):
        print(f"[搜索] 搜索结果解析失败")
        return False, keyword


def start_new_page(keyword=None, city_code=_PROFILE_CITY):













    state = load_page_state()


    pm = ProgressManager(PROGRESS_FILE)
    pm.load()

    try:
        from scripts import stats_core as _sc_base
        _base0 = int(_sc_base.compute()['cumulative']['success'])
    except Exception:
        _base0 = len(getattr(pm, 'delivered', []))
    state['page_start_delivered'] = _base0
    state['page_delivered_count'] = 0

    if keyword is None:

        state['page_type'] = 'recommend'
        state['current_keyword'] = ''
        save_page_state(state)

        print("[页面] 开始处理推荐页")
        from scripts.recommend_loader import navigate_to_recommend, reset_state
        reset_state()
        return navigate_to_recommend(city_code)
    else:


        ok, clean_kw = navigate_to_search(keyword, city_code)
        if ok:
            mark_keyword_tried(clean_kw)
        state = load_page_state()

        state['page_start_delivered'] = _base0
        state['page_delivered_count'] = 0
        state['pages_processed'] = state.get('pages_processed', 0) + 1
        save_page_state(state)

        print(f"[页面] 开始处理搜索页 (关键词: {clean_kw})")
        from scripts.recommend_loader import reset_state
        reset_state()
        return ok






def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI关键词进化 + 搜索导航")
    parser.add_argument("--action", required=True,
                        choices=['evaluate', 'next-keyword', 'navigate', 'add-keyword', 'reset', 'status'],
                        help="evaluate=评估页面结果 / next-keyword=获取下一个关键词 / navigate=导航到搜索页 / add-keyword=添加新关键词 / reset=重置 / status=查看状态")
    parser.add_argument("--keyword", default="", help="搜索关键词 (navigate/add-keyword时需要)")
    parser.add_argument("--city", default=_PROFILE_CITY, help="城市编码")
    args = parser.parse_args()

    if args.action == 'evaluate':
        result = evaluate_page_results()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.action == 'next-keyword':
        kw = get_next_keyword()
        if kw:
            print(f"下一个搜索关键词: {kw}")
        else:
            print("无可用关键词（预定义关键词已全部用完，需要AI生成新关键词）")
            print("使用 --action add-keyword --keyword '新关键词' 添加")

    elif args.action == 'navigate':
        if not args.keyword:
            print("ERROR: 需要 --keyword 参数")
            sys.exit(1)
        ok, clean_kw = navigate_to_search(args.keyword, args.city)
        if ok:
            mark_keyword_tried(clean_kw)
            print(f"已导航到搜索页: {clean_kw}")
        else:
            print(f"导航失败")
            sys.exit(1)

    elif args.action == 'add-keyword':
        if not args.keyword:
            print("ERROR: 需要 --keyword 参数")
            sys.exit(1)
        add_evolved_keyword(args.keyword)
        print(f"已添加: {args.keyword}")

    elif args.action == 'reset':
        reset_page_state()

    elif args.action == 'status':
        state = load_page_state()
        print(json.dumps(state, ensure_ascii=False, indent=2))


def _cli_entry():







    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from scripts import run_lock as _rl
    lock, holder = _rl.acquire("evolve", note="CLI 独立运行")
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
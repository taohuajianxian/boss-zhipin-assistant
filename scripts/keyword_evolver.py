#!/usr/bin/env python3
"""
模块 H: AI关键词进化 + 搜索导航 (v5.0 NEW)

★ 核心职责 — 流程图"AI评估当前页面投递结果，找出新的关键词"节点:
  当一个页面（推荐页或搜索页）的岗位被穷尽后:
  1. 读取本页面的投递结果（成功投递的岗位列表）
  2. AI 分析成功投递的岗位标题/行业/公司，提取高频关键词
  3. 结合 user_profile.json 的 search_keywords，生成新的搜索关键词
  4. 从未尝试过的关键词中选取下一个
  5. 导航到 BOSS直聘搜索结果页

★ 关键词进化逻辑:
  - 从成功投递的岗位中提取行业/职位关键词
  - 排除已经搜索过的关键词（tried_keywords）
  - 优先使用 user_profile.json 中预定义的 search_keywords
  - 如果预定义关键词用完，AI 可以基于投递结果生成新关键词
  - 新关键词写入 page_state.json 供跨进程使用

独立可运行:
    python keyword_evolver.py --action next-keyword
    python keyword_evolver.py --action navigate --keyword "运营 北京"
"""

import json
import os
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.profile_loader import WORK_DIR, CITY_CODE as _PROFILE_CITY, TARGET_CITY as _PROFILE_CITY_NAME

from scripts.webbridge_client import (
    SESSION, navigate, evaluate, health_check, ensure_active_tab,
    ProgressManager,
)

# 文件路径
PAGE_STATE_FILE = os.path.join(WORK_DIR, "page_state.json")
USER_PROFILE_FILE = os.path.join(SKILL_DIR, "user_profile.json")
PROGRESS_FILE = os.path.join(WORK_DIR, "stream_progress.json")

# 搜索URL模板
SEARCH_URL_TEMPLATE = "https://www.zhipin.com/web/geek/job?query={keyword}&city={city}"


# ═══════════════════════════════════════════════════════════
# 页面状态管理（跨进程持久化）
# ═══════════════════════════════════════════════════════════

def _default_page_state():
    """默认页面状态"""
    return {
        "page_type": "recommend",       # "recommend" | "search"
        "current_keyword": "",           # 当前搜索关键词（search模式）
        "tried_keywords": [],            # 已尝试过的搜索关键词
        "pages_processed": 0,            # 已处理的页面数
        "page_delivered_count": 0,       # 本页面投递成功数
        "page_start_delivered": 0,       # 本页面开始时的累计投递数
        "evolved_keywords": [],          # AI进化生成的新关键词
    }


def load_page_state():
    """加载页面状态"""
    if os.path.exists(PAGE_STATE_FILE):
        try:
            with open(PAGE_STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
                # 合并默认值（防止旧文件缺字段）
                default = _default_page_state()
                default.update(state)
                return default
        except (json.JSONDecodeError, IOError):
            pass
    return _default_page_state()


def save_page_state(state):
    """保存页面状态"""
    with open(PAGE_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def reset_page_state():
    """重置页面状态（新一轮开始时调用）"""
    state = _default_page_state()
    save_page_state(state)
    print("[页面状态] 已重置")
    return state


# ═══════════════════════════════════════════════════════════
# 关键词进化
# ═══════════════════════════════════════════════════════════

def evaluate_page_results():
    """★ 流程图节点: AI评估当前页面投递结果，找出新的关键词

    读取本页面投递成功的岗位，分析行业/标题分布，
    输出评估结果供 AI 进一步生成新关键词。

    返回:
        dict: {
            "page_delivered": int,        # 本页面投递成功数
            "total_delivered": int,       # 累计投递成功数
            "industry_distribution": dict, # 行业分布
            "title_keywords": list,       # 高频标题关键词
            "suggested_keywords": list,   # 建议的新搜索关键词
        }
    """
    state = load_page_state()

    # 读取投递进度
    pm = ProgressManager(PROGRESS_FILE)
    pm.load()

    delivered = getattr(pm, 'delivered', [])
    total_delivered = len(delivered)
    # ★ 统一口径（P1 修复）：累计成功数以统一台账 stats_core 为准，
    #   旧逻辑只读 stream_progress.delivered 列表，evolve 因此报「累计投递 0 份」
    try:
        from scripts import stats_core as _sc
        _st = _sc.compute()
        total_delivered = int(_st["cumulative"]["success"])
    except Exception as _e:
        print(f"[进化] 警告: stats_core 不可用({_e})，回退 stream_progress 计数")

    # 本页面的投递（从 page_start_delivered 到现在）
    page_start = state.get('page_start_delivered', 0)
    page_delivered_jobs = delivered[page_start:]
    page_delivered_count = len(page_delivered_jobs)
    if not delivered and total_delivered:
        page_delivered_count = max(0, total_delivered - int(page_start or 0))

    print(f"\n[进化] ═══ 本页面投递结果评估 ═══")
    print(f"[进化] 本页面投递成功: {page_delivered_count} 份")
    print(f"[进化] 累计投递成功: {total_delivered} 份")

    # 行业分布分析
    industry_dist = {}
    for job in page_delivered_jobs:
        industry = job.get('industry', '未知') or '未知'
        industry_dist[industry] = industry_dist.get(industry, 0) + 1

    print(f"[进化] 行业分布:")
    for ind, count in sorted(industry_dist.items(), key=lambda x: -x[1]):
        print(f"  {ind}: {count}")

    # 标题关键词提取（简单分词，找高频词）
    title_words = {}
    for job in page_delivered_jobs:
        title = job.get('title', '')
        # 简单按常见分隔符分词
        import re
        words = re.findall(r'[\u4e00-\u9fff]+', title)
        for w in words:
            if len(w) >= 2:
                title_words[w] = title_words.get(w, 0) + 1

    # 取前10高频词
    top_keywords = sorted(title_words.items(), key=lambda x: -x[1])[:10]
    print(f"[进化] 高频标题词:")
    for word, count in top_keywords:
        print(f"  {word}: {count}")

    # 加载用户画像中的预定义关键词
    user_profile = {}
    if os.path.exists(USER_PROFILE_FILE):
        with open(USER_PROFILE_FILE, 'r', encoding='utf-8') as f:
            user_profile = json.load(f)

    predefined_keywords = user_profile.get('search_keywords', [])
    tried_keywords = state.get('tried_keywords', [])

    # 未尝试过的预定义关键词
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

    # 输出评估结果
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
    """获取下一个未尝试的搜索关键词

    优先使用 user_profile.json 中的预定义关键词，
    如果全部用完，返回 None（此时需要 AI 生成新关键词）。

    返回:
        str or None: 下一个搜索关键词，或 None（无可用关键词）
    """
    state = load_page_state()
    tried = state.get('tried_keywords', [])

    # 加载用户画像
    user_profile = {}
    if os.path.exists(USER_PROFILE_FILE):
        with open(USER_PROFILE_FILE, 'r', encoding='utf-8') as f:
            user_profile = json.load(f)

    predefined = user_profile.get('search_keywords', [])
    evolved = state.get('evolved_keywords', [])

    # 合并候选列表（预定义优先，然后是AI进化的）
    all_candidates = predefined + evolved

    for kw in all_candidates:
        if kw not in tried:
            return kw

    return None


def add_evolved_keyword(keyword):
    """添加AI进化生成的新关键词

    参数:
        keyword: 新的搜索关键词
    """
    state = load_page_state()
    if keyword not in state.get('evolved_keywords', []):
        state.setdefault('evolved_keywords', []).append(keyword)
        save_page_state(state)
        print(f"[进化] 已添加新关键词: {keyword}")


def mark_keyword_tried(keyword):
    """标记关键词为已尝试

    参数:
        keyword: 已使用的搜索关键词
    """
    state = load_page_state()
    if keyword not in state.get('tried_keywords', []):
        state.setdefault('tried_keywords', []).append(keyword)
    state['current_keyword'] = keyword
    state['page_type'] = 'search'
    save_page_state(state)
    print(f"[进化] 关键词已标记为已尝试: {keyword}")


# ═══════════════════════════════════════════════════════════
# 搜索页导航
# ═══════════════════════════════════════════════════════════

def navigate_to_search(keyword, city_code=_PROFILE_CITY):
    """★ 流程图节点: 以新关键词搜索岗位，定位到搜索结果页

    导航到 BOSS直聘搜索结果页。
    搜索页的卡片结构和推荐页一致，分批加载逻辑通用。

    参数:
        keyword: 搜索关键词（纯职位关键词，不含城市。城市已由 city_code 参数锁定）
        city_code: 城市编码

    返回:
        (bool, str): (导航是否成功, 清洗后的关键词)
    """
    # ★ 关键词清洗：去除末尾的城市后缀
    # 城市已在 URL 参数 city=xxx 中指定，关键词不应重复携带
    import re
    city_name = _PROFILE_CITY_NAME.strip()
    if city_name:
        keyword = re.sub(r'\s*' + re.escape(city_name) + r'\s*$', '', keyword.strip()).strip()

    if not health_check():
        print("ERROR: WebBridge 不可达")
        return False, keyword

    # URL编码关键词
    from urllib.parse import quote
    encoded_kw = quote(keyword)
    url = SEARCH_URL_TEMPLATE.format(keyword=encoded_kw, city=city_code)

    print(f"[搜索] 导航到搜索结果页: {keyword}")
    print(f"[搜索] URL: {url}")

    # 导航前先确保独立标签页（不抢用户当前页面）
    if not ensure_active_tab():
        print(f"[搜索] ERROR: 无法建立独立 BOSS 标签页")
        return False, keyword

    navigate(url, wait_range=(8, 10))

    # 验证是否到达搜索页
    current_url = evaluate("window.location.href") or ""
    if "query=" not in current_url and "job" not in current_url:
        print(f"[搜索] 警告: 当前URL可能不在搜索页: {current_url}")
        return False, keyword

    time.sleep(2)

    # 检查是否有搜索结果
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
        # P1 统一口径：换页成功即刷新本页起点（否则 page_delivered 恒等于累计总数）
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
    """★ 开始处理新页面（推荐页或搜索页）

    流程图"当前页面处理开始"节点:
    - 第一次: 定位到职位推荐页
    - 后续: 以新关键词搜索，定位到搜索结果页

    参数:
        keyword: 搜索关键词。None=推荐页，有值=搜索页
        city_code: 城市编码

    返回:
        bool: 是否成功开始新页面
    """
    state = load_page_state()

    # 记录本页面开始时的累计投递数
    pm = ProgressManager(PROGRESS_FILE)
    pm.load()
    # P1 统一口径：起点以 stats_core 累计成功数为准
    try:
        from scripts import stats_core as _sc_base
        _base0 = int(_sc_base.compute()['cumulative']['success'])
    except Exception:
        _base0 = len(getattr(pm, 'delivered', []))
    state['page_start_delivered'] = _base0
    state['page_delivered_count'] = 0

    if keyword is None:
        # 推荐页
        state['page_type'] = 'recommend'
        state['current_keyword'] = ''
        save_page_state(state)

        print("[页面] 开始处理推荐页")
        from scripts.recommend_loader import navigate_to_recommend, reset_state
        reset_state()  # 重置分批状态
        return navigate_to_recommend(city_code)
    else:
        # 搜索页
        # ★ 先导航获取清洗后关键词，再标记
        ok, clean_kw = navigate_to_search(keyword, city_code)
        if ok:
            mark_keyword_tried(clean_kw)
        state = load_page_state()
        # 导航后重新 load 会丢掉上面在旧 state 上的赋值，这里补回起点计数
        state['page_start_delivered'] = _base0
        state['page_delivered_count'] = 0
        state['pages_processed'] = state.get('pages_processed', 0) + 1
        save_page_state(state)

        print(f"[页面] 开始处理搜索页 (关键词: {clean_kw})")
        from scripts.recommend_loader import reset_state
        reset_state()  # 重置分批状态
        return ok


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

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


if __name__ == "__main__":
    main()

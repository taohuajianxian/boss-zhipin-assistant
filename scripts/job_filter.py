#!/usr/bin/env python3
"""模块: 规则层粗筛 (Phase 2) — 判定精度第一层

设计原则
--------
判定分两层，规则层只做"机器可断言"的硬排除，语义层（AI）负责判断匹配度：

  规则层（本模块）  →  REJECT / REVIEW / PASS 三态
  语义层（AI decide）→  APPROVE / REJECT

三态语义：
  REJECT  硬排除，不入队（或入队即 skip），AI 无权放行
  REVIEW  规则无法断言，交 AI 语义判定（默认入队）
  PASS    命中画像核心方向词，优先入队

用户明确要求硬排除的方向（2026-11-18 确认）：
  餐饮、网咖、棋牌、助理、实习

误杀保护：
  "餐饮"类岗位若标题同时含 酒店/民宿/文旅/度假/景区/集团/公寓/营地 等住宿文旅信号，
  说明可能是酒店餐饮部/文旅综合体餐饮管理，降级为 REVIEW 交语义层，不硬排。
"""

import os
import re

try:
    from scripts.profile_loader import USER_PROFILE, MIN_SALARY_K, TARGET_CITY
except Exception:  # 独立运行兜底
    USER_PROFILE, MIN_SALARY_K, TARGET_CITY = {}, 0, ""


# ═══════════════════════════════════════════════════════════
# 硬排除词表
# ═══════════════════════════════════════════════════════════

# 用户点名硬排除组
HARD_EXCLUDE_TITLE = [
    # 餐饮门店线
    "餐饮", "餐厅经理", "后厨", "厨师", "厨工",
    "餐饮服务员", "传菜", "洗碗", "火锅", "烧烤", "奶茶店", "饮品店", "烘焙店",
    # 网吧/棋牌线
    "网咖", "网吧", "电竞馆", "网管", "棋牌", "麻将", "台球",
    # 助理/实习线
    "助理", "实习生", "实习", "管培实习生",
]

# 核心方向扩展词（与画像 core_prefer 合并使用，补齐画像未覆盖的住宿文旅子领域）
CORE_INDUSTRY_EXTRA = ["OTA", "民宿", "度假", "公寓", "旅游", "酒店集团", "客房", "前厅部"]

# 住宿文旅信号：与上表冲突时降级 REVIEW（防误杀酒店餐饮部/文旅综合体）
RETAIN_SIGNALS = [
    "酒店", "民宿", "文旅", "景区", "度假", "公寓", "营地", "康养",
    "会所", "集团", "综合体", "商业管理", "物业",
]

# 基础低价值岗（历史 T 级，保留）
BASE_TITLE_EXCLUDE = [
    "保安", "保洁", "服务员", "收银员", "理货员", "搬运", "分拣员", "配送员",
    "外卖骑手", "网约车", "快递员", "司机", "地推", "拉新", "家教", "兼职",
    "日结", "小时工", "纯销售", "销售代表", "销售专员", "销售顾问", "房产中介",
    "保健品会销", "主播", "娱乐主播", "带货主播", "招聘顾问", "RPO", "劳务派遣",
]

# 岗类硬排除：非运营/投放职能岗（2026-11-18 回归暴露）
# 根因：剪辑类 JD 正文普遍写"负责信息流视频剪辑"，语义层若用关键词匹配会被误放行
# v5.9: 方向词——标题同时含方向词时不硬排岗类词，交语义层判定（避免误杀复合岗）
DIRECTION_WORDS = [
    "运营", "投放", "投流", "直播", "电商", "私域", "社群", "增长", "策划",
    "店长", "经理", "主管", "总监", "渠道", "销售",
]

JOB_TYPE_EXCLUDE = [
    "拍剪", "剪辑", "后期制作", "后期剪辑", "视频制作", "短视频制作",
    "摄像", "摄影", "美工", "平面设计", "视觉设计", "UI设计", "插画",
    "AI训练师", "训练师", "数据标注", "标注员", "内容审核",
]

# 城市：用户可接受的非目标城市工作地（驻外/境外，见 user_profile 求职意愿）
CITY_WHITELIST_EXTRA = ["新加坡", "香港", "澳门", "日本", "东京", "大阪", "京都"]

# 常见城市名（用于从标题识别异地工作地点；缓存层已剥离 city 字段，只能靠标题兜底）
CITY_NAMES = [
    "宁波", "温州", "绍兴", "嘉兴", "湖州", "金华", "台州", "丽水", "衢州", "舟山",
    "义乌", "慈溪", "余姚", "诸暨", "海宁", "桐乡",
    "上海", "北京", "深圳", "广州", "南京", "苏州", "无锡", "常州", "南通", "徐州",
    "扬州", "合肥", "武汉", "成都", "重庆", "西安", "长沙", "郑州", "天津", "青岛",
    "济南", "烟台", "厦门", "福州", "泉州", "南昌", "昆明", "贵阳", "南宁", "海口",
    "三亚", "沈阳", "大连", "哈尔滨", "长春", "石家庄", "太原", "乌鲁木齐", "兰州",
    "银川", "西宁", "呼和浩特", "珠海", "东莞", "佛山", "中山", "惠州", "汕头",
    "保定", "廊坊", "洛阳", "襄阳", "宜昌", "株洲", "唐山", "赣州", "桂林",
]

# 标题里的"上班地点"标记词
LOCATION_MARKERS = ["上班", "工作地", "工作地点", "工作城市", "驻场", "派驻", "常驻", "驻"]

# 公司名硬排除（人力中介线）
C_EXCLUDE_COMPANY = [
    "人力资源", "人力", "劳务", "猎头", "猎聘", "人才服务", "人才中介",
    "RPO", "外包服务", "众包", "招聘服务",
]

# 培训机构 / 教育科技
TRAINING_ORG = [
    "培训", "教育科技", "职业培训", "驾培", "驾校", "专修学院", "技工学校",
    "考研", "公考", "留学中介",
]

# 非月薪（伪装高薪）
NON_MONTHLY_SALARY = ["元/天", "元/时", "元/周", "元/小时", "元/次"]

# 匿名/代招公司指代
# 仅排除"纯泛化指代"（某大型企业/某知名公司/某上市公司…）；
# "某文旅集团""某某酒店管理"这类带具体业务名的保留，交画像与语义层判断
ANON_COMPANY_PATTERN = re.compile(
    r"^(某|某某)(大型|知名|中型|小型|上市|外资|国有|民营|互联网|连锁)?(企业|公司|集团|单位|平台)"
    r"$|^(某|某某)(大型|知名|中型|小型|上市)(企业|公司|集团)"
    r"|^(知名|大型|上市)(企业|公司|集团)$")

# 薪资解析
SALARY_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*[Kk千]")
SALARY_SINGLE = re.compile(r"(\d+(?:\.\d+)?)\s*[Kk千]")

# ═══════════════════════════════════════════════════════════
# 规则单一真源（v5.8）：词表与口径统一从 rules.json 派生
# 内置字面量仅作 rules.json 缺失时兜底；改规则请改 rules.json，
# 否则 tools/check_consistency.py 会判 FAIL。
# ═══════════════════════════════════════════════════════════
try:
    from scripts import rules_loader as _rules
except Exception:
    try:
        from scripts import rules_loader as _rules
    except Exception:
        try:
            import rules_loader as _rules  # 独立运行时兜底
        except Exception:
            _rules = None

RULES_SOURCE = "builtin-fallback"
if _rules:
    _st = _rules.status()
    if _st.get("loaded"):
        RULES_SOURCE = "rules.json@%s" % _st.get("version", "?")
    _jt = _rules.job_type_exclude()
    if _jt:
        JOB_TYPE_EXCLUDE = _jt
    _loc = _rules.location_block() or {}
    if _loc.get("whitelist_extra"):
        CITY_WHITELIST_EXTRA = list(_loc["whitelist_extra"])
    if _loc.get("markers"):
        LOCATION_MARKERS = list(_loc["markers"])
    if _loc.get("city_names"):
        CITY_NAMES = list(_loc["city_names"])


def parse_salary_min_k(salary):
    """解析薪资下限(K)；面议/不可解析返回 0"""
    if not salary:
        return 0
    s = str(salary).replace("·", " ").replace("薪", "")
    m = SALARY_RANGE.search(s)
    if m:
        return float(m.group(1))
    m = SALARY_SINGLE.search(s)
    if m:
        return float(m.group(1))
    return 0


def parse_salary_max_k(salary):
    """解析薪资上限(K)；用于报表展示"""
    if not salary:
        return 0
    s = str(salary).replace("·", " ").replace("薪", "")
    m = SALARY_RANGE.search(s)
    if m:
        return float(m.group(2))
    m = SALARY_SINGLE.search(s)
    if m:
        return float(m.group(1))
    return 0


def city_from_title(title, target_city=""):
    """从标题识别明确写在标题里的异地工作地点；识别不到返回空串。

    背景：surface_cache 已剥离 city 字段（假设推荐页按城市过滤），
    但实际存在"投手（宁波上班）"这类挂本地、实地上班异地的岗位，
    因此城市校验必须补一层标题兜底。

    只认三类明确信号，避免把公司名/业务名里的城市当工作地：
      1) 标记词 + 城市：宁波上班 / 工作地点：宁波 / 驻宁波
      2) 括号内点名城市：（宁波）/（需驻上海）
      3) 标题裸城市名（未出现目标城市）
    若标题同时出现目标城市，视为本地岗，放行。
    """
    if not title:
        return ""
    allow = set(CITY_WHITELIST_EXTRA)
    if target_city:
        allow.add(str(target_city))

    def _ok(c):
        return c not in allow

    # 标题已写明目标城市/驻外城市 → 视为本地岗
    for m in LOCATION_MARKERS:
        if target_city and f"{m}{target_city}" in title:
            return ""

    # 1) 标记词 + 城市
    for m in LOCATION_MARKERS:
        for c in CITY_NAMES:
            if not _ok(c):
                continue
            if f"{m}{c}" in title or f"{m}：{c}" in title or f"{m}:{c}" in title:
                return c

    # 2) 括号内城市
    for seg in re.findall(r"[（(【\[]([^）)】\]]{1,14})[）)】\]]", str(title)):
        for c in CITY_NAMES:
            if c in seg and _ok(c):
                return c

    # 3) 标题裸城市名（排除已含目标城市的情况）
    if target_city and target_city in title:
        return ""
    for c in CITY_NAMES:
        if c in title and _ok(c):
            return c
    return ""


def _profile(profile=None):
    return profile if profile is not None else USER_PROFILE


def _hit(text, words):
    if not text:
        return []
    return [w for w in words if w and w in text]


def _rule_screen_impl(job, profile=None):
    """规则层粗筛

    参数:
        job: dict，至少含 title / company / salary，可选 industry / city
        profile: 用户画像 dict（默认读 user_profile.json）

    返回:
        {"verdict": "REJECT"|"REVIEW"|"PASS",
         "reason": str, "rule": str}
    """
    p = _profile(profile)
    title = str(job.get("title") or "")
    company = str(job.get("company") or "")
    salary = str(job.get("salary") or "")
    industry = str(job.get("industry") or "")
    city = str(job.get("city") or "")

    min_salary_k = p.get("min_salary_k", MIN_SALARY_K) or 0
    target_city = p.get("target_city", TARGET_CITY) or ""

    # 1. 非月薪岗位
    hit = _hit(salary, NON_MONTHLY_SALARY)
    if hit:
        return {"verdict": "REJECT", "reason": f"非月薪薪资「{salary[:14]}」", "rule": "非月薪"}

    # 2. 用户点名硬排除（含误杀保护）
    hit = _hit(title, HARD_EXCLUDE_TITLE)
    if hit:
        retain = _hit(title, RETAIN_SIGNALS)
        if retain and any(w in hit for w in ("餐饮", "火锅", "烧烤", "后厨", "厨师", "厨工", "饮品店", "烘焙店")):
            return {"verdict": "REVIEW",
                    "reason": f"餐饮类但含住宿文旅信号「{'/'.join(retain[:2])}」，需语义判断",
                    "rule": "餐饮误杀保护"}
        return {"verdict": "REJECT", "reason": f"标题含硬排除词「{'/'.join(hit[:2])}」", "rule": "标题硬排除"}

    # 3. 基础低价值岗（画像可扩展）
    extra_t = p.get("t_exclude") or []
    hit = _hit(title, BASE_TITLE_EXCLUDE + list(extra_t))
    if hit:
        return {"verdict": "REJECT", "reason": f"标题含低价值岗词「{'/'.join(hit[:2])}」", "rule": "标题硬排除"}

    # 4. 画像黑名单标题
    hit = _hit(title, p.get("black_title") or [])
    if hit:
        return {"verdict": "REJECT", "reason": f"命中画像 black_title「{'/'.join(hit[:2])}」", "rule": "画像黑名单"}

    # 4.5 岗类排除（剪辑/摄影/设计等非运营投放职能岗）
    #     v5.9: 条件硬排——标题同时含方向词(运营/投放/直播/店长…)时只判 REVIEW 交语义层，
    #     避免「酒店销售经理与短视频直播运营」这类复合岗被一刀切误杀（P3）
    hit = _hit(title, JOB_TYPE_EXCLUDE)
    if hit:
        _dir = _hit(title, DIRECTION_WORDS)
        if _dir:
            return {"verdict": "REVIEW",
                    "reason": f"岗类词「{'/'.join(hit[:2])}」与方向词「{'/'.join(_dir[:2])}」共存，交语义层判定",
                    "rule": "岗类待判"}
        return {"verdict": "REJECT", "reason": f"标题含岗类排除词「{'/'.join(hit[:2])}」", "rule": "岗类排除"}

    # 5. 公司名：人力中介 / 匿名代招
    extra_c = p.get("c_exclude") or []
    hit = _hit(company, C_EXCLUDE_COMPANY + list(extra_c))
    if hit:
        return {"verdict": "REJECT", "reason": f"公司名含中介词「{'/'.join(hit[:2])}」", "rule": "公司硬排除"}
    if p.get("anon_company_forbidden") and ANON_COMPANY_PATTERN.match(company):
        return {"verdict": "REJECT", "reason": f"公司名匿名指代「{company[:12]}」", "rule": "匿名代招"}

    # 5.5 行业黑名单（画像 black_industry：保险/医美/中介/贷款…）
    #    坑：surface_cache 的 industry 字段实测 0% 有值（卡片 DOM 取不到行业标签，
    #    KEEP_FIELDS 里留着字段但恒为空），只按 industry 判 = 行业规则长期空转，
    #    和当年 city 字段被剥一模一样。故字段缺失时用「公司名 + 标题」兜底取证。
    _black_ind = p.get("black_industry") or []
    hit = _hit(industry, _black_ind)
    if not hit and not industry and _black_ind:
        hit = _hit(company, _black_ind) or _hit(title, _black_ind)
        if hit:
            return {"verdict": "REJECT",
                    "reason": f"行业黑名单「{'/'.join(hit[:2])}」（industry 字段缺失，按公司名兜底）",
                    "rule": "行业黑名单"}
    if hit:
        return {"verdict": "REJECT", "reason": f"行业黑名单「{'/'.join(hit[:2])}」", "rule": "行业黑名单"}

    # 6. 培训机构（画像规则）
    if p.get("training_org_rule"):
        hit = _hit(company, TRAINING_ORG) or _hit(title, TRAINING_ORG)
        if hit:
            return {"verdict": "REJECT", "reason": f"培训/教育机构「{'/'.join(hit[:2])}」", "rule": "培训机构排除"}

    # 7. 城市（两级：job.city 字段 → 标题兜底）
    #    缓存层已剥离 city，字段校验常年空转，必须叠加标题识别
    if target_city:
        if city and target_city not in city:
            if not any(c in city for c in CITY_WHITELIST_EXTRA):
                return {"verdict": "REJECT", "reason": f"工作地点 {city}，非{target_city}", "rule": "城市不符"}
            _off_city = city_from_title(title, target_city)
            if _off_city:
                return {"verdict": "REJECT",
                        "reason": f"标题明示工作地点 {_off_city}，非{target_city}",
                        "rule": "城市不符"}
        elif city:
            _off_city = city_from_title(title, target_city)
            if _off_city:
                return {"verdict": "REJECT",
                        "reason": f"标题明示工作地点 {_off_city}，非{target_city}",
                        "rule": "城市不符"}
        else:
            # 城市字段缺失：旧版在此静默放行，city 校验长期空转（surface_cache 剥离 city 所致）
            # v5.8 fail-closed：字段缺失且标题无城市 → 不判 PASS，交语义层按 JD 原文举证地点
            _off_city = city_from_title(title, target_city)
            if _off_city:
                return {"verdict": "REJECT",
                        "reason": f"标题明示工作地点 {_off_city}，非{target_city}",
                        "rule": "城市不符"}
            if not any(w in (title or "") for w in ("驻外", "海外", "新加坡", "香港", "澳门", "日本")):
                return {"verdict": "REVIEW",
                        "reason": "工作地点未取证（city 字段缺失、标题无城市），需按 JD 原文核实",
                        "rule": "地点未取证"}

    # 8. 薪资门槛（口径真源：rules.json salary.policy_text —— 上限达标即可谈，仅"上限都不够"才硬排）
    #    原因：BOSS 区间如 7-9K，实际offer常给到9K，按下限一刀切会误杀大量有效岗位
    if min_salary_k:
        smin = parse_salary_min_k(salary)
        smax = parse_salary_max_k(salary) or smin
        if smin and smax and smax < min_salary_k:
            return {"verdict": "REJECT",
                    "reason": f"薪资上限 {smax:g}K 低于门槛 {min_salary_k}K（{salary[:12]}）",
                    "rule": "薪资门槛"}
        if smin and smin < min_salary_k <= smax:
            return {"verdict": "REVIEW",
                    "reason": f"薪资 {smin:g}-{smax:g}K 下限低于门槛，上限达标可谈",
                    "rule": "薪资可谈"}
        if not smin and salary and "面议" not in salary:
            return {"verdict": "REVIEW", "reason": f"薪资无法解析「{salary[:12]}」", "rule": "薪资待核"}
        if not smin and "面议" in salary:
            return {"verdict": "REVIEW", "reason": "面议薪资，需语义层确认", "rule": "面议"}

    # 9. 命中核心方向 → PASS
    core = list(p.get("core_prefer") or []) + CORE_INDUSTRY_EXTRA
    hay = title + " " + industry
    hit_core = _hit(hay, core)
    if hit_core:
        return {"verdict": "PASS", "reason": f"命中核心方向「{'/'.join(hit_core[:3])}」", "rule": "核心方向"}

    generic = p.get("generic_prefer") or []
    hit_gen = _hit(hay, generic)
    if hit_gen:
        return {"verdict": "REVIEW", "reason": f"命中泛方向「{'/'.join(hit_gen[:3])}」，需语义确认", "rule": "泛方向"}

    return {"verdict": "REVIEW", "reason": "与画像无显性交集，交语义层判定", "rule": "无交集"}


# ═══════════════════════════════════════════════════════════
# 规则触发统计（v5.8）：零触发标红
# 目的：规则写坏了不报错、规则空转了没人知道。城市校验曾因 surface_cache 剥离 city
# 而长期空转，就是"零触发"没人看。这里对每条规则统计触发次数，0 次触发即报警。
# ═══════════════════════════════════════════════════════════

# 必须被触发的关键规则（0 次触发 = 异常，必须排查，不接受"数据刚好没有"的解释）
CRITICAL_RULES = ["城市不符", "地点未取证", "薪资门槛", "岗类排除", "薪资可谈"]

_RULE_STATS = {"calls": 0, "hits": {}, "verdicts": {}}


def _rule_registry():
    """从源码提取全部可能的 rule 名称，保证统计口径与代码一致。"""
    import re as _re
    names = _re.findall(r'"rule":\s*f?"([^"]+)"', open(__file__, "r", encoding="utf-8").read())
    out = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def rule_screen(job, profile=None):
    """带统计的规则层入口（原 _rule_screen_impl 逻辑不变）。"""
    res = _rule_screen_impl(job, profile)
    try:
        _RULE_STATS["calls"] += 1
        v = str(res.get("verdict", "?"))
        _RULE_STATS["verdicts"][v] = _RULE_STATS["verdicts"].get(v, 0) + 1
        r = str(res.get("rule", "?"))
        _RULE_STATS["hits"][r] = _RULE_STATS["hits"].get(r, 0) + 1
    except Exception:
        pass
    return res


def get_rule_stats():
    """返回统计快照（含零触发清单）。"""
    hits = dict(_RULE_STATS["hits"])
    registry = _rule_registry()
    zero = [r for r in registry if hits.get(r, 0) == 0]
    critical_zero = [r for r in CRITICAL_RULES if hits.get(r, 0) == 0]
    return {
        "source": RULES_SOURCE,
        "calls": _RULE_STATS["calls"],
        "verdicts": dict(_RULE_STATS["verdicts"]),
        "hits": hits,
        "registry": registry,
        "zero_trigger": zero,
        "critical_zero_trigger": critical_zero,
    }


def reset_rule_stats():
    _RULE_STATS["calls"] = 0
    _RULE_STATS["hits"] = {}
    _RULE_STATS["verdicts"] = {}


def report_rule_stats(verbose=True):
    """打印规则触发表；0 次触发标红，关键规则 0 触发直接告警。"""
    st = get_rule_stats()
    if verbose:
        print("=" * 60)
        print("规则层触发统计  规则源: %s  调用 %d 次" % (st["source"], st["calls"]))
        print("=" * 60)
        for r in st["registry"]:
            n = st["hits"].get(r, 0)
            flag = "[红]零触发" if n == 0 else "        "
            print("  %s %-12s %d 次" % (flag, r, n))
        print("  判定分布: %s" % st["verdicts"])
    if st["calls"] > 0 and st["critical_zero_trigger"]:
        print("  ⚠ 关键规则零触发（规则可能已失效或未被执行）: %s" % "、".join(st["critical_zero_trigger"]))
    return st


def is_hard_reject(job, profile=None):
    """便捷接口：是否命中硬排除（供 decide 二次校验用）"""
    return rule_screen(job, profile).get("verdict") == "REJECT"


if __name__ == "__main__":
    import json
    import sys
    payload = sys.stdin.read() if not sys.argv[1:] else sys.argv[1]
    try:
        data = json.loads(payload)
        jobs = data if isinstance(data, list) else [data]
    except Exception:
        print("用法: echo '[{...}]' | python job_filter.py")
        sys.exit(1)
    for j in jobs:
        r = rule_screen(j)
        print(f"[{r['verdict']:<6}] {j.get('title','')[:24]:<24} | {r['reason']}")

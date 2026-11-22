#!/usr/bin/env python3






















import os
import re

try:
    from scripts.profile_loader import USER_PROFILE, MIN_SALARY_K, TARGET_CITY
except Exception:
    USER_PROFILE, MIN_SALARY_K, TARGET_CITY = {}, 0, ""







HARD_EXCLUDE_TITLE = [

    "餐饮", "餐厅经理", "后厨", "厨师", "厨工",
    "餐饮服务员", "传菜", "洗碗", "火锅", "烧烤", "奶茶店", "饮品店", "烘焙店",

    "网咖", "网吧", "电竞馆", "网管", "棋牌", "麻将", "台球",

    "助理", "实习生", "实习", "管培实习生",
]


CORE_INDUSTRY_EXTRA = ["OTA", "民宿", "度假", "公寓", "旅游", "酒店集团", "客房", "前厅部"]


RETAIN_SIGNALS = [
    "酒店", "民宿", "文旅", "景区", "度假", "公寓", "营地", "康养",
    "会所", "集团", "综合体", "商业管理", "物业",
]


BASE_TITLE_EXCLUDE = [
    "保安", "保洁", "服务员", "收银员", "理货员", "搬运", "分拣员", "配送员",
    "外卖骑手", "网约车", "快递员", "司机", "地推", "拉新", "家教", "兼职",
    "日结", "小时工", "纯销售", "销售代表", "销售专员", "销售顾问", "房产中介",
    "保健品会销", "主播", "娱乐主播", "带货主播", "招聘顾问", "RPO", "劳务派遣",
]




DIRECTION_WORDS = [
    "运营", "投放", "投流", "直播", "电商", "私域", "社群", "增长", "策划",
    "店长", "经理", "主管", "总监", "渠道", "销售",
]

JOB_TYPE_EXCLUDE = [
    "拍剪", "剪辑", "后期制作", "后期剪辑", "视频制作", "短视频制作",
    "摄像", "摄影", "美工", "平面设计", "视觉设计", "UI设计", "插画",
    "AI训练师", "训练师", "数据标注", "标注员", "内容审核",
]


CITY_WHITELIST_EXTRA = ["新加坡", "香港", "澳门", "日本", "东京", "大阪", "京都"]


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


LOCATION_MARKERS = ["上班", "工作地", "工作地点", "工作城市", "驻场", "派驻", "常驻", "驻"]


C_EXCLUDE_COMPANY = [
    "人力资源", "人力", "劳务", "猎头", "猎聘", "人才服务", "人才中介",
    "RPO", "外包服务", "众包", "招聘服务",
]


TRAINING_ORG = [
    "培训", "教育科技", "职业培训", "驾培", "驾校", "专修学院", "技工学校",
    "考研", "公考", "留学中介",
]


NON_MONTHLY_SALARY = ["元/天", "元/时", "元/周", "元/小时", "元/次"]




ANON_COMPANY_PATTERN = re.compile(
    r"^(某|某某)(大型|知名|中型|小型|上市|外资|国有|民营|互联网|连锁)?(企业|公司|集团|单位|平台)"
    r"$|^(某|某某)(大型|知名|中型|小型|上市)(企业|公司|集团)"
    r"|^(知名|大型|上市)(企业|公司|集团)$")


SALARY_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*[Kk千]")
SALARY_SINGLE = re.compile(r"(\d+(?:\.\d+)?)\s*[Kk千]")






try:
    from scripts import rules_loader as _rules
except Exception:
    try:
        from scripts import rules_loader as _rules
    except Exception:
        try:
            import rules_loader as _rules
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












    if not title:
        return ""
    allow = set(CITY_WHITELIST_EXTRA)
    if target_city:
        allow.add(str(target_city))

    def _ok(c):
        return c not in allow


    for m in LOCATION_MARKERS:
        if target_city and f"{m}{target_city}" in title:
            return ""


    for m in LOCATION_MARKERS:
        for c in CITY_NAMES:
            if not _ok(c):
                continue
            if f"{m}{c}" in title or f"{m}：{c}" in title or f"{m}:{c}" in title:
                return c


    for seg in re.findall(r"[（(【\[]([^）)】\]]{1,14})[）)】\]]", str(title)):
        for c in CITY_NAMES:
            if c in seg and _ok(c):
                return c


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










    p = _profile(profile)
    title = str(job.get("title") or "")
    company = str(job.get("company") or "")
    salary = str(job.get("salary") or "")
    industry = str(job.get("industry") or "")
    city = str(job.get("city") or "")

    min_salary_k = p.get("min_salary_k", MIN_SALARY_K) or 0
    target_city = p.get("target_city", TARGET_CITY) or ""


    hit = _hit(salary, NON_MONTHLY_SALARY)
    if hit:
        return {"verdict": "REJECT", "reason": f"非月薪薪资「{salary[:14]}」", "rule": "非月薪"}


    hit = _hit(title, HARD_EXCLUDE_TITLE)
    if hit:
        retain = _hit(title, RETAIN_SIGNALS)
        if retain and any(w in hit for w in ("餐饮", "火锅", "烧烤", "后厨", "厨师", "厨工", "饮品店", "烘焙店")):
            return {"verdict": "REVIEW",
                    "reason": f"餐饮类但含住宿文旅信号「{'/'.join(retain[:2])}」，需语义判断",
                    "rule": "餐饮误杀保护"}
        return {"verdict": "REJECT", "reason": f"标题含硬排除词「{'/'.join(hit[:2])}」", "rule": "标题硬排除"}


    extra_t = p.get("t_exclude") or []
    hit = _hit(title, BASE_TITLE_EXCLUDE + list(extra_t))
    if hit:
        return {"verdict": "REJECT", "reason": f"标题含低价值岗词「{'/'.join(hit[:2])}」", "rule": "标题硬排除"}


    hit = _hit(title, p.get("black_title") or [])
    if hit:
        return {"verdict": "REJECT", "reason": f"命中画像 black_title「{'/'.join(hit[:2])}」", "rule": "画像黑名单"}




    hit = _hit(title, JOB_TYPE_EXCLUDE)
    if hit:
        _dir = _hit(title, DIRECTION_WORDS)
        if _dir:
            return {"verdict": "REVIEW",
                    "reason": f"岗类词「{'/'.join(hit[:2])}」与方向词「{'/'.join(_dir[:2])}」共存，交语义层判定",
                    "rule": "岗类待判"}
        return {"verdict": "REJECT", "reason": f"标题含岗类排除词「{'/'.join(hit[:2])}」", "rule": "岗类排除"}


    extra_c = p.get("c_exclude") or []
    hit = _hit(company, C_EXCLUDE_COMPANY + list(extra_c))
    if hit:
        return {"verdict": "REJECT", "reason": f"公司名含中介词「{'/'.join(hit[:2])}」", "rule": "公司硬排除"}
    if p.get("anon_company_forbidden") and ANON_COMPANY_PATTERN.match(company):
        return {"verdict": "REJECT", "reason": f"公司名匿名指代「{company[:12]}」", "rule": "匿名代招"}





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


    if p.get("training_org_rule"):
        hit = _hit(company, TRAINING_ORG) or _hit(title, TRAINING_ORG)
        if hit:
            return {"verdict": "REJECT", "reason": f"培训/教育机构「{'/'.join(hit[:2])}」", "rule": "培训机构排除"}



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


            _off_city = city_from_title(title, target_city)
            if _off_city:
                return {"verdict": "REJECT",
                        "reason": f"标题明示工作地点 {_off_city}，非{target_city}",
                        "rule": "城市不符"}
            if not any(w in (title or "") for w in ("驻外", "海外", "新加坡", "香港", "澳门", "日本")):
                return {"verdict": "REVIEW",
                        "reason": "工作地点未取证（city 字段缺失、标题无城市），需按 JD 原文核实",
                        "rule": "地点未取证"}



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









CRITICAL_RULES = ["城市不符", "地点未取证", "薪资门槛", "岗类排除", "薪资可谈"]

_RULE_STATS = {"calls": 0, "hits": {}, "verdicts": {}}


def _rule_registry():

    import re as _re
    names = _re.findall(r'"rule":\s*f?"([^"]+)"', open(__file__, "r", encoding="utf-8").read())
    out = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


def rule_screen(job, profile=None):

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
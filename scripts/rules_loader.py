# -*- coding: utf-8 -*-













import json
import os
import sys

__version__ = "1.0.0"
__updated__ = "2026-11-19"

_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")


DEFAULTS = {
    "version": "0.9.0-fallback",
    "salary": {
        "policy": "max_basis",
        "policy_text": "判薪资看区间上限：上限 >= 最低门槛即可谈；上限 < 门槛才算不达标。",
        "forbidden_expressions": ["看下限", "不看上限", "按解析区间下限"],
        "min_k_default": 8,
        "below_max_verdict": "REJECT",
        "straddle_verdict": "REVIEW",
    },
    "location": {
        "policy_text": "城市字段缺失时不得静默放行，标 REVIEW 并计数。",
        "whitelist_extra": ["新加坡", "香港", "澳门", "日本", "东京", "大阪", "京都"],
        "markers": ["上班", "工作地", "工作地点", "工作城市", "驻场", "派驻", "常驻", "驻"],
        "city_names": [],
    },
    "job_type": {
        "policy_text": "职能型岗位不投。",
        "exclude": ["拍剪", "剪辑", "后期制作", "后期剪辑", "视频制作", "短视频制作", "摄像", "摄影",
                    "美工", "平面设计", "视觉设计", "UI设计", "插画", "AI训练师", "训练师", "数据标注",
                    "标注员", "内容审核"],
        "must_not_contain": ["运营", "策划", "投放", "千川", "管理", "店长", "经理"],
    },
    "direction": {
        "policy_text": "以业务/运营/策划/内容/流程提效为主体可投；判定须引 JD 原句。",
        "generic_prefer_hint": "",
    },
    "evidence": {
        "policy_text": "每项子判定须附 JD 原句证据，需真实命中，APPROVE 每项最短 15 字。",
        "min_chars": 15,
        "fields": ["location", "function", "salary", "direction"],
        "gate_mode_default": "shadow",
        "shadow_days": 7,
    },
    "prompt": {
        "version": "2026-11-19.1",
        "file": "scripts/profile_loader.py::get_llm_prompt",
        "required_sections": ["工作地点", "岗位职能", "薪资", "方向"],
    },
}

_cache = {"data": None, "loaded": False, "error": "", "path": _RULES_PATH}


def _deep_merge(base, override):

    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force=False):

    if _cache["data"] is not None and not force:
        return _cache["data"]
    data = DEFAULTS
    loaded = False
    err = ""
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        data = _deep_merge(DEFAULTS, raw)
        loaded = True
    except FileNotFoundError:
        err = "rules.json 缺失，已退回内置默认规则"
    except Exception as e:
        err = "rules.json 解析失败(%s)，已退回内置默认规则" % e
    if err:
        sys.stderr.write("[rules] WARN %s\n" % err)
    _cache.update({"data": data, "loaded": loaded, "error": err})
    return data


def status():

    load()
    return {
        "path": _cache["path"],
        "loaded": _cache["loaded"],
        "error": _cache["error"],
        "version": _cache["data"].get("version", ""),
    }


def get(path, default=None):

    cur = load()
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def salary_text():
    return get("salary.policy_text", "")


def salary_forbidden():
    return list(get("salary.forbidden_expressions", []) or [])


def location_block():
    return load().get("location", {})


def job_type_exclude():
    return list(get("job_type.exclude", []) or [])


def evidence_min_chars(field=None):






    try:
        if field:
            by = get("evidence.min_chars_by_field", {}) or {}
            if isinstance(by, dict) and field in by:
                return int(by[field])
        return int(get("evidence.min_chars", 15))
    except Exception:
        return 15


def gate_mode_default():
    return str(get("evidence.gate_mode_default", "shadow")).lower()


def prompt_version():
    return str(get("prompt.version", "unversioned"))


if __name__ == "__main__":
    st = status()
    print(json.dumps(st, ensure_ascii=False, indent=2))
    print("salary:", salary_text()[:40], "...")
    print("job_type_exclude:", len(job_type_exclude()), "项")
# -*- coding: utf-8 -*-
"""规则单一真源加载器（rules.json）。

设计原则
--------
1. rules.json 是薪资 / 地点 / 职能 / 方向口径的唯一真源；
   job_filter（规则层）、profile_loader（提示词层）、execution-manual（文档层）都从它派生。
2. 加载失败绝不阻断主流程：退回内置默认值（DEFAULTS），并在 loaded=False 时对外暴露，
   由 tools/check_consistency.py 与自检报告标红。买家环境不允许因为缺一个文件就跑不动。
3. 所有取值函数给出稳定形状，调用方不需要判空。

版本：1.0.0 / 2026-11-19
"""

import json
import os
import sys

__version__ = "1.0.0"
__updated__ = "2026-11-19"

_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")

# 内置兜底：与 rules.json 同名键保持一致；rules.json 缺失时使用
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
    """override 覆盖 base，dict 递归合并，list 直接替换。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force=False):
    """加载规则（带缓存）。始终返回可用 dict。"""
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
    except Exception as e:  # JSON 损坏等
        err = "rules.json 解析失败(%s)，已退回内置默认规则" % e
    if err:
        sys.stderr.write("[rules] WARN %s\n" % err)
    _cache.update({"data": data, "loaded": loaded, "error": err})
    return data


def status():
    """供自检 / 一致性检查读取加载状态。"""
    load()
    return {
        "path": _cache["path"],
        "loaded": _cache["loaded"],
        "error": _cache["error"],
        "version": _cache["data"].get("version", ""),
    }


def get(path, default=None):
    """点路径取值，如 get('salary.min_k_default', 8)。"""
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
    """证据最短字数。分字段阈值优先，未配置字段回退整体阈值。

    为什么要分档：地点证据往往是「杭州西湖区」这种 5-6 字短语，若统一按 15 字卡，
    全部决策都会被判"证据过短"，shadow 期的观察数据就变成 100% 不合格的噪声，
    等于取证机制自废。
    """
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

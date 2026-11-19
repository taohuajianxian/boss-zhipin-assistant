# -*- coding: utf-8 -*-
"""decision.json 结构化校验与取证门（evidence gate）。

职责
----
1. 结构校验：APPROVE 必须给出四项子判定（工作地点 / 岗位职能 / 薪资 / 方向），
   每项含 verdict 与 evidence；结构不合格直接判失败（fail-closed）。
2. 取证校验：evidence 做文本归一化（去空白、全半角统一、标点归一）后与 JD 原文比对，
   必须真实命中；APPROVE 每项证据最短由 rules.json evidence.min_chars 指定（默认 15 字）。
3. 双模式门：shadow（默认，只记录不拦截，产出一周无证据占比）与 enforce（硬拒收）。
4. 审计留痕：每次校验写 decision_audit.jsonl 一行，字段可回溯到 prompt_version。

命令行自检：
    python decision_schema.py --selftest
"""

import json
import os
import sys
import time
import unicodedata

def _import_rules_loader():
    """多路径导入规则真源：包内优先，独立运行兜底。

    坑：原来写裸 `import rules_loader`，在 `from scripts import xxx` 场景下必然失败，
    且被 except 静默吞成 None —— 阈值、门模式、提示词全部退回硬编码默认值，
    表面一切正常。这类"静默降级"正是本次整改要根除的病根，所以这里失败必须可见。
    """
    import importlib
    _err = None
    for _mod in ("scripts.rules_loader", "rules_loader"):
        try:
            return importlib.import_module(_mod)
        except Exception as _e:  # noqa: PERF203
            _err = _e
    try:
        sys.stderr.write("[warn] rules_loader 不可用，已回退内置默认值: %s\n" % _err)
    except Exception:
        pass
    return None


rules_loader = _import_rules_loader()

__version__ = "1.0.0"
__updated__ = "2026-11-19"

SUB_FIELDS = ["location", "function", "salary", "direction"]
SUB_LABELS = {
    "location": "工作地点",
    "function": "岗位职能",
    "salary": "薪资",
    "direction": "方向",
}
VALID_DECISIONS = {"APPROVE", "REJECT", "NEED_JD"}
VALID_VERDICTS = {"PASS", "REJECT", "UNKNOWN"}

_PUNCT_MAP = {
    "，": ",", "。": ".", "；": ";", "：": ":", "（": "(", "）": ")",
    "！": "!", "？": "?", "、": ",", "\u3000": " ", "“": '"', "”": '"',
    "‘": "'", "’": "'", "—": "-", "～": "~", "【": "[", "】": "]",
}


def normalize(text):
    """文本归一化：全半角统一 + 去所有空白 + 标点归一 + 小写。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    for a, b in _PUNCT_MAP.items():
        t = t.replace(a, b)
    t = "".join(t.split())
    return t.lower()


def evidence_hit(evidence, jd_text):
    """证据是否真实出现在 JD 中（归一化后子串匹配）。返回 (hit, normalized_len)。"""
    ev = normalize(evidence)
    jd = normalize(jd_text)
    if not ev or not jd:
        return False, len(ev)
    return (ev in jd), len(ev)


def _min_chars(field=None):
    if rules_loader:
        return rules_loader.evidence_min_chars(field)
    return 15


def gate_mode(explicit=None):
    """门模式：显式 > 环境变量 BOSS_DECISION_GATE > rules.json 默认（shadow）。"""
    if explicit:
        return str(explicit).lower()
    env = os.environ.get("BOSS_DECISION_GATE", "").strip().lower()
    if env in ("shadow", "enforce"):
        return env
    if rules_loader:
        m = rules_loader.gate_mode_default()
        if m in ("shadow", "enforce"):
            return m
    return "shadow"


def validate(decision_data, jd_text, mode=None, audit_dir=None, job_id=""):
    """校验决策。返回 dict：

    {
      ok: bool,                 # 结构是否合格（enforce 下证据不合格也判 False）
      errors: [str],            # 必须修的问题
      warnings: [str],          # shadow 下记录的观察项
      mode: 'shadow'|'enforce',
      evidence_ok: bool,        # 四项证据是否全部合格
      forced_decision: str|None,# enforce 下需要强制改判时的值（'REJECT'）
      sub_report: {field: {...}}
    }
    """
    mode = gate_mode(mode)
    errors, warnings, ev_errors = [], [], []  # errors=结构错误；ev_errors=证据错误
    min_chars_default = _min_chars()

    if not isinstance(decision_data, dict):
        return {
            "ok": False, "errors": ["decision.json 不是 JSON 对象"], "warnings": [],
            "mode": mode, "evidence_ok": False, "forced_decision": "REJECT", "sub_report": {},
        }

    decision = str(decision_data.get("decision", "")).upper()
    if decision not in VALID_DECISIONS:
        errors.append("decision 取值非法: %r（应为 APPROVE/REJECT/NEED_JD）" % decision)

    sub = decision_data.get("sub") or {}
    sub_report = {}
    evidence_ok = True

    if decision == "APPROVE":
        if not isinstance(sub, dict) or not sub:
            errors.append("APPROVE 缺少 sub 子判定块（需含 location/function/salary/direction）")
            evidence_ok = False
        else:
            for f in SUB_FIELDS:
                item = sub.get(f)
                label = SUB_LABELS[f]
                if not isinstance(item, dict):
                    errors.append("缺少子判定 %s（%s）" % (f, label))
                    evidence_ok = False
                    continue
                verdict = str(item.get("verdict", "")).upper()
                ev = str(item.get("evidence", "") or "")
                if verdict not in VALID_VERDICTS:
                    errors.append("%s 的 verdict 非法: %r" % (label, verdict))
                    evidence_ok = False
                hit, ev_len = evidence_hit(ev, jd_text)
                min_chars = _min_chars(f)
                short = ev_len < min_chars
                item_ok = bool(ev) and hit and not short
                if not ev:
                    ev_errors.append("%s 未提供 evidence（须引用 JD 原句）" % label)
                elif short:
                    ev_errors.append("%s 的 evidence 仅 %d 字，低于下限 %d 字" % (label, ev_len, min_chars))
                elif not hit:
                    ev_errors.append("%s 的 evidence 未在 JD 原文中命中（疑似编造）" % label)
                if verdict == "REJECT" and item_ok:
                    # 子项判负但整体 APPROVE：逻辑矛盾
                    ev_errors.append("%s 子判定为 REJECT，整体却 APPROVE（自相矛盾）" % label)
                evidence_ok = evidence_ok and item_ok
                sub_report[f] = {
                    "label": label, "verdict": verdict, "evidence_len": ev_len,
                    "hit": hit, "ok": item_ok,
                }
    elif decision == "REJECT":
        if not str(decision_data.get("reason", "")).strip():
            errors.append("REJECT 必须给出 reason")
        # v5.8 修订：REJECT 同样强制取证。
        # 原因：旧基线的最大毛病不是漏放而是「误杀」（62 条里误杀 23 条，私域/小红书/猫超运营
        # 全被拒）。拒绝不取证 = 误杀不可审计，也无法证伪。规则：判负的那个子项必须引 JD 原句。
        sub = sub if isinstance(sub, dict) else {}
        neg_fields = []
        any_hit = False
        for f in SUB_FIELDS:
            item = sub.get(f)
            if not isinstance(item, dict):
                continue
            verdict = str(item.get("verdict", "")).upper()
            ev = str(item.get("evidence", "") or "")
            hit, ev_len = evidence_hit(ev, jd_text) if ev else (False, 0)
            min_chars = _min_chars(f)
            short = bool(ev) and ev_len < min_chars
            item_ok = bool(ev) and hit and not short
            any_hit = any_hit or item_ok
            if verdict == "REJECT":
                neg_fields.append(f)
            if ev and not item_ok:
                if short:
                    ev_errors.append("%s 的 evidence 仅 %d 字，低于下限 %d 字" % (SUB_LABELS[f], ev_len, min_chars))
                elif not hit:
                    ev_errors.append("%s 的 evidence 未在 JD 原文中命中（疑似编造）" % SUB_LABELS[f])
            sub_report[f] = {"label": SUB_LABELS[f], "verdict": verdict,
                             "evidence_len": ev_len, "hit": hit, "ok": item_ok}
        # 未给出任何子判定 = 老格式 REJECT，无法审计，记观察项
        if not sub_report:
            ev_errors.append("REJECT 未提供 sub 子判定（无法证明拒绝依据，建议按四项子判定标注）")
        else:
            # 判负子项必须有命中证据；全无命中说明拒绝依据不在 JD 里
            for f in neg_fields:
                if not sub_report.get(f, {}).get("ok"):
                    ev_errors.append("%s 判负但无命中证据（拒绝依据未落到 JD 原文）" % SUB_LABELS[f])
            if not any_hit and not neg_fields:
                ev_errors.append("REJECT 无任何子判定命中 JD 原文（拒绝依据不可追溯）")
    else:  # NEED_JD
        pass

    # 门控
    # 结构错误（decision 非法 / 缺子判定块 / 缺字段 / verdict 非法 / REJECT 无理由）：任何模式都硬失败
    # 证据错误（未提供 / 过短 / 未命中 / 子项矛盾）：enforce 下硬失败；shadow 下仅记录观察
    if mode == "enforce":
        ok = (not errors) and (evidence_ok or decision != "APPROVE")
    else:
        ok = not errors
        warnings.extend(ev_errors)
        if ev_errors:
            warnings.append("shadow 模式：本轮不拦截，仅记录（先修提示词，满一周再看无证据占比）")
    forced = "REJECT" if not ok else None
    if ok and ev_errors and mode == "enforce":
        warnings.extend(ev_errors)  # 不会发生：enforce 下证据错误已拦截
    report_errors = list(errors) + (list(ev_errors) if mode == "enforce" else [])

    report = {
        "ok": ok,
        "errors": report_errors,
        "warnings": warnings,
        "mode": mode,
        "evidence_ok": evidence_ok,
        "forced_decision": forced,
        "sub_report": sub_report,
        "prompt_version": str(decision_data.get("prompt_version", "") or ""),
    }
    if audit_dir:
        audit_write(audit_dir, job_id, decision, report)
    return report


def audit_write(audit_dir, job_id, decision, report):
    """写审计行（JSONL），失败不影响主流程。"""
    try:
        if not os.path.isdir(audit_dir):
            os.makedirs(audit_dir, exist_ok=True)
        path = os.path.join(audit_dir, "decision_audit.jsonl")
        row = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "jobId": job_id,
            "decision": decision,
            "mode": report.get("mode"),
            "ok": report.get("ok"),
            "evidence_ok": report.get("evidence_ok"),
            "prompt_version": report.get("prompt_version", ""),
            "errors": report.get("errors", []),
            "warnings": report.get("warnings", []),
            "sub": report.get("sub_report", {}),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        sys.stderr.write("[decision_schema] audit 写入失败: %s\n" % e)
        return False


def summarize_audit(audit_dir):
    """汇总审计：总次数、APPROVE 中无证据占比（shadow 期核心指标）。"""
    path = os.path.join(audit_dir, "decision_audit.jsonl")
    total = approve = approve_no_ev = 0
    if not os.path.exists(path):
        return {"total": 0, "approve": 0, "approve_no_evidence": 0, "approve_no_evidence_ratio": 0.0}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            total += 1
            if str(row.get("decision", "")).upper() == "APPROVE":
                approve += 1
                if not row.get("evidence_ok"):
                    approve_no_ev += 1
    ratio = round(approve_no_ev / approve, 4) if approve else 0.0
    return {
        "total": total, "approve": approve, "approve_no_evidence": approve_no_ev,
        "approve_no_evidence_ratio": ratio,
    }


# ═══════════════════════════════════════════════════════════
# 自检：内置正反例，不依赖任何外部数据
# ═══════════════════════════════════════════════════════════

_JD = (
    "岗位职责：负责酒店民宿在抖音、携程等平台的运营，统筹千川信息流投放与私域承接；"
    "工作地点：杭州西湖区；薪资范围 7-9K；要求有文旅景区或酒店集团运营经验。"
)

_CASES = [
    ("全项合格", {
        "decision": "APPROVE",
        "sub": {
            "location": {"verdict": "PASS", "evidence": "工作地点：杭州西湖区；薪资范围 7-9K"},
            "function": {"verdict": "PASS", "evidence": "负责酒店民宿在抖音、携程等平台的运营，统筹千川信息流投放"},
            "salary": {"verdict": "PASS", "evidence": "薪资范围 7-9K；要求有文旅景区或酒店集团运营经验"},
            "direction": {"verdict": "PASS", "evidence": "要求有文旅景区或酒店集团运营经验"},
        },
        "prompt_version": "2026-11-19.1",
    }, True),
    ("证据编造", {
        "decision": "APPROVE",
        "sub": {
            "location": {"verdict": "PASS", "evidence": "工作地点：杭州钱江新城核心商务区"},
            "function": {"verdict": "PASS", "evidence": "负责酒店民宿在抖音、携程等平台的运营"},
            "salary": {"verdict": "PASS", "evidence": "薪资范围 7-9K"},
            "direction": {"verdict": "PASS", "evidence": "要求有文旅景区或酒店集团运营经验"},
        },
    }, False),
    ("证据过短", {
        "decision": "APPROVE",
        "sub": {
            "location": {"verdict": "PASS", "evidence": "杭州西湖"},
            "function": {"verdict": "PASS", "evidence": "负责酒店民宿在抖音、携程等平台的运营"},
            "salary": {"verdict": "PASS", "evidence": "薪资范围 7-9K"},
            "direction": {"verdict": "PASS", "evidence": "要求有文旅景区或酒店集团运营经验"},
        },
    }, False),
    ("缺子判定", {
        "decision": "APPROVE",
        "sub": {"location": {"verdict": "PASS", "evidence": "工作地点：杭州西湖区"}},
    }, False),
    ("REJECT 无理由", {"decision": "REJECT"}, False),
    ("REJECT 有理由", {"decision": "REJECT", "reason": "岗类为剪辑，不匹配"}, True),
]


def _selftest():
    print("=" * 64)
    print("decision_schema 自检（enforce 模式）")
    print("=" * 64)
    fails = 0
    for name, data, expect in _CASES:
        rep = validate(data, _JD, mode="enforce")
        got = bool(rep["ok"])
        flag = "PASS" if got == expect else "FAIL"
        if got != expect:
            fails += 1
        print("[%s] %-10s ok=%-5s 期望=%-5s forced=%s" % (flag, name, got, expect, rep["forced_decision"]))
        if got != expect or got:
            for e in rep["errors"][:2]:
                print("         · " + e)
    # shadow 模式：同样的证据编造不应拦截
    rep = validate(_CASES[1][1], _JD, mode="shadow")
    shadow_ok = rep["ok"] is True and rep["mode"] == "shadow"
    print("[%s] shadow-不拦截 ok=%s warnings=%d" % ("PASS" if shadow_ok else "FAIL", rep["ok"], len(rep["warnings"])))
    if not shadow_ok:
        fails += 1
    print("-" * 64)
    print("自检结果：%s（失败 %d 项）" % ("全部通过" if fails == 0 else "存在失败", fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    else:
        print(__doc__)

# 固定结构字段（v5.8）：decision 必须是这四项齐全，缺项在 shadow 阶段记结构不完整
REQUIRED_FIELDS = ("decision", "prompt_version")
SUB_KEYS = ("location", "function", "salary", "direction")


def missing_fields(decision_data):
    """返回缺失字段名列表，供写决策时即时提示。"""
    miss = [k for k in REQUIRED_FIELDS if not decision_data.get(k)]
    sub = decision_data.get("sub") or {}
    if not isinstance(sub, dict):
        miss.append("sub(非dict)")
    else:
        miss += ["sub.%s" % k for k in SUB_KEYS if not sub.get(k)]
    if not decision_data.get("evidence"):
        miss.append("evidence")
    return miss

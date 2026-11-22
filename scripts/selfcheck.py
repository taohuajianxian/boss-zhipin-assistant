# -*- coding: utf-8 -*-





















import os
import re
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPTS_DIR)
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

FORBIDDEN_MODULES = ("judge_baseline", "replay", "goldset_build")
FORBIDDEN_SOURCE_PATTERNS = [
    r"^\s*import\s+judge_baseline",
    r"^\s*from\s+judge_baseline\s+import",
    r"^\s*from\s+eval[\s.]",
    r"^\s*import\s+eval\b",
    r"sys\.path.*['\"]eval['\"]",
]


def _scan_source():
    hits = []
    for fn in sorted(os.listdir(SCRIPTS_DIR)):
        if not fn.endswith(".py"):
            continue
        path = os.path.join(SCRIPTS_DIR, fn)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            for pat in FORBIDDEN_SOURCE_PATTERNS:
                if re.search(pat, line):
                    hits.append("%s:%d 生产代码引用离线评测模块: %s" % (fn, i, line.strip()[:80]))
    return hits


def _scan_loaded_modules():
    hits = []
    for name in list(sys.modules):
        base = name.split(".")[-1]
        if base in FORBIDDEN_MODULES:
            hits.append("运行时已加载离线评测模块: %s" % name)
    return hits


def _check_rules():
    warns = []
    try:
        from scripts import rules_loader
        st = rules_loader.status()
        if not st.get("loaded"):
            warns.append("rules.json 未加载，已回退内置常量（不影响运行，但规则无单一真源）")
        elif not st.get("version"):
            warns.append("rules.json 缺少 version 字段")
    except Exception as e:
        warns.append("rules_loader 不可用: %s" % e)
    return warns


def _check_prompt():
    warns = []
    try:
        from scripts import profile_loader
        meta = profile_loader.get_prompt_meta() if hasattr(profile_loader, "get_prompt_meta") else {}
        if not meta.get("prompt_version"):
            warns.append("提示词无版本号，决策无法回溯到具体提示词")
        if "看下限" in (profile_loader.get_llm_prompt() or ""):
            warns.append("提示词仍含「看下限」旧口径，与 rules.json 的 max_basis 冲突")
    except Exception as e:
        warns.append("提示词自检异常: %s" % e)
    return warns


def run(verbose=True):
    critical = []
    critical += _scan_loaded_modules()
    critical += _scan_source()
    warns = _check_rules() + _check_prompt()
    if verbose:
        print("[自检] 生产隔离: %s" % ("通过" if not (_scan_loaded_modules() + _scan_source()) else "失败"))
        for w in warns:
            print("[自检] · %s" % w)
    return {"critical": critical, "warnings": warns, "ok": not critical}


def assert_clean():

    res = run(verbose=True)
    if res["critical"]:
        print("[自检] ✗ 致命问题，已中止：")
        for c in res["critical"]:
            print("   - %s" % c)
        raise SystemExit(2)
    return True


if __name__ == "__main__":
    res = run(verbose=True)
    print("-" * 50)
    print("结论：%s" % ("通过" if res["ok"] else "存在致命问题"))
    if res["critical"]:
        for c in res["critical"]:
            print("  ✗ %s" % c)
    else:
        print("  ✓ 未发现离线评测代码污染生产链路")
    sys.exit(0 if res["ok"] else 2)
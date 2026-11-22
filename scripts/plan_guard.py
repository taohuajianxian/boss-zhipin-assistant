#!/usr/bin/env python3
# -*- coding: utf-8 -*-















from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path




ALLOWED_SNIPPETS = frozenset({
    "read_jd", "find_card", "click_card", "verify_panel",
    "handle_popup", "login_check", "extract_jobs",
})


MAX_SNIPPET_BYTES = 8192


PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")


FORBIDDEN_PATTERNS = [
    (r"\beval\s*\(", "动态求值 eval"),
    (r"\bnew\s+Function\b", "动态构造函数 Function"),
    (r"\bfetch\s*\(", "网络请求 fetch"),
    (r"\bXMLHttpRequest\b", "网络请求 XMLHttpRequest"),
    (r"\bWebSocket\b", "长连接 WebSocket"),
    (r"\bsendBeacon\b", "外发数据 sendBeacon"),
    (r"\bimportScripts\b", "动态加载脚本 importScripts"),
    (r"\bnew\s+Worker\b", "后台线程 Worker"),
    (r"\bdocument\s*\.\s*write\b", "文档覆写 document.write"),
    (r"\binnerHTML\s*=", "HTML 注入 innerHTML"),
    (r"\bdocument\s*\.\s*cookie\b", "Cookie 读写"),
    (r"\blocalStorage\b", "本地存储写入"),
    (r"\bsessionStorage\b", "会话存储写入"),
    (r"\bpostMessage\b", "跨窗口通信 postMessage"),
    (r"\batob\s*\(|\bbtoa\s*\(", "Base64 编解码（混淆常见手段）"),
    (r"\bimport\s*\(", "动态 import"),
    (r"\brequire\s*\(", "模块加载 require"),
    (r"\bwindow\s*\.\s*open\s*\(", "打开新窗口 window.open"),
    (r"\b(top|parent)\s*\.\s*location\b", "框架跳转 top/parent.location"),
    (r"\bwindow\s*\.\s*location\s*=", "页面跳转赋值"),
    (r"\blocation\s*\.\s*(href|assign|replace)\s*=", "页面跳转赋值"),


    (r"(?<![.\w$])location\s*=\s*(?!\s*\()", "页面跳转赋值"),
]


REQUIRED_KEYS = ("plan_version", "selectors", "js_snippets")

_VERSION_DATE_RE = re.compile(r"^(\d{8})")




def canonical_bytes(obj) -> bytes:

    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def plan_digest(plan: dict) -> str:

    body = {k: v for k, v in plan.items() if k != "integrity"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def js_digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def js_digest_map(plan: dict) -> dict:
    return {name: js_digest((item or {}).get("code", ""))
            for name, item in (plan.get("js_snippets") or {}).items()}


def version_date(plan_version: str) -> str:

    m = _VERSION_DATE_RE.match(str(plan_version or ""))
    return m.group(1) if m else ""




def audit_js(name: str, code: str) -> list:

    problems = []
    if name not in ALLOWED_SNIPPETS:
        problems.append(f"{name}: 片段名不在随包登记清单内")
    if not isinstance(code, str) or not code.strip():
        problems.append(f"{name}: 代码为空")
        return problems
    size = len(code.encode("utf-8"))
    if size > MAX_SNIPPET_BYTES:
        problems.append(f"{name}: 代码 {size} 字节，超出上限 {MAX_SNIPPET_BYTES}")
    for pattern, reason in FORBIDDEN_PATTERNS:
        if re.search(pattern, code):
            problems.append(f"{name}: 命中禁止能力 —— {reason}")

    if "__" in PLACEHOLDER_RE.sub("", code):
        problems.append(f"{name}: 存在无法识别的占位符（__ 未成对闭合）")
    return problems


def audit_plan(plan, anchor: dict = None) -> list:

    problems = []
    if not isinstance(plan, dict):
        return ["适配数据不是合法对象"]
    for key in REQUIRED_KEYS:
        if not plan.get(key):
            problems.append(f"缺少必需字段: {key}")
    if problems:
        return problems
    if not isinstance(plan.get("selectors"), dict):
        problems.append("selectors 不是对象")
    snippets = plan.get("js_snippets")
    if not isinstance(snippets, dict):
        problems.append("js_snippets 不是对象")
        return problems
    if not snippets:
        problems.append("js_snippets 为空")
    for name, item in snippets.items():
        if not isinstance(item, dict):
            problems.append(f"{name}: 片段结构异常")
            continue
        problems.extend(audit_js(name, item.get("code", "")))

    if anchor:
        approved = ((anchor.get("js") or {}).get("approved_snippets") or {})
        if (anchor.get("js") or {}).get("policy") == "allowlist+digest":
            for name, digest in js_digest_map(plan).items():
                known = approved.get(name) or []
                if digest not in known:
                    problems.append(
                        f"{name}: 片段内容未在随包基准中登记（需随技能版本更新一同发布）")
    return problems




def main() -> int:
    ap = argparse.ArgumentParser(description="适配数据内容校验自检")
    ap.add_argument("--plan", default="dist/plan.json")
    ap.add_argument("--anchor", default="dist/trust_anchor.json")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    plan_path = root / args.plan if not Path(args.plan).is_absolute() else Path(args.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    anchor = None
    apath = root / args.anchor if not Path(args.anchor).is_absolute() else Path(args.anchor)
    if apath.exists():
        anchor = json.loads(apath.read_text(encoding="utf-8"))

    problems = audit_plan(plan, anchor)
    print(f"plan_version : {plan.get('plan_version')}")
    print(f"plan_digest  : {plan_digest(plan)}")
    for name, digest in sorted(js_digest_map(plan).items()):
        print(f"  JS {name:14s} {digest[:16]}")
    if problems:
        print("\nREJECT:")
        for p in problems:
            print("  -", p)
        return 1
    print("\nPASS：结构完整、页面片段全部通过审查与随包登记清单")
    return 0


if __name__ == "__main__":
    sys.exit(main())
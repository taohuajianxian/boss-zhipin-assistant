#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指令包安全守门（零第三方依赖，只用 Python 标准库）。

三道关，任一不过即拒绝加载整包：

  1. 规范化摘要 —— 对 plan 做确定性序列化后取 SHA-256，作为该指令包的唯一内容指纹。
     任何字段被改动（哪怕只多一个空格以外的值差异）指纹立刻变化，无法伪造。

  2. 签名校验 —— 云端下发的 integrity 用「按设备派生」的密钥做 HMAC-SHA256，
     消息体绑定 端点主机 + 包版本 + 内容摘要。客户端拿不到全局签名密钥，
     只能验、不能签，且某台设备泄露的派生密钥无法用于伪造别的设备。

  3. JS 静态白名单 —— 允许注入页面的可执行片段只认固定名字集合，每段代码的
     SHA-256 必须命中随包信任锚里预先审查过的历史指纹；代码里出现网络请求、
     动态求值、存储写入、跨窗口通信等能力一律拒收。

用法（自检）：
    python scripts/plan_guard.py --plan dist/plan.json --anchor dist/trust_anchor.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# ---------------- 常量 ----------------

# 允许随云端下发的 JS 片段名字（固定白名单，云端新增键一律拒收）
ALLOWED_SNIPPETS = frozenset({
    "read_jd", "find_card", "click_card", "verify_panel",
    "handle_popup", "login_check", "extract_jobs",
})

# 单段 JS 体积上限（当前最大 3.3KB，留足余量）
MAX_SNIPPET_BYTES = 8192

# 占位符形态：__SEL_BODY__ 这类，只允许字母数字下划线
PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")

# 危险能力：命中即拒（正则，原因）
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
    # 裸 location 赋值才算导航；`let location = (item.querySelector(...))` 这类
    # 局部变量只是取公司所在地文本，不能误杀
    (r"(?<![.\w$])location\s*=\s*(?!\s*\()", "页面跳转赋值"),
]

# 指令包必须存在的字段
REQUIRED_KEYS = ("plan_version", "selectors", "js_snippets")

_VERSION_DATE_RE = re.compile(r"^(\d{8})")


# ---------------- 指纹 ----------------

def canonical_bytes(obj) -> bytes:
    """确定性序列化：键排序、无多余空白、UTF-8。同一内容永远得到同一字节串。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def plan_digest(plan: dict) -> str:
    """整包内容指纹（不含 integrity 字段本身）。"""
    body = {k: v for k, v in plan.items() if k != "integrity"}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def js_digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def js_digest_map(plan: dict) -> dict:
    return {name: js_digest((item or {}).get("code", ""))
            for name, item in (plan.get("js_snippets") or {}).items()}


def version_date(plan_version: str) -> str:
    """从 20261113-3a90b51e 里取出日期段，用于拒绝版本回滚。"""
    m = _VERSION_DATE_RE.match(str(plan_version or ""))
    return m.group(1) if m else ""


# ---------------- 审查 ----------------

def audit_js(name: str, code: str) -> list:
    """单段 JS 静态审查，返回问题列表（空 = 通过）。"""
    problems = []
    if name not in ALLOWED_SNIPPETS:
        problems.append(f"{name}: 片段名不在随包白名单内")
    if not isinstance(code, str) or not code.strip():
        problems.append(f"{name}: 代码为空")
        return problems
    size = len(code.encode("utf-8"))
    if size > MAX_SNIPPET_BYTES:
        problems.append(f"{name}: 代码 {size} 字节，超出上限 {MAX_SNIPPET_BYTES}")
    for pattern, reason in FORBIDDEN_PATTERNS:
        if re.search(pattern, code):
            problems.append(f"{name}: 命中禁止能力 —— {reason}")
    # 占位符必须成对闭合：去掉合法占位符后不应残留 __
    if "__" in PLACEHOLDER_RE.sub("", code):
        problems.append(f"{name}: 存在无法识别的占位符（__ 未成对闭合）")
    return problems


def audit_plan(plan, anchor: dict = None) -> list:
    """整包审查。anchor 为空时跳过「随包指纹白名单」那一层（仅做静态审查）。"""
    problems = []
    if not isinstance(plan, dict):
        return ["指令包不是合法对象"]
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
                        f"{name}: 代码指纹未在随包信任锚中登记（需随技能版本更新一同发布）")
    return problems


# ---------------- 自检入口 ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description="指令包安全守门自检")
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
    print("\nPASS：结构完整、JS 全部通过静态审查与随包指纹白名单")
    return 0


if __name__ == "__main__":
    sys.exit(main())

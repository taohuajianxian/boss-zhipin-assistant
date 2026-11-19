#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块 E: 推荐页内原地点击卡片读右侧 JD

知识/执行分离：
  本文件**不再包含任何 BOSS直聘选择器字面量与内联 JS**。
  所有选择器、JS 模板、点击时序均来自云端指令包 plan.json：
      cloud_client.load_plan() → render_js(name, **选择器) → evaluate(js)
  页面结构一变，新版指令包会自动下发，用户端不用换包、不用改代码。

  指令包来源：激活后 ~/.boss_zhipin_deliver/plan.json；
  未激活 / 离线时回退到随包附带的 <SKILL_DIR>/dist/plan.json（免费体验版）。

核心流程（编排逻辑留本地）：
  1. 在左侧列表中按 jobId 找到目标卡片
  2. 滚动到可视区域
  3. 原生 click 选中该卡 → 右侧面板就地切换出 JD
  4. 校验右侧面板确实切到了目标岗位
  5. 读取右侧 JD 全文

独立可运行:
    python jd_reader.py <jobId> --output jd_<jobId>.json

也可被其他模块 import:
    from scripts.jd_reader import click_card_inplace, read_jd_from_panel
"""

import json
import time
import sys
import os

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts import cloud_client as cc
from scripts.webbridge_client import evaluate, decrypt_salary
from scripts.stop_codes import risk_scan


# ═══════════════════════════════════════════════════════════
# 云端指令包 → JS
# ═══════════════════════════════════════════════════════════

def _plan_or_raise():
    plan, source, _ = cc.load_plan()
    if plan is None:
        raise RuntimeError(
            "指令包不可用：既没有云端缓存，也找不到 dist/plan.json。"
            "请确认技能包完整，或执行 python scripts/cloud_client.py --sync"
        )
    return plan, source


def _js_vars(plan, **extra):
    """把云端选择器摊平成 render_js 需要的 __XXX__ 变量。"""
    sel = cc.selectors(plan=plan)
    detail = sel.get("detail", {})
    listing = sel.get("list", {})
    login = sel.get("login", {})
    variables = {
        "SEL_BODY": detail.get("body"),
        "SEL_TITLE": detail.get("title"),
        "SEL_SALARY": detail.get("salary"),
        "SEL_TAGS": detail.get("tags"),
        "SEL_BOSS": detail.get("boss"),
        "SEL_PANEL": detail.get("panel"),
        "SEL_BODY_CONTAINER": detail.get("body_container"),
        "SEL_SECTION": detail.get("section"),
        "SEL_CARD_LINK": listing.get("card_link"),
        "SEL_CARD_CONTAINER": listing.get("card_container"),
        "SEL_CARD_TITLE": listing.get("card_title"),
        "SEL_USER": login.get("user"),
        "SEL_LOGIN_BUTTON": login.get("login_button"),
        "POPUP_SELECTORS": cc.popup_selectors(plan=plan),
    }
    variables.update(extra)
    missing = [k for k, v in variables.items() if v is None or v == ""]
    if missing:
        raise RuntimeError(f"指令包缺少必要字段: {', '.join(missing)}")
    return variables


def _js(name, plan, **extra):
    return cc.render_js(name, plan=plan, **_js_vars(plan, **extra))


# ═══════════════════════════════════════════════════════════
# 卡片定位与点击
# ═══════════════════════════════════════════════════════════

def click_card_inplace(job_id, verify=True, max_wait=5):
    """在推荐页左侧列表中原地点击指定岗位卡片。

    返回: {"ok": bool, "detail": str, "title": str}
    """
    plan, _ = _plan_or_raise()

    # 1. 定位卡片并滚动到可视区域
    find_raw = evaluate(_js("find_card", plan, JID=job_id)) or "{}"
    try:
        find_result = json.loads(find_raw)
    except (json.JSONDecodeError, TypeError):
        return {"ok": False, "detail": f"JS解析失败: {find_raw[:100]}", "title": ""}

    if not find_result.get("found"):
        return {"ok": False,
                "detail": f"未找到 jobId={job_id} 的卡片: {find_result.get('detail', '?')}",
                "title": ""}
    title = find_result.get("title", "")

    # 2. 等待滚动完成
    time.sleep(1.5)

    # 3. 原生点击该卡片
    click_result = evaluate(_js("click_card", plan, JID=job_id)) or "not_found"
    if click_result == "not_found":
        return {"ok": False, "detail": "点击时未找到卡片", "title": title}
    if click_result == "no_card_container":
        return {"ok": False, "detail": "找到链接但未找到卡片容器", "title": title}

    # 4. 等待右侧面板更新
    time.sleep(2)

    # 5. 校验右侧面板确实切到了目标岗位
    if verify:
        checked = _verify_panel(job_id, max_wait, plan=plan)
        if not checked["ok"]:
            return {"ok": False,
                    "detail": f"右侧面板校验失败: {checked['detail']}",
                    "title": title}

    return {"ok": True, "detail": "卡片已点击，右侧面板已切换", "title": title}


def _verify_panel(job_id, max_wait=5, plan=None):
    """校验右侧面板确实切换到了目标岗位。返回 {"ok": bool, "detail": str}"""
    if plan is None:
        plan, _ = _plan_or_raise()
    script = _js("verify_panel", plan, JID=job_id)
    deadline = time.time() + max_wait

    while time.time() < deadline:
        raw = evaluate(script) or '{"ok":false}'
        try:
            data = json.loads(raw)
            if data.get("ok"):
                return {"ok": True, "detail": f"校验通过 ({data.get('method', '?')})"}
        except (json.JSONDecodeError, TypeError):
            pass
        time.sleep(1)

    return {"ok": False, "detail": f"等待{max_wait}s未校验到目标岗位"}


# ═══════════════════════════════════════════════════════════
# JD 内容读取
# ═══════════════════════════════════════════════════════════

EMPTY_JD = {
    "jd_text": "", "job_title": "", "company": "", "salary": "",
    "salary_decrypted": "", "city": "", "experience": "", "degree": "",
    "boss_name": "", "boss_title": "", "full_text": "",
}


def read_jd_from_panel():
    """从右侧面板读取 JD 全文。字段结构与 v5 完全一致，便于上层无感升级。"""
    plan, _ = _plan_or_raise()
    raw = evaluate(_js("read_jd", plan)) or "{}"
    try:
        result = json.loads(raw)
        result["salary_decrypted"] = decrypt_salary(result.get("salary", ""))
        for key, default in EMPTY_JD.items():
            result.setdefault(key, default)
        return result
    except (json.JSONDecodeError, TypeError):
        return dict(EMPTY_JD)


def _risk_of_jd(jd):
    """P0-1：JD 页面文本风控扫描
    返回 (hit, detail)；强字样（访问受限/验证码/人机验证…）即视为风控拦截。
    """
    text = " ".join(str(jd.get(k) or "") for k in
                    ("full_text", "jd_text", "job_title", "company"))
    level, pattern = risk_scan(text)
    if level == "strong":
        return True, f"风控字样命中: {pattern}"
    if level == "weak":
        return False, f"风控弱字样命中: {pattern}"
    return False, ""

def read_jd_for_job(job_id):
    """完整流程: 点击卡片 → 读 JD。返回 {"ok", "detail", "job_id", "jd"}"""
    click_result = click_card_inplace(job_id)
    if not click_result["ok"]:
        return {"ok": False, "detail": click_result["detail"],
                "job_id": job_id, "jd": {}, "risk_control": False}

    jd = read_jd_from_panel()
    risk_hit, risk_detail = _risk_of_jd(jd)
    if risk_hit:
        return {"ok": False, "detail": f"risk_control: {risk_detail}",
                "job_id": job_id, "jd": jd, "risk_control": True}
    if not jd.get("jd_text") and not jd.get("full_text"):
        return {"ok": False,
                "detail": "右侧面板无 JD 内容（可能页面未渲染完或岗位已关闭）",
                "job_id": job_id, "jd": jd, "risk_control": False}
    return {"ok": True, "detail": "JD 读取成功", "job_id": job_id, "jd": jd,
            "risk_control": False}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="推荐页内原地点击卡片读 JD")
    parser.add_argument("jobId", help="加密岗位 ID")
    parser.add_argument("--output", help="输出 JSON 文件路径 (可选)")
    parser.add_argument("--plan-status", action="store_true", help="只显示当前指令包来源")
    args = parser.parse_args()

    if args.plan_status:
        print(json.dumps(cc.plan_status(), ensure_ascii=False, indent=2))
        return

    print(f"[JD读取] jobId={args.jobId}")
    print("-" * 50)

    result = read_jd_for_job(args.jobId)
    if result.get("risk_control"):
        print("[风控] ⛔ 页面命中风控字样，立即停止：禁止继续读 JD / 投递 / 重试")
        print(f"[风控] {result['detail']}")
        print("[风控] 请人工到浏览器确认账号状态并完成验证，再执行 "
              "python serial_loop.py --step init --ack-risk")
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"[风控] 现场已保存至 {args.output}")
        sys.exit(3)
    if result["ok"]:
        jd = result["jd"]
        print(f"岗位: {jd.get('job_title', '?')}")
        print(f"公司: {jd.get('company', '?')}")
        print(f"薪资: {jd.get('salary', '?')} (解密: {jd.get('salary_decrypted', '?')})")
        print(f"城市: {jd.get('city', '?')}")
        print(f"经验: {jd.get('experience', '?')}")
        print(f"学历: {jd.get('degree', '?')}")
        print(f"招聘者: {jd.get('boss_name', '?')} - {jd.get('boss_title', '?')}")
        print(f"\nJD 正文 ({len(jd.get('jd_text', ''))} 字):")
        print(jd.get('jd_text', '(空)')[:500])
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"\n已保存至 {args.output}")
    else:
        print(f"失败: {result['detail']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
























import time





STOP_CODES = {
    "risk_control": {
        "label": "平台风控拦截",
        "manual": True,
        "hint": "页面出现风控字样，必须人工到浏览器确认账号状态并完成验证后再继续；"
                "脚本不做任何自动重试",
    },
    "comm_limit": {
        "label": "沟通次数软上限(120)",
        "manual": True,
        "hint": "120 次沟通额度已用，仍可搜索但无法新沟通；确认是否换号或等次日",
    },
    "daily_limit": {
        "label": "每日硬顶(150)",
        "manual": False,
        "hint": "达到平台每日硬顶，当天不再投递，次日重新 init 即可",
    },
    "target_reached": {
        "label": "投递目标达成",
        "manual": False,
        "hint": "已达每日目标份数，正常收工",
    },
    "consecutive_unknown": {
        "label": "连续未知状态熔断",
        "manual": True,
        "hint": "连续多条投递结果无法确认，疑似遮罩或页面异常，需人工看一眼页面",
    },
    "auth_fail": {
        "label": "登录态失效",
        "manual": True,
        "hint": "登录过期或被踢，需人工重新登录后 init；脚本不自动重登",
    },
    "selector_broken": {
        "label": "页面结构异常",
        "manual": True,
        "hint": "沟通按钮/卡片持续定位失败，疑似 BOSS 改版，需人工核对选择器",
    },
}


MANUAL_CODES = {code for code, meta in STOP_CODES.items() if meta["manual"]}


SOFT_CODES = set(STOP_CODES) - MANUAL_CODES







RISK_STRONG_PATTERNS = [
    "访问受限",
    "访问被拒绝",
    "访问过于频繁",
    "操作过于频繁",
    "操作太频繁",
    "请求过于频繁",
    "异常行为",
    "检测到异常",
    "账号异常",
    "账号存在风险",
    "账号被限制",
    "账号已被限制",
    "安全验证",
    "人机验证",
    "滑块验证",
    "请拖动滑块",
    "请完成验证",
    "验证码",
    "IP异常",
    "ip异常",
    "异地登录",
    "环境异常",
]


RISK_WEAK_PATTERNS = [
    "请稍后再试",
    "请稍后重试",
    "系统繁忙",
    "服务繁忙",
    "网络繁忙",
]


RISK_WEAK_STREAK_LIMIT = 2






def risk_scan(text):







    if not text:
        return None, ""
    low = text
    for p in RISK_STRONG_PATTERNS:
        if p in low:
            return "strong", p
    for p in RISK_WEAK_PATTERNS:
        if p in low:
            return "weak", p
    return None, ""


def new_stop_record(code, detail=""):






    meta = STOP_CODES.get(code) or {
        "label": f"未知原因({code})",
        "manual": True,
        "hint": "未知原因码，按需人工确认",
    }
    return {
        "stop_reason": code,
        "stop_label": meta["label"],
        "stop_detail": detail or "",
        "stop_manual": bool(meta["manual"]),
        "stop_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def describe(code, detail=""):

    if not code:
        return "无停机原因码"
    meta = STOP_CODES.get(code) or {"label": f"未知原因({code})", "hint": ""}
    text = f"[{code}] {meta['label']}"
    if detail:
        text += f" - {detail}"
    return text


def hint_of(code):

    meta = STOP_CODES.get(code) or {}
    return meta.get("hint", "")


def is_manual(code):

    return code in MANUAL_CODES
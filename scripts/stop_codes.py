# -*- coding: utf-8 -*-
"""停机原因码（stop reason codes）+ 风控字样词表

P0-1 功能：风控字样立即停 + 停机原因码落库。

设计原则
--------
1. 词表集中在本模块，deliver_engine / serial_loop 共用，禁止各自硬编码。
2. 任何停机都必须落库一个原因码，便于买家人工判断"要不要继续、要不要换号、
   要不要等明天"，避免"跑着跑着没动静了"的哑停。
3. risk_control / auth_fail / selector_broken 属人工介入项（manual=True）：
   落库后流水线后续步骤必须被 serial_loop._halt_guard() 拦下，禁止自动重试，
   否则越跑越容易把账号跑废。

原因码枚举（与 plan 侧约定一致）
-------------------------------
risk_control        平台风控拦截（访问受限 / 异常行为 / 验证码 / 人机验证）
comm_limit          沟通次数软上限（120 次档，仍可搜索但不能新沟通）
daily_limit         每日硬顶（150 次档，当天不能继续）
target_reached      投递目标达成
consecutive_unknown 连续投递结果未知，熔断
auth_fail           登录态失效
selector_broken     页面结构变化 / 按钮丢失 / 遮罩残留，脚本已无法操作
"""

import time

# ═══════════════════════════════════════════════════════════
# 原因码定义
# ═══════════════════════════════════════════════════════════

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

# 需要人工介入后才能继续的原因码
MANUAL_CODES = {code for code, meta in STOP_CODES.items() if meta["manual"]}

# 允许自动继续（不算人工阻塞）的原因码
SOFT_CODES = set(STOP_CODES) - MANUAL_CODES


# ═══════════════════════════════════════════════════════════
# 风控字样词表
# ═══════════════════════════════════════════════════════════

# 强风控字样：一旦出现在页面/弹窗文本中，立即停机（不做任何重试）
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

# 弱风控字样：单次可能是瞬时抖动，连续命中才停机
RISK_WEAK_PATTERNS = [
    "请稍后再试",
    "请稍后重试",
    "系统繁忙",
    "服务繁忙",
    "网络繁忙",
]

# 弱风控连续命中阈值
RISK_WEAK_STREAK_LIMIT = 2


# ═══════════════════════════════════════════════════════════
# 检测与落库辅助
# ═══════════════════════════════════════════════════════════

def risk_scan(text):
    """扫描文本中的风控字样（无副作用）

    返回:
        (level, pattern)
        level: 'strong' | 'weak' | None
        pattern: 命中词，未命中为空串
    """
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
    """构造停机记录（可直接 update 进 serial_progress.json）

    参数:
        code: STOP_CODES 中的原因码
        detail: 自然语言细节（命中词、页码、URL 等）
    """
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
    """把原因码渲染成一行人类可读文案"""
    if not code:
        return "无停机原因码"
    meta = STOP_CODES.get(code) or {"label": f"未知原因({code})", "hint": ""}
    text = f"[{code}] {meta['label']}"
    if detail:
        text += f" - {detail}"
    return text


def hint_of(code):
    """原因码的处置建议"""
    meta = STOP_CODES.get(code) or {}
    return meta.get("hint", "")


def is_manual(code):
    """该原因码是否属于人工介入项（禁止自动重试）"""
    return code in MANUAL_CODES

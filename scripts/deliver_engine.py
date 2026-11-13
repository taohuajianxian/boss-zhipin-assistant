#!/usr/bin/env python3
"""
模块 F: 投递执行引擎（推荐页/搜索页内原地投递 + 智能页面反馈监测）

★ v5.0 新增:
  - check_delivery_target() — 流程图"总投递数量是否满足要求"节点
  - DAILY_TARGET 常量 — 每日投递目标（默认150）

核心创新（融合参考skill v6.3+ 和本机 v2 反馈检测）:
  1. 全程停留在当前页面（推荐页/搜索页），不 navigate 到 job_detail URL
  2. 在右侧面板点击「立即沟通」-> 处理原地弹窗 -> 点「留在此页」
  3. 保留 v2 智能反馈检测: FATAL_PATTERNS + _check_fatal_patterns
  4. 致命错误优先检测: 在处理弹窗之前检测上限提示
  5. 连续 3 次未知状态 -> 熔断暂停

★ v4.2 修复:
  1. 点击 .op-btn-chat 前先 scrollIntoView 确保按钮在视口内
  2. 每步操作后立即检测状态
  3. 正确处理原地弹窗
  4. 成功判断基于累计证据
  5. ProgressManager API 修正

独立可运行:
    python deliver_engine.py jobs_to_deliver.json --progress stream_progress.json --max 50

也可被其他模块 import:
    from scripts.deliver_engine import deliver_inplace, check_delivery_target
"""

import json
import time
import random
import re
import sys
import os

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.profile_loader import WORK_DIR, DAILY_TARGET

from scripts.webbridge_client import (
    SESSION, evaluate, navigate, handle_popup, popup_residue,
    ensure_active_tab, health_check,
    ProgressManager, register_signal_guard, SafeInterrupt,
)
from scripts.jd_reader import click_card_inplace

# ═══════════════════════════════════════════════════════════
# ★ v5.0 投递目标常量 — 从 user_profile.json 读取 (profile_loader 导入)
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
# 页面反馈检测常量 (从 v2 迁移)
# ═══════════════════════════════════════════════════════════

# ★ 20260810 拆分：120次沟通上限 ≠ 完全日上限
# 沟通上限仍然可以搜索浏览收集，不应触发 step_finish() 终止一切
COMM_LIMIT_PATTERNS = [
    "您今天已与",           # "您今天已与120/120位BOSS沟通过" — 仅限制新沟通，可继续搜索
]

FATAL_PATTERNS = [
    "今日沟通次数已达上限",
    "今日沟通已达上限",
    "今日已达上限",
    "沟通次数已达上限",
    "今日投递已达上限",      # 完全日上限
    "每天最多沟通",
    "今日沟通额度已用完",
    "达到沟通上限",
    "沟通上限",
    "休息一下，明天再来",     # 完全日上限
    "已达今日沟通上限",
]

LOGIN_PATTERNS = [
    "请先登录",
    "登录已过期",
    "扫码登录",
    "BOSS直聘登录",
    "安全验证",
]

SKIP_PATTERNS = [
    "已经沟通过",
    "已沟通过该职位",
    "职位已关闭",
    "职位不存在",
    "职位已下线",
    "该职位已失效",
]

MAX_CONSECUTIVE_UNKNOWN = 3
# ★ 20261113: 连续 N 条 comm_limit 判定为"页面被遮罩挡住"或"平台真封顶"，停止本批待人工确认
MAX_CONSECUTIVE_COMM = 3


# ═══════════════════════════════════════════════════════════
# ★ v5.0 投递目标检查 — 流程图"总投递数量是否满足要求"节点
# ═══════════════════════════════════════════════════════════

def check_delivery_target(progress_file=None):
    """★ 流程图节点: 总投递数量是否满足要求

    读取投递进度文件，判断成功投递总数是否达到 DAILY_TARGET。

    参数:
        progress_file: 进度文件路径（默认使用工作目录下的 stream_progress.json）

    返回:
        dict: {
            "target_met": bool,       # 是否达标
            "delivered_count": int,   # 已投递成功数
            "target": int,            # 目标数
            "remaining": int,         # 还差多少
        }
    """
    if progress_file is None:
        progress_file = os.path.join(WORK_DIR, "stream_progress.json")

    pm = ProgressManager(progress_file)
    pm.load()

    delivered_count = getattr(pm, 'delivered_count', 0) or len(getattr(pm, 'delivered', []))
    target_met = delivered_count >= DAILY_TARGET
    remaining = max(0, DAILY_TARGET - delivered_count)

    result = {
        "target_met": target_met,
        "delivered_count": delivered_count,
        "target": DAILY_TARGET,
        "remaining": remaining,
    }

    print(f"[目标] 已投递 {delivered_count}/{DAILY_TARGET}" + 
          (f" ★ 已达标！" if target_met else f" (还差 {remaining})"))

    return result


# ═══════════════════════════════════════════════════════════
# 弹窗处理
# ═══════════════════════════════════════════════════════════

def handle_greet_popup():
    """处理打招呼弹窗，点击"发送"按钮完成投递

    BOSS直聘点击"立即沟通"后原地弹出打招呼确认框，
    需要点击"发送"按钮发送招呼语。

    返回:
        'sent:xxx' | 'no_popup'
    """
    popup_result = evaluate("""
    (function(){
        var dialogSelectors = '.dialog-wrap, .greet-pop, .dialog-container, .layer-dialog, .modal, .ant-modal-wrap, .layer-ext';
        var dialogs = document.querySelectorAll(dialogSelectors);
        for (var i = 0; i < dialogs.length; i++) {
            var d = dialogs[i];
            var rect = d.getBoundingClientRect();
            var style = window.getComputedStyle(d);
            if (rect.width > 0 && rect.height > 0 && style.display !== 'none') {
                var btns = d.querySelectorAll('a, button, .btn, .dialog-btn, span.btn');
                for (var j = 0; j < btns.length; j++) {
                    var text = btns[j].textContent.trim();
                    if (text === '发送' || text === '确定' || text === '发送招呼' || text === '好' || text === '发送消息') {
                        btns[j].click();
                        return 'sent:' + text;
                    }
                }
            }
        }

        var allBtns = document.querySelectorAll('a, button, .btn, .dialog-btn, .btn-sure, .layer-btn-confirm');
        for (var k = 0; k < allBtns.length; k++) {
            var el = allBtns[k];
            var text = el.textContent.trim();
            if (text === '发送' || text === '发送招呼' || text === '发送消息') {
                var rect = el.getBoundingClientRect();
                var style = window.getComputedStyle(el);
                if (rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden') {
                    el.click();
                    return 'sent:fallback_' + text;
                }
            }
        }

        var sureBtn = document.querySelector('.btn-sure');
        if (sureBtn) {
            var rect = sureBtn.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                sureBtn.click();
                return 'sent:btn-sure';
            }
        }

        return 'no_popup';
    })()
    """)
    return popup_result if popup_result else 'no_popup'


def click_stay_on_page():
    """点击"留在此页"按钮

    投递成功后 BOSS 原地弹出确认弹窗，含"留在此页"和"继续沟通"两个按钮。
    必须点"留在此页"（留在当前页面），绝不点"继续沟通"（会跳转聊天页）。

    返回:
        'clicked:留在此页' | 'not_found'
    """
    result = evaluate("""
    (function(){
        var btns = document.querySelectorAll('button, a');
        for (var i = 0; i < btns.length; i++) {
            var el = btns[i];
            var text = (el.textContent || '').trim();
            if (text === '留在此页') {
                var rect = el.getBoundingClientRect();
                var style = window.getComputedStyle(el);
                if (rect.width > 0 && rect.height > 0 && style.display !== 'none') {
                    el.click();
                    return 'clicked:留在此页';
                }
            }
        }
        return 'not_found';
    })()
    """)
    return result or 'not_found'


# ═══════════════════════════════════════════════════════════
# 状态检测辅助函数
# ═══════════════════════════════════════════════════════════

def _check_fatal_patterns():
    """检测页面是否有致命错误/登录过期/跳过模式

    返回:
        (status, detail): 匹配到模式时返回对应 status，无匹配返回 (None, '')
    """
    page_text = evaluate("""
    (function(){
        var texts = [];
        texts.push(document.body.innerText || '');
        var dialogs = document.querySelectorAll(
            '.dialog-wrap, .dialog-container, .greet-pop, .layer-dialog, ' +
            '.modal, .toast, .message, .ant-message, .ant-modal, ' +
            '.dialog-body, .dialog-content, .pop-content, .tips-content'
        );
        for (var i = 0; i < dialogs.length; i++) {
            var d = dialogs[i];
            var rect = d.getBoundingClientRect();
            var style = window.getComputedStyle(d);
            if (rect.width > 0 && rect.height > 0 && style.display !== 'none') {
                texts.push(d.innerText || '');
            }
        }
        return texts.join('\\n').substring(0, 3000);
    })()
    """) or ""

    current_url = evaluate("window.location.href") or ""
    if "login" in current_url or "passport" in current_url:
        return 'login_expired', f"页面跳转登录: {current_url[:80]}"

    # ★ 20260911 修复: 两级上限精确区分（用户实测反馈）
    #   120次: "您今天已与120/120位BOSS沟通过" — 仅提示，仍可继续投递
    #   150次: "您今天已与150位BOSS沟通，休息一下，明天再来吧～" — 硬顶，停止一切
    #   旧版 COMM_LIMIT_PATTERNS=["您今天已与"] 同时匹配两级，且先于 FATAL 检测，
    #   导致 150 硬顶被误判为 120 软顶 → 永不触发 fatal_limit。
    # 方案: 优先用数字解析区分，再回退到模式匹配（FATAL 先于 COMM）。
    # 120 格式: "您今天已与120/120位BOSS沟通过"
    m120 = re.search(r'您今天已与\s*(\d+)\s*/\s*(\d+)\s*位', page_text)
    if m120:
        return 'comm_limit', f"触发沟通上限提示({m120.group(1)}/{m120.group(2)}位): 仍可继续投递"

    # 150 格式: "您今天已与150位BOSS沟通，休息一下，明天再来吧～"
    m150 = re.search(r'您今天已与\s*(\d+)\s*位', page_text)
    if m150:
        num = int(m150.group(1))
        if num >= 150:
            return 'fatal_limit', f"触发每日硬顶({num}位): 停止投递"
        return 'comm_limit', f"触发沟通上限提示({num}位): 仍可继续投递"

    # 回退: 模式匹配 — ★ 20260911 调整顺序: 先 FATAL（150硬顶特征更强）再 COMM（120软顶）
    for pattern in FATAL_PATTERNS:
        if pattern in page_text:
            return 'fatal_limit', f"触发每日上限: {pattern}"

    for pattern in COMM_LIMIT_PATTERNS:
        if pattern in page_text:
            return 'comm_limit', f"触发沟通上限(120次): {pattern}"

    for pattern in LOGIN_PATTERNS:
        if pattern in page_text:
            return 'login_expired', f"登录过期: {pattern}"

    for pattern in SKIP_PATTERNS:
        if pattern in page_text:
            return 'skip', f"跳过: {pattern}"

    return None, ''


def _get_chat_button_text():
    """获取当前沟通按钮的文字"""
    return evaluate("""
    (function(){
        var btn = document.querySelector('.op-btn-chat, .btn-startchat, .job-chat-btn');
        return btn ? btn.textContent.trim() : '';
    })()
    """) or ""


# ═══════════════════════════════════════════════════════════
# 核心投递逻辑
# ═══════════════════════════════════════════════════════════

def deliver_inplace(job_id, job_info=None):
    """当前页面内原地投递单个岗位

    流程:
      1. 点击卡片（右侧面板就地切 JD）
      2. scrollIntoView + 点击「立即沟通」(.op-btn-chat)
      3. 等2s -> 立即检测致命错误
      4. 处理打招呼弹窗（点击"发送"）
      5. 等1s -> 点击「留在此页」
      6. 等1s -> 检测最终状态
      7. 根据累计证据返回 status

    参数:
        job_id: 加密岗位 ID
        job_info: 可选的岗位信息 dict

    返回:
        (status, detail)
        status: 'success' | 'fatal_limit' | 'login_expired' | 'skip' | 'fail' | 'unknown'
    """
    title = (job_info or {}).get('title', '')[:25] if job_info else job_id

    # ★ 20260911: 投递前确保仍在本 session 独立标签页（用户切标签也不会误投到用户页面）
    if not ensure_active_tab():
        return 'fail', '无法确认独立标签页，停止投递'

    # ★ 20261113 preflight 清障：BOSS 上限提示会叠加 2~6 层遮罩，遮罩挡住卡片点击时，
    # 页面表现为"点了没反应"，会被误判成 comm_limit/fail（假失败主因）。
    # 零延时探测：干净则直接跳过，只有真检测到遮罩才付出关闭成本。
    _residue = popup_residue()
    if _residue > 0:
        _cleared = handle_popup()
        _left = popup_residue()
        if _left > 0:
            time.sleep(1.2)
            _left = popup_residue()
        if _left > 0:
            return 'fail', f'遮罩残留 {_left} 个未清除({_cleared})，跳过防假失败'

    # 1. 点击卡片
    click_result = click_card_inplace(job_id, verify=True, max_wait=5)
    if not click_result["ok"]:
        return 'fail', f"卡片定位失败: {click_result['detail']}"

    time.sleep(1)

    # 2. scrollIntoView + 点击
    scroll_result = evaluate("""
    (function(){
        var btn = document.querySelector('.op-btn-chat');
        if (!btn) btn = document.querySelector('.btn-startchat, .job-chat-btn');
        if (!btn) return 'not_found';
        var text = btn.textContent.trim();
        if (text.indexOf('继续沟通') >= 0) return 'already:' + text;
        btn.scrollIntoView({block: 'center'});
        return 'scrolled:' + text;
    })()
    """)

    if scroll_result.startswith('already:'):
        return 'skip', f'已沟通过: {scroll_result}'

    if scroll_result == 'not_found':
        body_text = evaluate("document.body.innerText") or ""
        if any(p in body_text for p in ['职位已关闭', '职位不存在', '已下线']):
            return 'skip', '职位已关闭/下线'
        return 'fail', '沟通按钮未找到'

    time.sleep(0.5)

    click_result = evaluate("""
    (function(){
        var btn = document.querySelector('.op-btn-chat');
        if (!btn) btn = document.querySelector('.btn-startchat, .job-chat-btn');
        if (!btn) return 'not_found';
        btn.click();
        return 'clicked';
    })()
    """)

    if click_result == 'not_found':
        return 'fail', '点击时按钮消失'

    # 3. 等待弹窗 → 检测致命错误
    time.sleep(2)
    fatal_status, fatal_detail = _check_fatal_patterns()
    if fatal_status:
        return fatal_status, fatal_detail

    # 4. 处理打招呼弹窗
    popup = handle_greet_popup()
    sent_greeting = (popup != 'no_popup')

    if sent_greeting:
        time.sleep(1.5)

    # 5. 点击「留在此页」
    stay_result = click_stay_on_page()
    stayed_on_page = (stay_result == 'clicked:留在此页')

    if stayed_on_page:
        time.sleep(1)
    elif not sent_greeting:
        handle_popup()
        time.sleep(1)

    # 6. 检测最终状态
    fatal_status, fatal_detail = _check_fatal_patterns()
    if fatal_status:
        return fatal_status, fatal_detail

    btn_text = _get_chat_button_text()

    # 7. 综合判断
    if '继续沟通' in btn_text:
        return 'success', '投递成功(按钮已变为继续沟通)'

    if sent_greeting and stayed_on_page:
        return 'success', '投递成功(发送招呼+留在此页)'

    if sent_greeting:
        return 'success', '投递成功(已发送招呼)'

    # v5.1 修复: "留在此页"被点击本身就是投递成功的强证据
    # "留在此页"按钮只在招呼语发送成功后的确认弹窗中出现
    if stayed_on_page:
        return 'success', '投递成功(留在此页已点击,招呼弹窗可能自动发送)'

    return 'unknown', f'未确认成功: popup={popup}, stay={stay_result}, btn={btn_text[:20]}'


# ═══════════════════════════════════════════════════════════
# 批量投递（兼容旧接口）
# ═══════════════════════════════════════════════════════════

def run_batch(jobs_file, progress_file="stream_progress.json", max_count=0):
    """批量投递（兼容旧接口，v5.0推荐使用serial_loop.py串行控制）"""
    register_signal_guard()

    if not health_check():
        print("WebBridge 不可达，请先运行 env_check.py")
        return None

    if not ensure_active_tab():
        print("无法建立标签页")
        return None

    with open(jobs_file, 'r', encoding='utf-8') as f:
        jobs = json.load(f)

    unapproved = [j for j in jobs if j.get('decision') != 'APPROVE']
    if unapproved:
        print(f"AI 审核守卫: 发现 {len(unapproved)} 条未通过 AI 审核的岗位，拒绝投递")
        return None

    pm = ProgressManager(progress_file)
    pm.load()

    to_deliver = [j for j in jobs if not pm.is_done(j.get('jobId', ''))]
    max_to_deliver = max_count if max_count > 0 else len(to_deliver)
    actual_to_deliver = min(len(to_deliver), max_to_deliver)

    print(f"SESSION: {SESSION}")
    print(f"本批 {len(jobs)} 条，已投 {pm.delivered_count} 条，待投 {len(to_deliver)} 条，本批上限 {max_to_deliver}")
    print("=" * 60)

    stats = {
        "success": 0, "skip": 0, "fail": 0, "fatal_limit": 0,
        "comm_limit": 0, "login_expired": 0, "unknown": 0, "total": 0,
    }
    consecutive_unknown = 0
    consecutive_comm = 0

    for idx, job in enumerate(to_deliver[:actual_to_deliver]):
        job_id = job.get("jobId", "")
        job_title = job.get("title", "?")[:25]

        if not job_id:
            continue

        print(f"[{idx+1}/{actual_to_deliver}] 投递 {job_title} ({job_id}) ...", end=" ")

        status, detail = deliver_inplace(job_id, job_info=job)

        # ★ 20261113: comm_limit 先清障再复投一次。实测多数 comm_limit 是遮罩挡住点击的假失败，
        # 关掉叠加弹窗后重投通常直接成功；只有清障后仍 comm_limit 才算真上限。
        if status == 'comm_limit':
            _cleared = handle_popup()
            _left = popup_residue()
            if _left == 0:
                print(f"\n  [comm_limit] 已清障({_cleared})，复投一次 ...", end=" ")
                time.sleep(1)
                retry_status, retry_detail = deliver_inplace(job_id, job_info=job)
                if retry_status == 'comm_limit':
                    detail = f"{detail} [清障后复投仍 comm_limit]"
                    print("仍 comm_limit")
                else:
                    status, detail = retry_status, f"{retry_detail} [清障后复投]"
                    print(f"{status.upper()}")
            else:
                print(f"\n  [comm_limit] 清障未净，残留 {_left} 个遮罩，按原结果记失败")

        if status == 'success':
            pm.add_delivered(job_id, job)
        else:
            pm.add_failed(job_id, job, detail)
        pm.save()

        stats["total"] += 1
        stats[status] = stats.get(status, 0) + 1

        print(f"{status.upper()}: {detail[:60]}")

        if status == 'unknown':
            consecutive_unknown += 1
            if consecutive_unknown >= MAX_CONSECUTIVE_UNKNOWN:
                print(f"\n连续 {MAX_CONSECUTIVE_UNKNOWN} 次未知状态，熔断暂停")
                break
        else:
            consecutive_unknown = 0

        if status in ('fatal_limit', 'login_expired'):
            print(f"\n致命错误，整批停止")
            break

        # ★ 20261113: 120 次提示是"仍可继续"，150 次才是硬顶（硬顶已由 fatal_limit 在上方拦截）。
        # 单次 comm_limit 不阻断；但连续多条说明遮罩清不干净或平台真封顶，继续投只会刷假失败。
        if status == 'comm_limit':
            consecutive_comm += 1
            if consecutive_comm >= MAX_CONSECUTIVE_COMM:
                print(f"\n连续 {MAX_CONSECUTIVE_COMM} 条 comm_limit（关弹窗+复投均无效）— "
                      f"判定页面被遮挡或已达平台上限，停止本批；请人工确认页面状态后再续投")
                break
            print(f"沟通上限提示 — 清障复投仍失败(连续 {consecutive_comm} 次)，继续下一条")
        else:
            consecutive_comm = 0

        # ★ v5.0 投递目标检查
        target_check = check_delivery_target(progress_file)
        if target_check["target_met"]:
            print(f"\n★ 投递目标 {DAILY_TARGET} 份已达成！停止投递")
            break

        time.sleep(random.uniform(2, 3.5))

    print("\n" + "=" * 60)
    print(f"投递完成: 成功 {stats['success']} / 跳过 {stats['skip']} / 失败 {stats['fail']}")
    print(f"         上限 {stats['fatal_limit']} / 登录过期 {stats['login_expired']} / 未知 {stats['unknown']}")
    print("=" * 60)

    stats["delivered_total"] = pm.delivered_count
    return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="投递执行引擎")
    parser.add_argument("jobs_file", help="待投递岗位 JSON 文件")
    parser.add_argument("--progress", default="stream_progress.json", help="进度文件")
    parser.add_argument("--max", type=int, default=0, help="本批最多投递数量")
    args = parser.parse_args()

    run_batch(args.jobs_file, args.progress, args.max)

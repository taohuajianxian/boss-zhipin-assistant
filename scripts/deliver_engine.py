#!/usr/bin/env python3




























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

from scripts.stop_codes import (
    RISK_STRONG_PATTERNS,
    RISK_WEAK_PATTERNS,
    RISK_WEAK_STREAK_LIMIT,
    risk_scan,
)












COMM_LIMIT_PATTERNS = [
    "您今天已与",
]

FATAL_PATTERNS = [
    "今日沟通次数已达上限",
    "今日沟通已达上限",
    "今日已达上限",
    "沟通次数已达上限",
    "今日投递已达上限",
    "每天最多沟通",
    "今日沟通额度已用完",
    "达到沟通上限",
    "沟通上限",
    "休息一下，明天再来",
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

MAX_CONSECUTIVE_COMM = 3

MAX_CONSECUTIVE_SELECTOR = 3







_RISK_WEAK_STREAK = 0


def check_risk_control(page_text):








    global _RISK_WEAK_STREAK
    level, pattern = risk_scan(page_text)
    if level == 'strong':
        _RISK_WEAK_STREAK = 0
        return True, 'strong', f"风控字样命中: {pattern}"
    if level == 'weak':
        _RISK_WEAK_STREAK += 1
        if _RISK_WEAK_STREAK >= RISK_WEAK_STREAK_LIMIT:
            streak = _RISK_WEAK_STREAK
            _RISK_WEAK_STREAK = 0
            return True, 'weak', f"风控弱字样连续{streak}次命中: {pattern}"
        return False, 'weak', f"风控弱字样命中1次: {pattern}(未定级停机)"
    _RISK_WEAK_STREAK = 0
    return False, '', ''






def check_delivery_target(progress_file=None):















    if progress_file is None:
        progress_file = os.path.join(WORK_DIR, "stream_progress.json")


    delivered_count = None
    try:
        from scripts import stats_core
        _st = stats_core.compute()
        delivered_count = _st["cumulative"]["success"]
    except Exception:
        delivered_count = None
    if delivered_count is None:
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









_GREET_PROOF_KEYWORDS = (
    '已向BOSS发送消息',
    '已向Boss发送消息',
    '已向boss发送消息',
    '如需修改打招呼内容',
)

_GREET_WEAK_KEYWORDS = ('自我介绍一下', '打招呼内容', '发送招呼')


def _text_snippet(text, keyword, span=120):





    if not text or not keyword:
        return ''
    idx = text.find(keyword)
    if idx < 0:
        return ''
    start = max(0, idx - 20)
    end = min(len(text), idx + len(keyword) + span)
    return text[start:end].replace('\n', ' ').strip()


def grab_greet_evidence():










    text = evaluate("""
    (function(){
        var texts = [];
        var sels = '.dialog-wrap, .greet-pop, .dialog-container, .layer-dialog, ' +
                   '.ant-modal-wrap, .dialog-body, .dialog-content, .pop-content, ' +
                   '.tips-content, .toast, .message, .ant-message, .greet-msg';
        var nodes = document.querySelectorAll(sels);
        for (var i = 0; i < nodes.length; i++) {
            var d = nodes[i];
            var rect = d.getBoundingClientRect();
            var style = window.getComputedStyle(d);
            if (rect.width > 0 && rect.height > 0 && style.display !== 'none'
                && style.visibility !== 'hidden') {
                texts.push(d.innerText || '');
            }
        }
        texts.push(document.body ? (document.body.innerText || '') : '');
        return texts.join('\\n').substring(0, 4000);
    })()
    """) or ""

    flat = text.replace('\r', ' ').replace('\n', ' ')

    for kw in _GREET_PROOF_KEYWORDS:
        if kw in flat:
            return {'matched': True, 'kind': kw, 'snippet': _text_snippet(flat, kw)}

    for kw in _GREET_WEAK_KEYWORDS:
        if kw in flat:
            return {'matched': False, 'kind': f'weak:{kw}', 'snippet': _text_snippet(flat, kw)}

    return {'matched': False, 'kind': '', 'snippet': ''}


def handle_greet_popup():








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






def _check_fatal_patterns():





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
    _url_low = current_url.lower()

    if any(k in _url_low for k in ('captcha', 'verify', 'safe-check', 'security-check')):
        return 'risk_control', f"风控拦截(URL跳转验证页): {current_url[:80]}"
    if "login" in current_url or "passport" in current_url:
        return 'login_expired', f"页面跳转登录: {current_url[:80]}"


    risk_hit, risk_level, risk_detail = check_risk_control(page_text)
    if risk_hit:
        return 'risk_control', f"{risk_detail}[{risk_level}]"








    m120 = re.search(r'您今天已与\s*(\d+)\s*/\s*(\d+)\s*位', page_text)
    if m120:
        return 'comm_limit', f"触发沟通上限提示({m120.group(1)}/{m120.group(2)}位): 仍可继续投递"


    m150 = re.search(r'您今天已与\s*(\d+)\s*位', page_text)
    if m150:
        num = int(m150.group(1))
        if num >= 150:
            return 'fatal_limit', f"触发每日硬顶({num}位): 停止投递"
        return 'comm_limit', f"触发沟通上限提示({num}位): 仍可继续投递"


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

    return evaluate("""
    (function(){
        var btn = document.querySelector('.op-btn-chat, .btn-startchat, .job-chat-btn');
        return btn ? btn.textContent.trim() : '';
    })()
    """) or ""






def deliver_inplace(job_id, job_info=None):
























    title = (job_info or {}).get('title', '')[:25] if job_info else job_id


    if not ensure_active_tab():
        return 'fail', '无法确认独立标签页，停止投递'




    _residue = popup_residue()
    if _residue > 0:
        _cleared = handle_popup()
        _left = popup_residue()
        if _left > 0:
            time.sleep(1.2)
            _left = popup_residue()
        if _left > 0:
            return 'fail', f'遮罩残留 {_left} 个未清除({_cleared})，跳过防假失败'


    click_result = click_card_inplace(job_id, verify=True, max_wait=5)
    if not click_result["ok"]:
        return 'selector_broken', f"卡片定位失败: {click_result['detail']}"

    time.sleep(1)


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

        risk_hit, risk_level, risk_detail = check_risk_control(body_text)
        if risk_hit:
            return 'risk_control', f"{risk_detail}[{risk_level}]"
        if any(p in body_text for p in ['职位已关闭', '职位不存在', '已下线']):
            return 'skip', '职位已关闭/下线'
        return 'selector_broken', '沟通按钮未找到'

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
        return 'selector_broken', '点击时按钮消失'


    time.sleep(2)
    fatal_status, fatal_detail = _check_fatal_patterns()
    if fatal_status:
        return fatal_status, fatal_detail


    pre_ev = grab_greet_evidence()
    popup = handle_greet_popup()
    sent_greeting = (popup != 'no_popup')


    post_ev = grab_greet_evidence()
    if sent_greeting:
        time.sleep(1.5)
        if not post_ev.get('matched'):
            post_ev2 = grab_greet_evidence()
            if post_ev2.get('matched') or not post_ev.get('kind'):
                post_ev = post_ev2


    greet_ev = {'matched': False, 'kind': '', 'snippet': ''}
    for cand in (post_ev, pre_ev):
        if cand and cand.get('matched'):
            greet_ev = cand
            break
    else:
        if post_ev and post_ev.get('kind'):
            greet_ev = post_ev

    greet_verified = bool(greet_ev.get('matched'))
    greet_tag = f"证据:{greet_ev['kind']}" if greet_verified else (
        f"弱证据:{greet_ev['kind']}" if greet_ev.get('kind') else '无弹窗证据')
    if greet_ev.get('snippet'):
        greet_tag += f"|{greet_ev['snippet'][:80]}"



    stay_result = click_stay_on_page()
    stayed_on_page = (stay_result == 'clicked:留在此页')

    if stayed_on_page:
        time.sleep(1)
    elif not sent_greeting:
        handle_popup()
        time.sleep(1)


    fatal_status, fatal_detail = _check_fatal_patterns()
    if fatal_status:
        return fatal_status, fatal_detail

    btn_text = _get_chat_button_text()




    if greet_verified:
        return 'success', f'投递成功(已发送招呼|{greet_tag})'

    if '继续沟通' in btn_text:
        return 'success', f'投递成功(按钮已变为继续沟通|{greet_tag})'

    if sent_greeting and stayed_on_page:
        return 'success', f'投递成功(发送招呼+留在此页|{greet_tag})'

    if sent_greeting:
        return 'success', f'投递成功(已点击发送招呼|{greet_tag})'



    if stayed_on_page:
        return 'success', f'投递成功(留在此页已点击,招呼弹窗可能自动发送|{greet_tag})'

    return 'unknown', f'未确认成功: popup={popup}, stay={stay_result}, btn={btn_text[:20]}, greet={greet_tag}'






def run_batch(jobs_file, progress_file="stream_progress.json", max_count=0):

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



        if status == 'comm_limit':
            consecutive_comm += 1
            if consecutive_comm >= MAX_CONSECUTIVE_COMM:
                print(f"\n连续 {MAX_CONSECUTIVE_COMM} 条 comm_limit（关弹窗+复投均无效）— "
                      f"判定页面被遮挡或已达平台上限，停止本批；请人工确认页面状态后再续投")
                break
            print(f"沟通上限提示 — 清障复投仍失败(连续 {consecutive_comm} 次)，继续下一条")
        else:
            consecutive_comm = 0


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
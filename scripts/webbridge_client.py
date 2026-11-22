#!/usr/bin/env python3





















import json
try:
    from scripts.atomic_io import atomic_dump_json
except ImportError:
    from atomic_io import atomic_dump_json

import http.client
import time
import re
import random
import os
import signal
import difflib
import glob
import sys
from datetime import datetime





SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from scripts.profile_loader import SESSION_NAME, USER_NAME





DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10086



SESSION = SESSION_NAME

DEFAULT_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_DELAY = 3





def decrypt_salary(text):

    if not text:
        return ""
    result = []
    for ch in text:
        cp = ord(ch)
        if 0xE031 <= cp <= 0xE03A:
            result.append(str(cp - 0xE031))
        else:
            result.append(ch)
    return ''.join(result)


def parse_salary_max(s):




    if not s:
        return 0
    s = re.sub(r'·\d+薪', '', s)
    m = re.search(r'(\d+)-(\d+)K', s)
    if m:
        return int(m.group(2))
    m = re.search(r'(\d+)-(\d+)元/月', s)
    if m:
        return int(m.group(2)) / 1000
    return 0


def parse_salary_min(s):






    if not s:
        return 0
    s = re.sub(r'·\d+薪', '', s)
    m = re.search(r'(\d+)-(\d+)K', s)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)-(\d+)元/月', s)
    if m:
        return int(m.group(1)) / 1000
    m = re.search(r'(\d+)-(\d+)万/月', s)
    if m:
        return int(m.group(1)) * 10
    m = re.search(r'(\d+)\s*K', s)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*元/月', s)
    if m:
        return int(m.group(1)) / 1000
    return 0






def api(action, args=None, session=SESSION, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):

    if args is None:
        args = {}
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            body = json.dumps({"action": action, "args": args, "session": session}).encode("utf-8")
            conn.request("POST", "/command", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            conn.close()
            return data
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    raise last_error


def evaluate(code, session=SESSION):

    r = api("evaluate", {"code": code}, session=session)
    return r.get("data", {}).get("value", "")


def navigate(url, wait_range=(4, 6), session=SESSION, new_tab=False, group_title=None):





    args = {"url": url}
    if new_tab:
        args["newTab"] = True
    if group_title:
        args["group_title"] = group_title
    r = api("navigate", args, session=session)
    time.sleep(random.uniform(*wait_range))
    return r


def click(selector, session=SESSION):

    return api("click", {"selector": selector}, session=session).get("data", {}).get("success", False)


def scroll_page(session=SESSION):

    evaluate("(function(){window.scrollTo(0, document.body.scrollHeight);return document.body.scrollHeight;})()", session=session)






_EXTRACT_JOBS_JS = r"""
(function(){
    var cards = document.querySelectorAll('.job-card-wrap');
    var jobs = [];
    var seen = new Set();
    for (var i = 0; i < cards.length; i++) {
        try {
            var d = cards[i].__vue__.$props.data;
            if (!d || !d.encryptJobId) continue;
            var jid = d.encryptJobId;
            if (seen.has(jid)) continue;
            seen.add(jid);
            jobs.push({
                title: d.jobName || "",
                salary: d.salaryDesc || "",
                city: d.cityName || "",
                company: d.brandName || "",
                industry: d.brandIndustry || "",
                scale: d.brandScaleName || "",
                stage: d.brandStageName || "",
                online: !!d.bossOnline,
                welfare: (d.welfareList || []).join("|"),
                degree: d.jobDegree || "",
                experience: d.jobExperience || "",
                jobId: jid,
            });
        } catch(e) {}
    }
    return JSON.stringify({total: jobs.length, list: jobs});
})()
"""


def extract_jobs(session=SESSION):

    raw = evaluate(_EXTRACT_JOBS_JS, session=session)
    try:
        data = json.loads(raw) if raw else {}
        jobs = data.get("list", [])
        for j in jobs:
            j['salary'] = decrypt_salary(j.get('salary', ''))
        return jobs
    except (json.JSONDecodeError, TypeError):
        return []


_POPUP_SELECTORS = [

    '.dialog-wrap.greet-pop .btn-sure',
    '.greet-pop .btn',
    '.dialog-container .btn',
    '.dialog-container .btn-sure',
    '.dialog-wrap .btn-sure',
    '.tips-pop .btn',
    '.boss-tip .btn',
    '.btn-sure',
    '.dialog-confirm', '.layer-btn-confirm', '.btn-confirm',
    '.dialog-ok', '.modal-confirm', '.layui-layer-btn0',

    '.dialog-wrap', '.dialog-container', '.greet-pop',
    '.tips-pop', '.boss-tip', '.boss-popup',
    '.modal-wrap', '.layer-layer',
]


_POPUP_TEXTS = ['好', '确定', '我知道了', '知道了', '继续沟通', '关闭', '明白了', '下次再说']
_POPUP_MAX_ROUNDS = 4


_RESIDUE_JS = """
(function(){
    var sels = %s;
    var n = 0;
    for (var i = 0; i < sels.length; i++) {
        var nodes = document.querySelectorAll(sels[i]);
        for (var j = 0; j < nodes.length; j++) {
            var r = nodes[j].getBoundingClientRect();
            var s = window.getComputedStyle(nodes[j]);
            if (r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden') n++;
        }
    }
    return String(n);
})()
""" % json.dumps(_POPUP_SELECTORS)


def popup_residue(session=SESSION):





    try:
        raw = evaluate(_RESIDUE_JS, session=session)
    except Exception:
        return 0
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def handle_popup(session=SESSION, max_rounds=_POPUP_MAX_ROUNDS, skip_if_clean=False):








    if skip_if_clean and popup_residue(session=session) == 0:
        return 'no_popup'

    time.sleep(1.5)

    js = """
    (function(){
        var selectors = %s;
        var texts = %s;
        var hits = [];
        var guard = 0;

        function visible(el) {
            if (!el) return false;
            var r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return false;
            var s = window.getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        }
        function isBtn(el) {
            if (!el) return false;
            var tag = el.tagName;
            if (tag === 'BUTTON' || tag === 'A') return true;
            return /(^|\\s)(btn|sure|confirm|ok)[\\w-]*/i.test(el.className || '');
        }
        function buttonIn(root) {
            if (isBtn(root)) return root;
            var kids = root.querySelectorAll('button, a, .btn, .btn-sure, [class*="btn"], [class*="sure"], [class*="confirm"]');
            for (var i = 0; i < kids.length; i++) {
                if (visible(kids[i])) return kids[i];
            }
            return null;
        }
        function fire(el) {
            var r = el.getBoundingClientRect();
            var x = r.left + r.width / 2;
            var y = r.top + r.height / 2;
            ['mousedown', 'mouseup', 'click'].forEach(function (type) {
                el.dispatchEvent(new MouseEvent(type, {
                    bubbles: true, cancelable: true, view: window, clientX: x, clientY: y
                }));
            });
        }

        while (guard++ < 8) {
            var target = null;
            var hit = '';

            for (var p = 0; p < selectors.length && !target; p++) {
                var nodes = document.querySelectorAll(selectors[p]);
                for (var n = 0; n < nodes.length; n++) {
                    if (!visible(nodes[n])) continue;
                    var cand = buttonIn(nodes[n]);
                    if (cand) { target = cand; hit = selectors[p]; break; }
                }
            }

            if (!target) {
                // 文本兜底第一趟：只认叶子节点，避免点在容器上（容器 textContent 也可能是"好"）
                var pool = document.querySelectorAll('button, a, span, i, div');
                for (var k = 0; k < pool.length; k++) {
                    var leaf = pool[k];
                    if (leaf.children.length > 0) continue;
                    var text = (leaf.textContent || '').trim();
                    if (!text || text.length > 8) continue;
                    if (texts.indexOf(text) >= 0 && visible(leaf)) {
                        target = leaf; hit = 'text:' + text; break;
                    }
                }
                // 第二趟：整块文案命中时，下沉取容器内部按钮
                if (!target) {
                    for (var m = 0; m < pool.length; m++) {
                        var box = pool[m];
                        if (box.children.length === 0) continue;
                        var boxText = (box.textContent || '').trim();
                        if (!boxText || boxText.length > 8) continue;
                        if (texts.indexOf(boxText) < 0 || !visible(box)) continue;
                        var inner = buttonIn(box);
                        if (inner) { target = inner; hit = 'text-in:' + boxText; break; }
                    }
                }
            }

            if (!target) break;
            try { fire(target); } catch (e) { break; }
            hits.push(hit);
        }

        return JSON.stringify({clicked: hits.length, hits: hits});
    })()
    """ % (json.dumps(_POPUP_SELECTORS), json.dumps(_POPUP_TEXTS, ensure_ascii=False))

    total, hits, rounds = 0, [], 0
    for _ in range(max_rounds):
        raw = evaluate(js, session=session)
        payload = {}
        if raw:
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                payload = {}
        clicked = 0
        try:
            clicked = int(payload.get("clicked") or 0)
        except (TypeError, ValueError, AttributeError):
            clicked = 0
        if not clicked:
            break
        total += clicked
        hits.extend(payload.get("hits") or [])
        rounds += 1
        time.sleep(0.6)

    if not total:
        return 'no_popup'


    residue = popup_residue(session=session)

    summary = 'clicked: %d (%d轮) [%s]' % (total, rounds, '|'.join(hits))
    if residue > 0:
        summary += ' + residue:%d' % residue
    return summary






class ProgressManager:











    def __init__(self, filepath="stream_progress.json"):
        self.filepath = filepath
        self.data = {"delivered": [], "failed": []}

    def load(self):
        if os.path.exists(self.filepath):
            with open(self.filepath, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        return self

    def save(self):
        atomic_dump_json(self.filepath, self.data, indent=2)

    @property
    def delivered_ids(self):
        return set(j['jobId'] for j in self.data.get('delivered', []))

    @property
    def failed_ids(self):
        return set(j['jobId'] for j in self.data.get('failed', []))

    @property
    def all_done_ids(self):
        return self.delivered_ids | self.failed_ids

    def is_done(self, job_id):
        return job_id in self.all_done_ids

    def add_delivered(self, job_id, job):
        self.data['delivered'].append({
            'jobId': job_id,
            'title': job.get('title', ''),
            'company': job.get('company', ''),
            'salary': job.get('salary', ''),
            'industry': job.get('industry', ''),
            'result': 'success',
        })

    def add_failed(self, job_id, job, reason):
        self.data['failed'].append({
            'jobId': job_id,
            'title': job.get('title', ''),
            'company': job.get('company', ''),
            'result': 'fail',
            'reason': reason,
        })

    @property
    def delivered_count(self):
        return len(self.data.get('delivered', []))

    @property
    def failed_count(self):
        return len(self.data.get('failed', []))






def health_check(host=DEFAULT_HOST, port=DEFAULT_PORT):

    try:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/")
        status = conn.getresponse().status
        conn.close()
        return status == 200 or status == 404
    except Exception:
        return False


def ensure_active_tab(session=SESSION, timeout_wait=10):






    try:
        if not health_check():
            return False


        r = api("list_tabs", {}, session=session, timeout=5)
        if r.get("ok"):
            tabs_data = r.get("data", {})
            tabs = tabs_data.get("tabs", []) if isinstance(tabs_data, dict) else tabs_data
            if tabs:

                for t in tabs:
                    url = (t.get("url") or "") if isinstance(t, dict) else ""
                    if "zhipin.com" in url:

                        fr = api("find_tab", {"url": url}, session=session, timeout=5)
                        if fr.get("ok"):
                            if str(evaluate("1+1", session=session)) == "2":
                                return True



        nav_r = navigate("https://www.zhipin.com/web/geek/job",
                         wait_range=(timeout_wait, timeout_wait + 2),
                         session=session, new_tab=True, group_title="BOSS直聘投递")
        if not nav_r.get("ok"):
            return False
        return str(evaluate("1+1", session=session)) == "2"
    except Exception:
        return False


def clean_pid_files():

    pid_patterns = [
        os.path.expanduser("~/.kimi-webbridge/daemon.pid"),
        os.path.expanduser("~/.kimi-webbridge*.pid"),

    ]
    cleaned = []
    for pattern in pid_patterns:
        for f in glob.glob(pattern):
            if os.path.exists(f):
                try:
                    os.remove(f)
                    cleaned.append(f)
                except Exception:
                    pass
    return cleaned






class SafeInterrupt(BaseException):
    pass


def _signal_handler(signum, frame):
    raise SafeInterrupt(f"Received signal {signum}")


def register_signal_guard():
    signal.signal(signal.SIGTERM, _signal_handler)






class WebBridge:








    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, session=SESSION, timeout=DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.session = session
        self.timeout = timeout

    def api(self, action, args=None, timeout=None):
        return api(action, args, self.session, self.host, self.port, timeout or self.timeout)

    def evaluate(self, code):
        return evaluate(code, self.session)

    def navigate(self, url, wait_range=(4, 6)):
        return navigate(url, wait_range, self.session)

    def click(self, selector):
        return click(selector, self.session)

    def scroll_page(self):
        return scroll_page(self.session)

    def snapshot(self):
        return self.api("snapshot", {})

    def screenshot(self, path):
        return self.api("screenshot", {"path": path})

    def list_tabs(self):
        r = self.api("list_tabs", {})
        data = r.get("data", r)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("tabs", data.get("data", []))
        return []

    def close_tab(self, tab_id):
        return self.api("close_tab", {"tabId": tab_id})

    def extract_jobs(self):
        return extract_jobs(self.session)

    def handle_popup(self):
        return handle_popup(self.session)

    def ensure_active_tab(self, timeout_wait=10):
        return ensure_active_tab(self.session, timeout_wait)

    def verify_login(self, username=None):
        if username is None:
            username = USER_NAME
        body = self.evaluate("document.body.innerText") or "" 
        if username in body:
            return True
        time.sleep(5)
        body = self.evaluate("document.body.innerText") or ""
        return username in body

    def body_with_retry(self):
        body = self.evaluate("document.body.innerText") or ""
        if len(body) < 200:
            time.sleep(5)
            body = self.evaluate("document.body.innerText") or ""
        return body

    def find_chat_button_ref(self):
        snap = self.snapshot()
        tree_str = json.dumps(snap, ensure_ascii=False)
        for p in [
            r"'name':\s*'立即沟通'.*?'ref':\s*'(@e\d+)'",
            r"'ref':\s*'(@e\d+)'.*?'name':\s*'立即沟通'",
            r'"name":\s*"立即沟通".*?"ref":\s*"(@e\d+)"',
            r'"ref":\s*"(@e\d+)".*?"name":\s*"立即沟通"',
        ]:
            m = re.search(p, tree_str)
            if m:
                return m.group(1)
        return None






__version__ = "3.0.0"
__updated__ = "2026-07-26"
__changes__ = """
v3.0.0 (2026-07-26):
  - 全局唯一 SESSION="boss-main"，禁止各脚本自定义 session 名
  - 新增 extract_jobs(): Vue $props.data 统一提取
  - 新增 handle_popup(): 弹窗处理（含第120份投递"好"弹窗）
  - 新增 scroll_page(): 滚动加载
  - 新增 parse_salary_max(): 薪资解析
  - 新增 ProgressManager: 断点续传进度管理类
  - 新增 clean_pid_files(): PID 残留清理
  - api() 自带 3 次重试
  - 所有函数默认使用全局 SESSION
"""
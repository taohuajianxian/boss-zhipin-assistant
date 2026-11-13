#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端指令包客户端（零第三方依赖，只用 Python 标准库）。

职责边界：
  - 只负责「取知识」：指令包缓存、选择器 / JS / 时序参数读取、云端同步。
  - 不负责「判断能不能用」：授权判定在 entitlement.py。
  - 不上传任何用户画像、简历、JD 正文、登录态；请求里只有 激活码 + 匿名设备指纹。

指令包来源优先级：
  1. ~/.boss_zhipin_deliver/plan.json   —— 激活后从服务端同步下来的新版
  2. <SKILL_DIR>/dist/plan.json          —— 随包附带的免费体验版（能跑通，但不更新）

随包版本功能完整；激活后会持续同步新版指令包，页面结构变化时无需手动换包。

独立可运行:
    python scripts/cloud_client.py --status     查看当前指令包来源与版本
    python scripts/cloud_client.py --sync       用已激活的码重新拉一次指令包
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import ssl
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

SKILL_DIR = Path(__file__).resolve().parent.parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

STATE_DIR = Path(os.environ.get("BOSS_LICENSE_DIR", str(Path.home() / ".boss_zhipin_deliver")))
LICENSE_FILE = STATE_DIR / "license.json"
PLAN_CACHE = STATE_DIR / "plan.json"
SYNC_FILE = STATE_DIR / "sync.json"
USAGE_FILE = STATE_DIR / "usage.json"
CONFIG_FILE = SKILL_DIR / "scripts" / "cloud_config.json"
BUNDLED_PLAN = SKILL_DIR / "dist" / "plan.json"

GRACE_HOURS = 24
TIMEOUT = 8
SKILL_VERSION = "20261113"   # 与 skill.md 的 version 保持一致，仅用于 User-Agent

# 禁用环境代理：服务地址可能是本机 / 内网，走代理会连不上
def _ssl_context():
    """HTTPS 校验用的 CA：优先 certifi，缺失时回退系统默认。

    背景：部分 Windows 机器 OpenSSL 默认 CA 路径（Program Files\\Common Files\\SSL\\cert.pem）
    不存在或系统证书库含过期根，会让 urllib 报 certificate has expired，而浏览器 / curl 正常。
    统一走 certifi 可跨平台消除这类误判。
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_OPENER = build_opener(ProxyHandler({}), HTTPSHandler(context=_ssl_context()))


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data):
    ensure_state_dir()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def config() -> dict:
    """模板 + 本地覆盖。如需自定义服务地址 / 微信号，写进同目录 cloud_config.local.json，
    便于保留个人设置。"""
    base = _read_json(CONFIG_FILE, {}) or {}
    local_path = CONFIG_FILE.with_name("cloud_config.local.json")
    local = _read_json(local_path, {}) or {}
    override = {k: v for k, v in local.items() if not k.startswith("_")}
    return {**base, **override}


def endpoint() -> str:
    """服务地址：环境变量优先，其次 scripts/cloud_config.json。留空 = 纯离线免费体验。"""
    ep = os.environ.get("BOSS_LICENSE_ENDPOINT", "").strip()
    if not ep:
        ep = str(config().get("endpoint") or "").strip()
    return ep.rstrip("/")


def device_id() -> str:
    """匿名设备指纹：只上传哈希，不上传 MAC / 用户名 / 路径原文。"""
    raw = "|".join([
        platform.system(), platform.machine(),
        str(uuid.getnode()), os.path.expanduser("~"),
    ])
    return "dev-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def valid_plan(plan) -> bool:
    return (
        isinstance(plan, dict)
        and bool(plan.get("plan_version"))
        and isinstance(plan.get("selectors"), dict)
        and isinstance(plan.get("js_snippets"), dict)
    )


def load_plan():
    """返回 (plan_dict, source, path)。source: cloud / bundle / None。"""
    for path, source in ((PLAN_CACHE, "cloud"), (BUNDLED_PLAN, "bundle")):
        if not path.exists():
            continue
        plan = _read_json(path)
        if valid_plan(plan):
            return plan, source, path
    return None, None, None


def selectors(group=None, plan=None):
    p = plan if plan is not None else load_plan()[0]
    sel = (p or {}).get("selectors", {})
    return sel.get(group, {}) if group else sel


def timing(plan=None):
    p = plan if plan is not None else load_plan()[0]
    return (p or {}).get("timing", {})


def urls(plan=None):
    p = plan if plan is not None else load_plan()[0]
    return (p or {}).get("urls", {})


def limits(plan=None):
    p = plan if plan is not None else load_plan()[0]
    return (p or {}).get("limits", {})


def popup_selectors(plan=None):
    p = plan if plan is not None else load_plan()[0]
    return (p or {}).get("popup_selectors", [])


def js(name, plan=None) -> str:
    """取一段 JS 模板（里面可能含 __XXX__ 占位符，用 render_js 填充）。"""
    p = plan if plan is not None else load_plan()[0]
    item = ((p or {}).get("js_snippets", {})).get(name) or {}
    return item.get("code", "") or ""


def render_js(name, plan=None, **variables) -> str:
    """把云端 JS 模板里的 __KEY__ 占位符替换成 JSON 字面量。

    例：document.querySelector(__SEL_BODY__) → document.querySelector(".job-detail-body .desc")
    列表 / 字典也可以传，会转成 JS 数组 / 对象字面量。
    """
    code = js(name, plan)
    for key, value in variables.items():
        code = code.replace(f"__{key.upper()}__",
                            json.dumps(value, ensure_ascii=False))
    return code


def _ua() -> str:
    """自定义 User-Agent。

    必须显式设置：Cloudflare 默认会把 Python-urllib / python-requests 这类
    默认 UA 判为爬虫并直接 1010 拦截（403），表现为"服务地址打不开"。
    """
    return f"BossDeliver/{SKILL_VERSION} (+https://boss.thjx.dpdns.org)"


def _http(url, payload=None):
    """返回 (status, dict_or_text)。网络异常统一返回 (0, 原因字符串)。"""
    base_headers = {"Accept": "application/json", "User-Agent": _ua()}
    try:
        if payload is None:
            req = Request(url, headers=base_headers)
        else:
            req = Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={**base_headers, "Content-Type": "application/json"},
                method="POST",
            )
        with _OPENER.open(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(body)
    except HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"error": body[:200]}
    except (URLError, OSError, ValueError, ssl.SSLError) as e:
        return 0, {"error": f"网络不可达: {e}"}


def verify(code: str, device: str = None):
    """POST /v1/verify。返回 (status, obj)。"""
    ep = endpoint()
    if not ep:
        return 0, {"error": "未配置服务地址（BOSS_LICENSE_ENDPOINT 或 scripts/cloud_config.json）"}
    return _http(f"{ep}/v1/verify", {"code": code, "device": device or device_id()})


def free_signin(device: str = None):
    """POST /v1/free —— 免费体验资格记账，防止删本地记录无限重置。返回 (status, obj)。"""
    ep = endpoint()
    if not ep:
        return 0, {"error": "未配置服务地址"}
    return _http(f"{ep}/v1/free", {"device": device or device_id()})


# ---------------- 授权文件验签 ----------------
# 服务端用 SIGNING_KEY 给「码|设备|到期日|指令包版本」签名，客户端用同一把钥匙验签。
# 配了 key：手改 expires_at 立刻暴露；没配 key：改用「每次运行都联网核验」兜底（见 entitlement.py）。

def signing_key() -> str:
    key = os.environ.get("BOSS_SIGNING_KEY", "").strip()
    if key:
        return key
    return str((config() or {}).get("signing_key") or "").strip()


def license_payload(lic: dict) -> str:
    return "%s|%s|%s|%s" % (
        lic.get("code") or "", lic.get("device") or "",
        lic.get("expires_at") or "", lic.get("plan_version") or "")


def verify_license_sig(lic: dict) -> bool | None:
    """True=签名有效；False=被改过；None=验不了（没配 key 或是老版本文件），交给联网核验。"""
    key = signing_key()
    if not key:
        return None
    got = str(lic.get("signature") or "")
    if not got:
        # 20261113 之前发出的老授权文件没有 signature，联一次网就会补签，不能直接判死
        return None
    want = hmac.new(key.encode("utf-8"), license_payload(lic).encode("utf-8"),
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(got, want)


def sync_plan(code: str = None, device: str = None):
    """GET /v1/plan 并写入本地缓存。成功返回 {"ok":True,...}，失败返回 {"ok":False,...}。"""
    ep = endpoint()
    if not ep:
        return {"ok": False, "reason": "未配置服务地址，继续使用随包附带的免费体验指令包"}
    if not code:
        lic = _read_json(LICENSE_FILE, {}) or {}
        code, device = lic.get("code"), lic.get("device")
    if not code:
        return {"ok": False, "reason": "尚未激活，无法从云端拉取新版指令包"}
    device = device or device_id()

    status, obj = _http(
        f"{ep}/v1/plan?code={quote(code)}&device={quote(device)}")
    if status != 200 or not isinstance(obj, dict):
        reason = (obj or {}).get("error", f"HTTP {status}") if isinstance(obj, dict) else f"HTTP {status}"
        return {"ok": False, "reason": reason}
    if not valid_plan(obj):
        return {"ok": False, "reason": "云端返回的指令包结构不完整，已保留本地缓存"}

    _write_json(PLAN_CACHE, obj)
    _write_json(SYNC_FILE, {
        "last_sync_at": datetime.now().isoformat(timespec="seconds"),
        "plan_version": obj.get("plan_version"),
    })
    return {"ok": True, "plan_version": obj.get("plan_version"),
            "selectors": sum(len(v) for v in obj.get("selectors", {}).values())}


def sync_status() -> dict:
    """离线宽限判断：最后一次成功同步超过 GRACE_HOURS 才算 degraded。"""
    info = _read_json(SYNC_FILE, {}) or {}
    last = info.get("last_sync_at")
    hours = None
    if last:
        try:
            hours = round((datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600, 1)
        except ValueError:
            pass
    return {
        "last_sync_at": last,
        "hours_since_sync": hours,
        "degraded": hours is not None and hours > GRACE_HOURS,
        "grace_hours": GRACE_HOURS,
    }


def plan_status() -> dict:
    plan, source, path = load_plan()
    return {
        "plan_version": (plan or {}).get("plan_version"),
        "source": source or "none",
        "path": str(path) if path else None,
        "selectors": sum(len(v) for v in (plan or {}).get("selectors", {}).values()),
        "js_snippets": len((plan or {}).get("js_snippets", {})),
        "endpoint": endpoint() or "(未配置)",
        "sync": sync_status(),
    }


def main():
    ap = argparse.ArgumentParser(description="云端指令包客户端")
    ap.add_argument("--status", action="store_true", help="查看指令包来源与同步状态")
    ap.add_argument("--sync", action="store_true", help="用已激活的码重新拉一次指令包")
    args = ap.parse_args()

    if args.sync:
        result = sync_plan()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    print(json.dumps(plan_status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

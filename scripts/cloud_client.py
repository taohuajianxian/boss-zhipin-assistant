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

云端指令包安全策略（与 dist/trust_anchor.json 配套，默认强制，失败即拒收）：
  - 签名：云端包必须带 HMAC-SHA256 签名，密钥按设备派生（服务端下发，客户端只验不签），
          消息体绑定「端点主机 + 包版本 + 内容摘要」，签名不符直接丢弃、保留旧缓存。
  - 摘要：整包按确定性序列化取 SHA-256，与签名内摘要逐字节比对，任何字段改动都会暴露。
  - 端点：scheme/host 固定自随包信任锚，运行期不接受静默变更；换端点需显式确认。
  - 版本：拒绝低于信任锚 min_version_date 的包，防回滚到旧版。
  - JS：只有随包登记过指纹的片段才会被注入，静态审查命中网络请求 / 动态求值 /
        存储写入等能力即拒收整包（见 scripts/plan_guard.py）。

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

from scripts.plan_guard import (  # noqa: E402
    ALLOWED_SNIPPETS, audit_plan, js_digest_map, plan_digest, version_date,
)

STATE_DIR = Path(os.environ.get("BOSS_LICENSE_DIR", str(Path.home() / ".boss_zhipin_deliver")))
LICENSE_FILE = STATE_DIR / "license.json"
PLAN_CACHE = STATE_DIR / "plan.json"
PLAN_ATTEST = STATE_DIR / "plan.attest.json"
SYNC_FILE = STATE_DIR / "sync.json"
USAGE_FILE = STATE_DIR / "usage.json"
ENDPOINT_ACK = STATE_DIR / "endpoint_ack.json"
CONFIG_FILE = SKILL_DIR / "scripts" / "cloud_config.json"
BUNDLED_PLAN = SKILL_DIR / "dist" / "plan.json"
TRUST_FILE = SKILL_DIR / "dist" / "trust_anchor.json"

GRACE_HOURS = 24
TIMEOUT = 8
SKILL_VERSION = "20261115"   # 与 skill.md 的 version 保持一致，仅用于 User-Agent
SIG_ALG = "HMAC-SHA256"


class PlanRejected(Exception):
    """云端 / 本地指令包未通过完整性校验。抛出即表示「不得使用该包」。"""


class EndpointRejected(Exception):
    """端点与随包信任锚不一致，且未获得用户显式确认。"""

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
    """模板 + 本地覆盖。如需自定义服务地址 / 联系方式，写进同目录 cloud_config.local.json，
    便于保留个人设置。"""
    base = _read_json(CONFIG_FILE, {}) or {}
    local_path = CONFIG_FILE.with_name("cloud_config.local.json")
    local = _read_json(local_path, {}) or {}
    override = {k: v for k, v in local.items() if not k.startswith("_")}
    return {**base, **override}


def trust_anchor() -> dict:
    """随包信任根：固定端点主体、最低包版本、可注入 JS 的指纹白名单。"""
    return _read_json(TRUST_FILE, {}) or {}


def _parse_endpoint(ep: str):
    from urllib.parse import urlparse

    return urlparse(ep if "://" in ep else "https://" + ep)


def endpoint_ack() -> dict:
    """用户显式确认过的端点变更记录（由 CLI --accept-endpoint 写入）。"""
    return _read_json(ENDPOINT_ACK, {}) or {}


def check_endpoint(ep: str) -> str:
    """端点守门：scheme / host 必须与随包信任锚一致。返回告警文本，不合规抛 EndpointRejected。"""
    anchor = trust_anchor()
    fixed = anchor.get("endpoint") or {}
    u = _parse_endpoint(ep)
    if not u.hostname:
        raise EndpointRejected(f"无法解析服务地址: {ep}")
    require_https = bool((anchor.get("plan") or {}).get("require_https", True))
    if require_https and u.scheme != "https":
        ack = endpoint_ack()
        if u.hostname != str(ack.get("host") or ""):
            raise EndpointRejected(
                f"服务地址不是 HTTPS（{u.scheme}://{u.hostname}），且未获显式确认，已拒绝连接")
    pinned_host = str(fixed.get("host") or "").strip().lower()
    if not pinned_host:
        return ""            # 信任锚缺失（旧安装）时只强制 HTTPS
    if u.hostname.lower() == pinned_host:
        return ""
    ack = endpoint_ack()
    if str(ack.get("host") or "").lower() == u.hostname.lower():
        return (f"注意：服务地址已由用户显式切换到 {u.hostname}"
                f"（{ack.get('confirmed_at') or '时间未知'}），与随包固定的 {pinned_host} 不同")
    raise EndpointRejected(
        f"服务地址 {u.hostname} 与随包固定端点 {pinned_host} 不一致。"
        f"如确需切换，请运行 python scripts/cloud_client.py --accept-endpoint {ep}")


def endpoint(strict: bool = True) -> str:
    """服务地址：环境变量优先，其次 scripts/cloud_config.json。留空 = 纯离线免费体验。
    strict=True 时执行端点守门，不合规抛 EndpointRejected，调用方不得继续联网。"""
    ep = os.environ.get("BOSS_LICENSE_ENDPOINT", "").strip()
    if not ep:
        ep = str(config().get("endpoint") or "").strip()
    ep = ep.rstrip("/")
    if ep and strict:
        check_endpoint(ep)
    return ep


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


def allowed_snippets() -> set:
    names = ((trust_anchor().get("js") or {}).get("allowed_names") or [])
    return set(names) | set(ALLOWED_SNIPPETS)


def record_reject(source: str, problems):
    """把拒收原因落到本地，便于 --status 回溯（不阻断使用，静默失败才是真风险）。"""
    info = _read_json(SYNC_FILE, {}) or {}
    info["last_reject"] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "reasons": list(problems)[:8],
    }
    _write_json(SYNC_FILE, info)


def verify_cached_plan(plan) -> list:
    """本地缓存的复核：静态审查 + 与同步时记录的内容摘要逐字节比对。"""
    problems = audit_plan(plan, trust_anchor() or None)
    attest = _read_json(PLAN_ATTEST, {}) or {}
    if not attest:
        problems.append("缺少同步凭证（plan.attest.json），无法证明缓存来源与完整性")
    else:
        if str(attest.get("digest") or "") != plan_digest(plan):
            problems.append("缓存内容摘要与同步凭证不一致（缓存被改动）")
        if str(attest.get("plan_version") or "") != str(plan.get("plan_version") or ""):
            problems.append("缓存版本与同步凭证不一致")
    return problems


_PLAN_MEMO = {"ready": False, "value": (None, None, None)}


def load_plan(force: bool = False):
    """返回 (plan_dict, source, path)。source: cloud / bundle / None。

    两条来源都要过守门：云端缓存需有同步凭证且摘要一致；随包包需通过静态审查与
    JS 指纹白名单。任一不过即降级到下一来源，绝不放行来路不明的指令包。
    """
    if _PLAN_MEMO["ready"] and not force:
        return _PLAN_MEMO["value"]

    result = (None, None, None)
    if PLAN_CACHE.exists():
        cached = _read_json(PLAN_CACHE)
        if valid_plan(cached):
            problems = verify_cached_plan(cached)
            if problems:
                record_reject("cloud-cache", problems)
            else:
                result = (cached, "cloud", PLAN_CACHE)
    if result[0] is None and BUNDLED_PLAN.exists():
        bundled = _read_json(BUNDLED_PLAN)
        if valid_plan(bundled):
            problems = audit_plan(bundled, trust_anchor() or None)
            if problems:
                record_reject("bundle", problems)
            else:
                result = (bundled, "bundle", BUNDLED_PLAN)
    _PLAN_MEMO.update(ready=True, value=result)
    return result


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
    try:
        ep = endpoint()
    except EndpointRejected as e:
        return 0, {"error": str(e)}
    if not ep:
        return 0, {"error": "未配置服务地址（BOSS_LICENSE_ENDPOINT 或 scripts/cloud_config.json）"}
    return _http(f"{ep}/v1/verify", {"code": code, "device": device or device_id()})


def free_signin(device: str = None):
    """POST /v1/free —— 免费体验资格记录，防止删本地记录无限重置。返回 (status, obj)。"""
    try:
        ep = endpoint()
    except EndpointRejected as e:
        return 0, {"error": str(e)}
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


# ---------------- 云端指令包完整性校验 ----------------
# 威胁模型：服务端或传输链路被攻破后，下发被篡改的指令包（含可执行 JS）注入已登录页面。
# 对策：内容摘要 + 按设备派生密钥的签名 + 端点/版本固定 + JS 指纹白名单，任一不符即拒收。

def plan_key() -> str:
    """验签密钥：按设备在服务端派生后随授权文件下发，客户端只保存不生成。
    某台设备的密钥泄露只影响该设备，无法用来伪造下发给其他设备的包。"""
    key = os.environ.get("BOSS_PLAN_KEY", "").strip()
    if key:
        return key
    lic = _read_json(LICENSE_FILE, {}) or {}
    return str(lic.get("plan_key") or "").strip()


def integrity_message(host: str, plan_version: str, digest: str) -> bytes:
    """签名消息体：把端点主机、包版本、内容摘要绑在一起，防跨设备/跨版本重放。"""
    return f"plan-v1|{host}|{plan_version}|{digest}".encode("utf-8")


def unwrap_plan(obj):
    """服务端用信封结构 {envelope_version, plan, integrity} 下发。裸包视为未签名。"""
    if isinstance(obj, dict) and isinstance(obj.get("plan"), dict):
        return obj["plan"], (obj.get("integrity") or {})
    return obj, {}


def verify_integrity(plan: dict, integrity: dict, ep: str) -> None:
    """签名 + 摘要校验。不通过抛 PlanRejected。"""
    anchor = trust_anchor()
    require_sig = bool((anchor.get("plan") or {}).get("require_signature", True))
    if not integrity:
        if require_sig:
            raise PlanRejected("云端指令包未附带签名，已拒绝加载（保留本地缓存）")
        return

    if str(integrity.get("alg") or "") != SIG_ALG:
        raise PlanRejected(f"不支持的签名算法: {integrity.get('alg')!r}")

    digest = plan_digest(plan)
    if not hmac.compare_digest(str(integrity.get("digest") or ""), digest):
        raise PlanRejected("指令包内容摘要与签名声明不符（内容被改动）")
    if str(integrity.get("plan_version") or "") != str(plan.get("plan_version") or ""):
        raise PlanRejected("签名声明的包版本与包内版本不一致")

    key = plan_key()
    if not key:
        raise PlanRejected("缺少验签密钥（未激活或授权文件不含 plan_key），已拒绝加载云端指令包")

    host = (_parse_endpoint(ep).hostname or "").lower()
    want = hmac.new(key.encode("utf-8"),
                    integrity_message(host, plan.get("plan_version"), digest),
                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, str(integrity.get("sig") or "")):
        raise PlanRejected("指令包签名校验失败（来源不可信）")


def check_version_floor(plan: dict) -> None:
    """防回滚：不接受低于随包要求的最低版本日期的包。"""
    floor = str((trust_anchor().get("plan") or {}).get("min_version_date") or "")
    if not floor:
        return
    got = version_date(plan.get("plan_version"))
    if not got or got < floor:
        raise PlanRejected(
            f"指令包版本 {plan.get('plan_version')} 早于随包要求的最低版本 {floor}，已拒绝回滚")


def _refresh_plan_key(code: str, device: str) -> bool:
    """老版授权文件里没有验签密钥时，联网向服务端补领一次（/v1/verify 会下发）。"""
    status, obj = verify(code, device)
    if status != 200 or not isinstance(obj, dict) or not obj.get("ok"):
        return False
    key = ((obj.get("license") or {}).get("plan_key") or "").strip()
    if not key:
        return False
    lic = _read_json(LICENSE_FILE, {}) or {}
    lic["plan_key"] = key
    lic["last_ok_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(LICENSE_FILE, lic)
    return True


def sync_plan(code: str = None, device: str = None):
    """GET /v1/plan，通过完整性校验后写入本地缓存与同步凭证。

    成功 {"ok":True,...}；任何校验失败都返回 {"ok":False,"reason":...} 且不改动现有缓存。
    """
    try:
        ep = endpoint()
    except EndpointRejected as e:
        return {"ok": False, "reason": str(e)}
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

    plan, integrity = unwrap_plan(obj)

    # 1) 结构 + JS 静态审查 + 指纹白名单
    problems = audit_plan(plan, trust_anchor() or None)
    if problems:
        record_reject("cloud", problems)
        return {"ok": False, "rejected": True,
                "reason": "云端指令包未通过安全审查，已拒绝加载并保留本地缓存",
                "details": problems[:5]}
    # 2) 签名与摘要
    try:
        if not plan_key() and code:
            _refresh_plan_key(code, device)      # 老授权文件补领验签密钥
        verify_integrity(plan, integrity, ep)
        # 3) 版本回滚
        check_version_floor(plan)
    except PlanRejected as e:
        record_reject("cloud", [str(e)])
        return {"ok": False, "rejected": True, "reason": str(e)}

    digest = plan_digest(plan)
    _write_json(PLAN_CACHE, plan)
    _write_json(PLAN_ATTEST, {
        "plan_version": plan.get("plan_version"),
        "digest": digest,
        "alg": SIG_ALG,
        "key_id": str(integrity.get("key_id") or ""),
        "sig": str(integrity.get("sig") or ""),
        "endpoint_host": (_parse_endpoint(ep).hostname or ""),
        "synced_at": datetime.now().isoformat(timespec="seconds"),
    })
    info = _read_json(SYNC_FILE, {}) or {}
    info.pop("last_reject", None)
    info.update({
        "last_sync_at": datetime.now().isoformat(timespec="seconds"),
        "plan_version": plan.get("plan_version"),
        "digest": digest,
        "verified": True,
    })
    _write_json(SYNC_FILE, info)
    load_plan(force=True)          # 让内存缓存立刻反映新包
    return {"ok": True, "plan_version": plan.get("plan_version"),
            "digest": digest[:16],
            "selectors": sum(len(v) for v in plan.get("selectors", {}).values()),
            "js_snippets": len(plan.get("js_snippets", {}))}


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


def integrity_report() -> dict:
    """给 --status / --verify 用的完整性快照。"""
    anchor = trust_anchor()
    plan, source, _ = load_plan()
    attest = _read_json(PLAN_ATTEST, {}) or {}
    cached_digest = plan_digest(plan) if plan else ""
    report = {
        "anchor_present": bool(anchor),
        "pinned_host": ((anchor.get("endpoint") or {}).get("host")),
        "min_version_date": ((anchor.get("plan") or {}).get("min_version_date")),
        "require_signature": bool((anchor.get("plan") or {}).get("require_signature", True)),
        "plan_source": source or "none",
        "plan_digest": cached_digest[:16] if cached_digest else None,
        "signature_verified_cache": bool(
            attest and str(attest.get("digest")) == cached_digest and attest.get("sig")),
        "signature_verified_at": attest.get("synced_at"),
        "key_present": bool(plan_key()),
        "js_allowlist": sorted(allowed_snippets()),
        "js_policy": ((anchor.get("js") or {}).get("policy")),
    }
    info = _read_json(SYNC_FILE, {}) or {}
    if info.get("last_reject"):
        report["last_reject"] = info["last_reject"]
    return report


def plan_status() -> dict:
    plan, source, path = load_plan()
    anchor = trust_anchor()
    ep_err = None
    try:
        ep = endpoint()
        check_endpoint(ep) if ep else ""
    except EndpointRejected as e:
        ep, ep_err = "", str(e)
    return {
        "plan_version": (plan or {}).get("plan_version"),
        "source": source or "none",
        "path": str(path) if path else None,
        "selectors": sum(len(v) for v in (plan or {}).get("selectors", {}).values()),
        "js_snippets": len((plan or {}).get("js_snippets", {})),
        "endpoint": ep or "(未配置)",
        "endpoint_rejected": ep_err,
        "pinned_host": ((anchor.get("endpoint") or {}).get("host")),
        "integrity": integrity_report(),
        "sync": sync_status(),
    }


def main():
    ap = argparse.ArgumentParser(description="云端指令包客户端")
    ap.add_argument("--status", action="store_true", help="查看指令包来源、完整性状态与同步情况")
    ap.add_argument("--sync", action="store_true", help="用已激活的码重新拉一次指令包（强制验签）")
    ap.add_argument("--verify", action="store_true", help="只做本地完整性与 JS 白名单核验，不联网")
    ap.add_argument("--accept-endpoint", metavar="URL",
                    help="显式确认切换到新的服务地址（会记录到本机，便于回溯）")
    args = ap.parse_args()

    if args.accept_endpoint:
        url = args.accept_endpoint.strip().rstrip("/")
        try:
            u = _parse_endpoint(url)
            pinned = ((trust_anchor().get("endpoint") or {}).get("host") or "")
        except Exception as e:                       # noqa: BLE001
            print(f"地址无法解析: {e}")
            return 1
        _write_json(ENDPOINT_ACK, {
            "endpoint": url,
            "host": u.hostname,
            "pinned_host": pinned,
            "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        })
        print(f"已记录端点确认: {url}")
        print(f"  随包固定端点: {pinned or '(无)'}")
        print("  后续连接将放行该主机；如需撤销，删除 "
              f"{ENDPOINT_ACK} 即可。")
        return 0

    if args.sync:
        result = sync_plan()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.verify:
        plan, source, path = load_plan(force=True)
        report = integrity_report()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not plan:
            print("FAIL：没有可用于运行的指令包（缓存与随包包均未通过校验）")
            return 1
        print(f"PASS：正在使用的指令包来自 {source}，摘要 {report['plan_digest']}")
        return 0

    print(json.dumps(plan_status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

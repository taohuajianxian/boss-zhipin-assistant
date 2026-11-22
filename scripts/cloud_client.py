#!/usr/bin/env python3
# -*- coding: utf-8 -*-


















from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import platform
import socket
import ssl
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

SKILL_DIR = Path(__file__).resolve().parent.parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from scripts.plan_guard import (
    ALLOWED_SNIPPETS, audit_plan, js_digest_map, plan_digest, version_date,
)

try:
    from scripts import plan_crypt
except ImportError:
    import plan_crypt

try:
    from scripts.atomic_io import atomic_write_text
except ImportError:
    from atomic_io import atomic_write_text

STATE_DIR = Path(os.environ.get("BOSS_LICENSE_DIR", str(Path.home() / ".boss_zhipin_deliver")))
LICENSE_FILE = STATE_DIR / "license.json"
PLAN_CACHE = STATE_DIR / "plan.json"
PLAN_ATTEST = STATE_DIR / "plan.attest.json"
PLAN_KEY_FILE = STATE_DIR / "plan_key.json"
SYNC_FILE = STATE_DIR / "sync.json"
USAGE_FILE = STATE_DIR / "usage.json"
ENDPOINT_ACK = STATE_DIR / "endpoint_ack.json"
CONFIG_FILE = SKILL_DIR / "scripts" / "cloud_config.json"
BUNDLED_PLAN = SKILL_DIR / "dist" / "plan.json"
TRUST_FILE = SKILL_DIR / "dist" / "trust_anchor.json"

GRACE_HOURS = 24
TIMEOUT = 8
AUTO_SYNC_TIMEOUT = 3


def _skill_version() -> str:





    try:
        for line in (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("version:"):
                raw = line.split(":", 1)[1].strip().strip("'\"").lstrip("vV")
                raw = raw.split("-")[0].split("+")[0].strip()
                parts = [p for p in raw.split(".") if p.strip().isdigit()]
                if len(parts) == 3:
                    y, m, d = parts
                    y = y if len(y) == 4 else "20" + y
                    return f"{y}{int(m):02d}{int(d):02d}"
                digits = "".join(ch for ch in raw if ch.isdigit())
                if digits:
                    return digits
    except Exception:
        pass
    return "unknown"


SKILL_VERSION = _skill_version()
SIG_ALG = "HMAC-SHA256"


class PlanRejected(Exception):
    pass


class EndpointRejected(Exception):
    pass


def _ssl_context():






    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_OPENER = build_opener(ProxyHandler({}), HTTPSHandler(context=_ssl_context()))
















CF_FALLBACK_IPS = (
    "104.18.0.1",
    "104.17.0.1",
    "104.20.0.1",
    "104.19.0.1",
    "188.114.96.1",
)
IP_PIN_FILE = STATE_DIR / "endpoint_ip.json"
IP_PIN_TTL = 30 * 60
IP_PROBE_TIMEOUT = 5
IP_MAX_TRIES = 3
_IP_PIN = {"host": None, "ips": []}


def _fallback_enabled() -> bool:
    return os.environ.get("BOSS_LICENSE_NO_IPFALLBACK", "").strip().lower() not in ("1", "true", "yes")


def _fallback_ips() -> list:
    raw = os.environ.get("BOSS_LICENSE_IPS", "").strip()
    if raw:
        return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    ips = []
    try:
        cfg = _read_json(CONFIG_FILE, {}) or {}
        ips = [str(x).strip() for x in (cfg.get("fallback_ips") or []) if str(x).strip()]
    except Exception:
        pass
    return ips + [ip for ip in CF_FALLBACK_IPS if ip not in ips]


def _url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


@contextlib.contextmanager
def _pinned_inet(host: str, ip: str):

    original = socket.getaddrinfo
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET

    def patched(h, port, family=0, type=0, proto=0, flags=0):
        if str(h).lower() == host:
            return original(ip, port, fam, type, proto, flags)
        return original(h, port, family, type, proto, flags)

    socket.getaddrinfo = patched
    try:
        yield
    finally:
        socket.getaddrinfo = original


def _public_host(host: str) -> bool:

    if not host or ":" in host and host.count(":") > 1:
        return False
    if host in ("localhost",) or host.endswith(".local"):
        return False
    try:
        socket.inet_aton(host)
        return False
    except OSError:
        return "." in host


def _race_addresses(host: str, timeout=None):





    import threading

    give_up = timeout or IP_PROBE_TIMEOUT
    cands = []
    try:
        for _fam, _t, _p, _c, sa in socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM):
            cands.append(sa[0])
    except Exception:
        pass
    for ip in _fallback_ips():
        if ip not in cands:
            cands.append(ip)

    done = []
    lock = threading.Lock()

    def probe(ip):
        t0 = time.time()
        try:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            raw = socket.socket(fam, socket.SOCK_STREAM)
            raw.settimeout(give_up)
            raw.connect((ip, 443))
            ss = _ssl_context().wrap_socket(raw, server_hostname=host)
            ss.close()
            with lock:
                done.append((time.time() - t0, ip))
        except Exception:
            pass

    threads = []
    for ip in cands:
        th = threading.Thread(target=probe, args=(ip,), daemon=True)
        th.start()
        threads.append(th)

    start = time.time()
    deadline = start + give_up + 0.5
    while time.time() < deadline:
        with lock:
            if done:
                if time.time() - start >= min(d[0] for d in done) + 0.2:
                    break
        time.sleep(0.05)
    with lock:
        snapshot = sorted(done)
    if not snapshot:
        return []
    return [ip for _dt, ip in snapshot]


def _pinned_ip(host: str):

    if not host:
        return []
    if _IP_PIN["host"] == host and _IP_PIN["ips"]:
        return list(_IP_PIN["ips"])
    data = _read_json(IP_PIN_FILE, {}) or {}
    if data.get("host") == host:
        ips = [str(x) for x in (data.get("ips") or []) if str(x).strip()]
        if not ips and data.get("ip"):
            ips = [str(data["ip"])]
        if ips:
            try:
                if time.time() - float(data.get("ts") or 0) < IP_PIN_TTL:
                    _IP_PIN.update(host=host, ips=ips)
                    return list(ips)
            except Exception:
                pass
    return []


def _pin_ip(host: str, ips):
    ips = [ip for ip in dict.fromkeys(ips) if ip]
    if not ips:
        return
    _IP_PIN.update(host=host, ips=ips)
    try:
        ensure_state_dir()
        _write_json(IP_PIN_FILE, {"host": host, "ips": ips, "ip": ips[0], "ts": time.time()})
    except Exception:
        pass


def _unpin_ip():
    _IP_PIN.update(host=None, ips=[])
    try:
        IP_PIN_FILE.unlink()
    except Exception:
        pass


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data):
    ensure_state_dir()
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def config() -> dict:


    base = _read_json(CONFIG_FILE, {}) or {}
    local_path = CONFIG_FILE.with_name("cloud_config.local.json")
    local = _read_json(local_path, {}) or {}
    override = {k: v for k, v in local.items() if not k.startswith("_")}
    return {**base, **override}


def trust_anchor() -> dict:

    return _read_json(TRUST_FILE, {}) or {}


def _parse_endpoint(ep: str):
    from urllib.parse import urlparse

    return urlparse(ep if "://" in ep else "https://" + ep)


def endpoint_ack() -> dict:

    return _read_json(ENDPOINT_ACK, {}) or {}


def check_endpoint(ep: str) -> str:

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
        return ""
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


    ep = os.environ.get("BOSS_LICENSE_ENDPOINT", "").strip()
    if not ep:
        ep = str(config().get("endpoint") or "").strip()
    ep = ep.rstrip("/")
    if ep and strict:
        check_endpoint(ep)
    return ep


def device_id() -> str:

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

    info = _read_json(SYNC_FILE, {}) or {}
    info["last_reject"] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "reasons": list(problems)[:8],
    }
    _write_json(SYNC_FILE, info)


def verify_cached_plan(plan) -> list:

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



        inner = (bundled.get("plan_enc") if isinstance(bundled, dict) else None)
        if plan_crypt.is_blob(bundled) or plan_crypt.is_blob(inner):
            blob = inner if plan_crypt.is_blob(inner) else bundled
            key_hex = str(plan_enc_key_cached().get("key") or "")
            try:
                if not key_hex:
                    raise plan_crypt.PlanCryptError("本机没有可用凭据")
                bundled = plan_crypt.decrypt_blob(blob, key_hex)
            except plan_crypt.PlanCryptError as e:
                record_reject("bundle", [f"随包数据无法解码：{e}；"
                                         "请联网运行一次以申领凭据"])
                bundled = None
        if isinstance(bundled, dict) and valid_plan(bundled):
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

    p = plan if plan is not None else load_plan()[0]
    item = ((p or {}).get("js_snippets", {})).get(name) or {}
    return item.get("code", "") or ""


def render_js(name, plan=None, **variables) -> str:





    code = js(name, plan)
    for key, value in variables.items():
        code = code.replace(f"__{key.upper()}__",
                            json.dumps(value, ensure_ascii=False))
    return code


def _ua() -> str:





    return f"BossDeliver/{SKILL_VERSION} (+https://boss.thjx.dpdns.org)"


def _http_raw(url, payload=None, timeout=None, host=None, ip=None):

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
        pin_ctx = _pinned_inet(host, ip) if (ip and host) else contextlib.nullcontext()
        with pin_ctx:
            with _OPENER.open(req, timeout=timeout or TIMEOUT) as resp:
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


def _http(url, payload=None, timeout=None):






    host = _url_host(url)
    tmo = timeout or TIMEOUT
    short = min(IP_PROBE_TIMEOUT, tmo)

    cached = _pinned_ip(host)
    if cached:
        for ip in cached[:IP_MAX_TRIES]:
            status, obj = _http_raw(url, payload, short, host, ip)
            if status > 0:
                return status, obj
        _unpin_ip()

    can_probe = bool(host) and _fallback_enabled() and _public_host(host) and tmo >= 4
    if can_probe:
        alive = _race_addresses(host, timeout=min(IP_PROBE_TIMEOUT, tmo))
        for ip in alive[:2]:
            status, obj = _http_raw(url, payload, short, host, ip)
            if status > 0:
                _pin_ip(host, alive)
                return status, obj

    return _http_raw(url, payload, tmo)


def verify(code: str, device: str = None, timeout=None):

    try:
        ep = endpoint()
    except EndpointRejected as e:
        return 0, {"error": str(e)}
    if not ep:
        return 0, {"error": "未配置服务地址（BOSS_LICENSE_ENDPOINT 或 scripts/cloud_config.json）"}
    return _http(f"{ep}/v1/verify", {"code": code, "device": device or device_id()}, timeout=timeout)


def free_signin(device: str = None):

    try:
        ep = endpoint()
    except EndpointRejected as e:
        return 0, {"error": str(e)}
    if not ep:
        return 0, {"error": "未配置服务地址"}
    return _http(f"{ep}/v1/free", {"device": device or device_id()})






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

    key = signing_key()
    if not key:
        return None
    got = str(lic.get("signature") or "")
    if not got:

        return None
    want = hmac.new(key.encode("utf-8"), license_payload(lic).encode("utf-8"),
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(got, want)






def plan_key() -> str:


    key = os.environ.get("BOSS_PLAN_KEY", "").strip()
    if key:
        return key
    lic = _read_json(LICENSE_FILE, {}) or {}
    return str(lic.get("plan_key") or "").strip()


def integrity_message(host: str, plan_version: str, digest: str) -> bytes:

    return f"plan-v1|{host}|{plan_version}|{digest}".encode("utf-8")






def plan_enc_key_cached() -> dict:
    return _read_json(PLAN_KEY_FILE, {}) or {}


def _save_plan_enc_key(key_obj) -> None:
    if not isinstance(key_obj, dict) or not key_obj.get("key"):
        return
    rec = {
        "key": str(key_obj.get("key") or ""),
        "alg": str(key_obj.get("alg") or ""),
        "plan_version": str(key_obj.get("plan_version") or ""),
        "exp": int(key_obj.get("exp") or 0),
        "ttl_hours": int(key_obj.get("ttl_hours") or 0),
        "tier": str(key_obj.get("tier") or ""),
        "device": device_id(),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(PLAN_KEY_FILE, rec)


def plan_key_expired(skew_seconds: int = 300) -> bool:

    exp = int(plan_enc_key_cached().get("exp") or 0)
    return exp <= int(time.time()) + int(skew_seconds)


def request_plan_key(ep: str, code: str = None, device: str = None,
                     timeout=None) -> dict:

    if not (code and device):
        lic = _read_json(LICENSE_FILE, {}) or {}
        code = code or lic.get("code")
        device = device or lic.get("device")
    device = device or device_id()
    body = {"device": device,
            "plan_version": str(plan_enc_key_cached().get("plan_version") or "")}
    if code:
        body["code"] = code
    try:
        status, obj = _http(f"{ep}/v1/key", payload=body, timeout=timeout)
    except (HTTPError, URLError, OSError) as e:
        return {"ok": False, "reason": f"钥匙申领失败（{e}）"}
    if status != 200 or not isinstance(obj, dict) or not obj.get("ok"):
        reason = (obj or {}).get("error", "") if isinstance(obj, dict) else ""
        return {"ok": False, "reason": reason or f"HTTP {status}"}
    _save_plan_enc_key(obj)
    return {"ok": True, "tier": obj.get("tier"), "exp": obj.get("exp"),
            "ttl_hours": obj.get("ttl_hours"),
            "version_match": obj.get("version_match")}


def unwrap_plan(obj):


    if isinstance(obj, dict) and plan_crypt.is_blob(obj.get("plan_enc")):
        blob = obj["plan_enc"]
        candidates = []
        key_obj = obj.get("key") if isinstance(obj.get("key"), dict) else {}
        if key_obj.get("key"):
            _save_plan_enc_key(key_obj)
            candidates.append(str(key_obj["key"]))
        cached = str(plan_enc_key_cached().get("key") or "")
        if cached and cached not in candidates:
            candidates.append(cached)
        if not candidates:
            raise plan_crypt.PlanCryptError("云端下发了数据，但本机没有可用凭据")
        last = None
        for key_hex in candidates:
            try:
                return (plan_crypt.decrypt_blob(blob, key_hex),
                        (obj.get("integrity") or {}))
            except plan_crypt.PlanCryptError as e:
                last = e
        raise last
    if isinstance(obj, dict) and isinstance(obj.get("plan"), dict):
        return obj["plan"], (obj.get("integrity") or {})
    return obj, {}


def verify_integrity(plan: dict, integrity: dict, ep: str) -> None:

    anchor = trust_anchor()
    require_sig = bool((anchor.get("plan") or {}).get("require_signature", True))
    if not integrity:
        if require_sig:
            raise PlanRejected("云端数据未附带核对信息，已拒绝加载（保留本地缓存）")
        return

    if str(integrity.get("alg") or "") != SIG_ALG:
        raise PlanRejected(f"不支持的核对算法: {integrity.get('alg')!r}")

    digest = plan_digest(plan)
    if not hmac.compare_digest(str(integrity.get("digest") or ""), digest):
        raise PlanRejected("适配数据内容摘要与声明不符（内容被改动）")
    if str(integrity.get("plan_version") or "") != str(plan.get("plan_version") or ""):
        raise PlanRejected("声明的数据版本与数据内版本不一致")

    key = plan_key()
    if not key:
        raise PlanRejected("缺少核对凭据（未激活或授权文件不含 plan_key），已拒绝加载云端数据")

    host = (_parse_endpoint(ep).hostname or "").lower()
    want = hmac.new(key.encode("utf-8"),
                    integrity_message(host, plan.get("plan_version"), digest),
                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, str(integrity.get("sig") or "")):
        raise PlanRejected("适配数据核对失败（来源不可信）")


def check_version_floor(plan: dict) -> None:

    floor = str((trust_anchor().get("plan") or {}).get("min_version_date") or "")
    if not floor:
        return
    got = version_date(plan.get("plan_version"))
    if not got or got < floor:
        raise PlanRejected(
            f"适配数据版本 {plan.get('plan_version')} 早于随包要求的最低版本 {floor}，已拒绝回滚")


def _refresh_plan_key(code: str, device: str, timeout=None) -> bool:

    status, obj = verify(code, device, timeout)
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


def sync_plan(code: str = None, device: str = None, timeout=None):




    try:
        ep = endpoint()
    except EndpointRejected as e:
        return {"ok": False, "reason": str(e)}
    if not ep:
        return {"ok": False, "reason": "未配置服务地址，继续使用随包附带的免费体验适配数据"}
    lic = _read_json(LICENSE_FILE, {}) or {}
    if not code:
        code = lic.get("code")

    device = device or lic.get("device") or device_id()



    free_mode = not str(code or "").strip()
    if free_mode:
        st, fo = free_signin(device)
        if st != 200 or not isinstance(fo, dict) or not fo.get("free_today"):
            reason = ""
            if isinstance(fo, dict):
                reason = str(fo.get("reason") or fo.get("error") or "")
            return {"ok": False, "free": True, "need_activation": True,
                    "reason": reason or f"免费体验资格校验失败（HTTP {st}）"}
        query = f"device={quote(device)}&enc=1"
    else:
        query = f"code={quote(code)}&device={quote(device)}&enc=1"

    status, obj = _http(f"{ep}/v1/plan?{query}", timeout=timeout)
    if status != 200 or not isinstance(obj, dict):
        reason = (obj or {}).get("error", f"HTTP {status}") if isinstance(obj, dict) else f"HTTP {status}"
        return {"ok": False, "reason": reason}



    try:
        plan, integrity = unwrap_plan(obj)
    except plan_crypt.PlanCryptError as first_err:
        renewed = request_plan_key(ep, None if free_mode else code, device,
                                   timeout=timeout)
        try:
            plan, integrity = unwrap_plan(obj)
        except plan_crypt.PlanCryptError as e:
            record_reject("cloud", [str(first_err), str(e)])
            hint = ("" if renewed.get("ok")
                    else f"；钥匙续期失败：{renewed.get('reason') or '未知原因'}")
            return {"ok": False, "rejected": True,
                    "reason": f"云端适配数据解码失败：{e}{hint}"}


    problems = audit_plan(plan, trust_anchor() or None)
    if problems:
        record_reject("cloud", problems)
        return {"ok": False, "rejected": True,
                "reason": "云端适配数据未通过核对，已拒绝加载并保留本地缓存",
                "details": problems[:5]}

    try:
        if free_mode:



            if integrity and str(integrity.get("digest") or "") != plan_digest(plan):
                raise PlanRejected("适配数据内容摘要与声明不符（内容被改动）")
        else:
            if not plan_key() and code:
                _refresh_plan_key(code, device, timeout)
            verify_integrity(plan, integrity, ep)

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
        "tier": "free" if free_mode else "paid",
    })
    _write_json(SYNC_FILE, info)
    load_plan(force=True)
    return {"ok": True, "plan_version": plan.get("plan_version"),
            "digest": digest[:16],
            "selectors": sum(len(v) for v in plan.get("selectors", {}).values()),
            "js_snippets": len(plan.get("js_snippets", {}))}


def auto_sync_if_stale(max_age_hours: float = None, timeout: float = None) -> dict:












    hours_limit = GRACE_HOURS if max_age_hours is None else float(max_age_hours)
    out = {"attempted": False, "synced": False, "changed": False,
           "from": None, "to": None, "reason": ""}
    try:
        lic = _read_json(LICENSE_FILE, {}) or {}
        if not str(lic.get("code") or "").strip():


            has_key = bool(str(plan_enc_key_cached().get("key") or ""))
            if has_key and not plan_key_expired() and PLAN_CACHE.exists():
                out["reason"] = "免费体验中，钥匙未过期，无需同步"
                return out

        info = _read_json(SYNC_FILE, {}) or {}
        stamps = [x for x in (info.get("last_sync_at"), info.get("last_auto_attempt_at")) if x]
        if stamps:
            try:
                gap = (datetime.now() - datetime.fromisoformat(max(stamps))).total_seconds() / 3600
                if gap < hours_limit:
                    out["reason"] = f"距上次同步 {gap:.1f}h，未满 {hours_limit:g}h"
                    return out
            except ValueError:
                pass

        out["attempted"] = True
        before = str(info.get("plan_version") or "")
        res = sync_plan(timeout=timeout or AUTO_SYNC_TIMEOUT)

        fresh = _read_json(SYNC_FILE, {}) or {}
        fresh["last_auto_attempt_at"] = datetime.now().isoformat(timespec="seconds")
        _write_json(SYNC_FILE, fresh)

        if res.get("ok"):
            out["synced"] = True
            out["from"] = before or None
            out["to"] = str(res.get("plan_version") or "")
            out["changed"] = bool(before) and before != out["to"]
        else:
            out["reason"] = str(res.get("reason") or "同步失败")
    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {e}"
    return out


def sync_status() -> dict:

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

    anchor = trust_anchor()
    plan, source, _ = load_plan()
    attest = _read_json(PLAN_ATTEST, {}) or {}
    cached_digest = plan_digest(plan) if plan else ""
    enc_key = plan_enc_key_cached()
    key_exp = int(enc_key.get("exp") or 0)
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
        "plan_enc_key_expires_at": (datetime.fromtimestamp(key_exp).isoformat(timespec="seconds")
                                if key_exp else None),
        "plan_enc_key_ttl_hours": int(enc_key.get("ttl_hours") or 0) or None,
        "plan_enc_key_expired": (key_exp <= int(time.time())) if key_exp else True,
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
    ap = argparse.ArgumentParser(description="云端适配数据客户端")
    ap.add_argument("--status", action="store_true", help="查看适配数据来源、完整性状态与同步情况")
    ap.add_argument("--sync", action="store_true", help="用已激活的码重新拉一次适配数据（强制核对）")
    ap.add_argument("--verify", action="store_true", help="只做本地数据核对，不联网")
    ap.add_argument("--accept-endpoint", metavar="URL",
                    help="显式确认切换到新的服务地址（会记录到本机，便于回溯）")
    args = ap.parse_args()

    if args.accept_endpoint:
        url = args.accept_endpoint.strip().rstrip("/")
        try:
            u = _parse_endpoint(url)
            pinned = ((trust_anchor().get("endpoint") or {}).get("host") or "")
        except Exception as e:
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
            print("FAIL：没有可用于运行的适配数据（缓存与随包包均未通过校验）")
            return 1
        print(f"PASS：正在使用的适配数据来自 {source}，摘要 {report['plan_digest']}")
        return 0

    print(json.dumps(plan_status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
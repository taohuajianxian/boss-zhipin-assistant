#!/usr/bin/env python3
# -*- coding: utf-8 -*-























from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import cloud_client as cc

GRACE_HOURS = 24
DEFAULT_FREE_LIMIT = 150




ONLINE_CHECK_HOURS = 72
ONLINE_FAIL_LIMIT_HOURS = 168






def load_license() -> dict:
    return cc._read_json(cc.LICENSE_FILE, {}) or {}


def load_usage() -> dict:
    usage = cc._read_json(cc.USAGE_FILE, {}) or {}
    usage.setdefault("by_date", {})
    usage.setdefault("total", 0)
    return usage


def save_usage(usage: dict):
    cc._write_json(cc.USAGE_FILE, usage)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def free_limit() -> int:

    try:
        return int(cc.limits().get("fatal") or DEFAULT_FREE_LIMIT)
    except (TypeError, ValueError):
        return DEFAULT_FREE_LIMIT


def _hours_after_expiry(expires_at: str):
    try:
        return (datetime.now() - datetime.fromisoformat(expires_at)).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def _days_left(expires_at: str):
    try:
        return (datetime.fromisoformat(expires_at) - datetime.now()).days
    except (ValueError, TypeError):
        return None


def _hours_since(iso: str):
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def _safe_endpoint() -> str:

    try:
        return cc.endpoint() or ""
    except Exception:
        return ""


def refresh_license(license_data: dict):







    code = license_data.get("code")
    device = cc.device_id()
    status, obj = cc.verify(code, device)
    if status == 0:
        return None, (obj or {}).get("error", "网络不可达"), license_data
    if status != 200 or not isinstance(obj, dict) or not obj.get("ok"):
        reason = (obj or {}).get("error") or (obj or {}).get("reason") or f"HTTP {status}"
        return False, reason, license_data
    lic = obj.get("license") or {}
    merged = {
        **license_data,
        **lic,
        "device": device,
        "activated_at": license_data.get("activated_at") or lic.get("activated_at"),
        "last_ok_at": datetime.now().isoformat(timespec="seconds"),
    }
    cc._write_json(cc.LICENSE_FILE, merged)
    return True, "", merged


def status_snapshot(start_free: bool = False) -> dict:

    license_data = load_license()
    usage = load_usage()
    plan, source, _ = cc.load_plan()
    sync = cc.sync_status()
    limit = free_limit()

    first = usage.get("first_run_date")
    if not first and start_free:
        first = today()
        usage["first_run_date"] = first
        save_usage(usage)
    effective_first = first or today()

    used_today = int(usage.get("by_date", {}).get(today(), 0))
    snap = {
        "allowed": False,
        "mode": "blocked",
        "title": "",
        "detail": "",
        "days_left": None,
        "expires_at": license_data.get("expires_at"),
        "code": license_data.get("code"),
        "type": license_data.get("type"),
        "device": cc.device_id(),
        "free_first_day": effective_first,
        "free_used_today": used_today,
        "free_limit": limit,
        "free_left": max(0, limit - used_today),
        "total_delivered_recorded": usage.get("total", 0),
        "plan_version": (plan or {}).get("plan_version"),
        "plan_source": source or "none",
        "endpoint": _safe_endpoint(),
        "last_sync_at": sync.get("last_sync_at"),
        "last_ok_at": license_data.get("last_ok_at"),
        "verify_note": "",
        "degraded": sync.get("degraded", False),
        "wechat_id": str(cc.config().get("wechat_id") or "").strip(),
    }

    expiry = license_data.get("expires_at")
    if license_data.get("code"):

        owner = license_data.get("device")
        if owner and owner != snap["device"]:
            snap.update({
                "allowed": False, "mode": "wrong_device",
                "title": "授权文件不属于这台电脑",
                "detail": "直接拷别人的授权文件是没法用的。用你自己买的码执行 "
                          "--activate，换电脑时找作者说明一下就能改绑。",
            })
            return snap


        sig_ok = cc.verify_license_sig(license_data)
        if sig_ok is False:
            snap.update({
                "allowed": False, "mode": "tampered",
                "title": "授权文件被改动过",
                "detail": "校验没通过，请用你自己买的码重新执行 --activate。",
            })
            return snap


        last_ok = license_data.get("last_ok_at") or license_data.get("activated_at")
        idle = _hours_since(last_ok) if last_ok else None
        must_check = sig_ok is None or idle is None or idle >= ONLINE_CHECK_HOURS
        if must_check:
            ok, reason, fresh = refresh_license(license_data)
            if ok:
                license_data = fresh
                expiry = license_data.get("expires_at")
                snap["expires_at"] = expiry
                snap["code"] = license_data.get("code")
                snap["type"] = license_data.get("type")
            elif ok is False and "到期" not in reason:
                snap.update({
                    "allowed": False, "mode": "revoked",
                    "title": "激活码已失效",
                    "detail": f"{reason}。找作者续一张新码，再用 --activate 激活即可。",
                })
                return snap
            elif ok is False:
                snap["verify_note"] = "授权已到期，本地 24h 宽限内仍可用"
            else:
                if idle is not None and idle >= ONLINE_FAIL_LIMIT_HOURS:
                    snap.update({
                        "allowed": False, "mode": "offline_too_long",
                        "title": "太久没能联网核验授权",
                        "detail": f"已经 {int(idle // 24)} 天连不上作者服务器（{reason}）。"
                                  "连一次网就会自动恢复，授权本身不作废。",
                    })
                    return snap
                snap["verify_note"] = f"暂未联网核验（{reason}），授权按本地文件继续有效"

        if not expiry:
            left = None
            hours_after = -1
        else:
            left = _days_left(expiry)
            hours_after = _hours_after_expiry(expiry)
            if hours_after is None:
                hours_after = 999

        if hours_after <= 0:
            snap.update({
                "allowed": True, "mode": "licensed",
                "title": "长期有效" if not expiry else f"已激活 · 剩 {max(0, left or 0)} 天",
                "detail": "授权文件已落盘，离线也能跑；新版网页结构会自动同步。",
                "days_left": None if not expiry else max(0, left or 0),
            })
            return snap
        if hours_after <= GRACE_HOURS:
            snap.update({
                "allowed": True, "mode": "grace",
                "title": f"授权刚到期 · {GRACE_HOURS}h 宽限中",
                "detail": "还可以继续用，尽快找作者续一张新码。",
                "days_left": 0,
            })
            return snap
        snap.update({
            "allowed": False, "mode": "expired",
            "title": "授权已到期",
            "detail": "找作者续一张新码，再用 --activate 激活即可，历史投递记录不受影响。",
            "days_left": 0,
        })
        return snap


    free_today = False
    free_reason = ""
    status, obj = cc.free_signin(snap["device"])
    if status == 200 and isinstance(obj, dict) and obj.get("free_today"):
        free_today = True
        if obj.get("first_date"):
            snap["free_first_day"] = obj["first_date"]
        if start_free and not usage.get("first_run_date"):
            usage["first_run_date"] = today()
            save_usage(usage)
    elif status == 0:




        snap.update({
            "allowed": False, "mode": "free_unverified",
            "title": "免费体验需要联网核验",
            "detail": "暂时连不上作者服务器，免费资格没法确认。联网后重新运行即可；"
                      "或者找作者要一张激活码，激活后有离线宽限，不必每次联网。",
            "verify_note": "离线，免费资格未联网核验",
        })
        return snap
    else:
        free_reason = (obj or {}).get("reason") or "免费体验已结束"

    if free_today:
        if used_today < limit:
            snap.update({
                "allowed": True, "mode": "free",
                "title": f"免费体验 · 今天还可投 {limit - used_today} 份",
                "detail": "首次体验当天可完整投满一天上限，跑完会给你联系方式。",
            })
        else:
            snap.update({
                "allowed": False, "mode": "free_exhausted",
                "title": "今天的免费额度已用完",
                "detail": "想继续用就找作者要一张激活码，随喜，不方便的话说一声直接送。",
            })
        return snap

    snap.update({
        "allowed": False, "mode": "blocked",
        "title": "免费体验已结束",
        "detail": f"{free_reason}。找作者要一张激活码（随喜，经济困难直接说，免费给你），"
                  "再用 --activate 激活。",
    })
    return snap






def gate_at_start() -> dict:

    return status_snapshot(start_free=True)


def record_delivery(count: int = 1):

    usage = load_usage()
    if not usage.get("first_run_date"):
        usage["first_run_date"] = today()
    key = today()
    usage["by_date"][key] = int(usage["by_date"].get(key, 0)) + int(count)
    usage["total"] = int(usage.get("total", 0)) + int(count)
    save_usage(usage)
    return usage


def activation_hint(snap: dict) -> str:

    if snap["mode"] in ("licensed", "grace"):
        return "授权正常，新版页面结构会自动同步。有问题随时找我。"
    if snap["mode"] == "free":
        return "今天先免费用着，跑完觉得省时间，加个微信交个朋友。"
    return "加个微信说一声，我给你发一张新的激活码；不方便的话直接讲，免费送你。"






TYPE_LABEL = {
    "gift": "福利码", "casual": "随喜码",
    "monthly": "月卡", "lifetime": "长期码",
}


def activate(code: str) -> int:
    code = (code or "").strip().upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z2-9]{4,8}(?:-[A-Z2-9]{4,8}){0,4}", code):
        print("激活码格式不对，应该长这样：ABCD-1234-EFGH")
        return 1
    try:
        ep = cc.endpoint()
    except Exception as e:
        print(f"服务地址未通过安全校验：{e}")
        print("确认这就是你要连接的地址后，执行："
              f"python scripts/cloud_client.py --accept-endpoint <URL>")
        return 1
    if not ep:
        print("还没配置服务地址：设置 BOSS_LICENSE_ENDPOINT，或填 scripts/cloud_config.json 的 endpoint。")
        return 1

    device = cc.device_id()
    status, obj = cc.verify(code, device)
    if status != 200 or not isinstance(obj, dict) or not obj.get("ok"):
        reason = (obj or {}).get("error") or (obj or {}).get("reason") or f"HTTP {status}"
        print(f"激活失败：{reason}")
        print("确认码没抄错；如果换过电脑，找作者说明一下。")
        return 1

    lic = obj["license"]
    stamp = datetime.now().isoformat(timespec="seconds")
    cc._write_json(cc.LICENSE_FILE, {
        **lic,
        "device": device,
        "activated_at": stamp,
        "last_ok_at": stamp,
    })

    synced = cc.sync_plan(code, device)
    expiry = lic.get("expires_at") or "长期"
    print(f"激活成功 | {TYPE_LABEL.get(lic.get('type'), lic.get('type'))} | 到期 {expiry[:10] if expiry != '长期' else '长期'}"
          f" | 剩余 {lic.get('days_left') if lic.get('days_left') is not None else '长期'}"
          f" | 适配数据 {lic.get('plan_version') or '未下发'}")
    if synced.get("ok"):
        print(f"云端适配数据已同步：{synced['plan_version']}（{synced['selectors']} 个选择器）")
    else:
        print(f"云端适配数据暂未同步：{synced.get('reason')}；先用随包附带的版本，不影响使用。")
    print("已落盘授权文件，之后每天直接跑，不用再输码、也不必每次联网。")
    return 0


def print_status(snap: dict, as_json: bool = False):
    if as_json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return
    print(f"授权状态  : {snap['title']}")
    print(f"模式      : {snap['mode']}")
    print(f"说明      : {snap['detail']}")
    print(f"免费额度  : 今天已投 {snap['free_used_today']} / {snap['free_limit']} 份（首日 {snap['free_first_day']}）")
    print(f"激活码    : {snap['code'] or '（无）'}  类型 {snap['type'] or '—'}  到期 {snap['expires_at'] or '—'}")
    print(f"适配数据    : {snap['plan_version'] or '未加载'}（来源 {snap['plan_source']}）")
    print(f"服务地址  : {snap['endpoint'] or '（未配置，离线免费体验）'}")
    print(f"设备指纹  : {snap['device']}  （只上传哈希，不含 MAC / 用户名）")
    if snap["last_sync_at"]:
        print(f"最近同步  : {snap['last_sync_at']}" + ("  ⚠ 已超 24h，建议 --sync" if snap["degraded"] else ""))
    if snap.get("last_ok_at"):
        print(f"授权核验  : {snap['last_ok_at']}（每 {ONLINE_CHECK_HOURS}h 自动联网核对一次）")
    if snap.get("verify_note"):
        print(f"核验提示  : {snap['verify_note']}")
    if snap["wechat_id"]:
        print(f"联系方式  : {snap['wechat_id']}")


def main():
    ap = argparse.ArgumentParser(description="激活码与免费额度（CLI 即权威）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--activate", metavar="CODE", help="激活一张码")
    g.add_argument("--status", action="store_true", help="查看授权状态")
    g.add_argument("--check", action="store_true", help="门禁检查，退出码 0=放行 / 1=拦截")
    g.add_argument("--record", action="store_true", help="记录一次成功投递")
    g.add_argument("--sync", action="store_true", help="重新拉取云端适配数据")
    ap.add_argument("--count", type=int, default=1, help="--record 的投递次数，默认 1")
    ap.add_argument("--json", action="store_true", help="--status / --check 输出 JSON")
    args = ap.parse_args()

    if args.activate:
        return activate(args.activate)
    if args.record:
        usage = record_delivery(args.count)
        print(f"已记录 {args.count} 次投递 | 今天 {usage['by_date'].get(today(), 0)} 份 | 累计 {usage['total']} 份")
        return 0
    if args.sync:
        result = cc.sync_plan()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    snap = status_snapshot(start_free=args.check)
    if args.check:
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2))
        else:
            print_status(snap)
            if not snap["allowed"]:
                print("\n[门禁] 未放行。运行期不会被打断，本步 init 已停止。")
                print("[门禁] 拿到激活码后执行：python scripts/entitlement.py --activate <CODE>")
        return 0 if snap["allowed"] else 1

    print_status(snap, as_json=args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
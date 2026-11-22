#!/usr/bin/env python3























import json
try:
    from scripts.atomic_io import atomic_dump_json
except ImportError:
    from atomic_io import atomic_dump_json

import os

WORK_DIR = os.environ.get("BOSS_WORK_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if os.path.basename(WORK_DIR) == "scripts":
    WORK_DIR = os.path.dirname(WORK_DIR)

SERIAL_FILE = "serial_progress.json"
STREAM_FILE = "stream_progress.json"
UNIFIED_FILE = "delivery_unified.json"
SURFACE_FILE = "surface_cache.json"

_STATUS_RANK = {"success": 5, "failed": 4, "skipped": 3, "rejected": 2, "unknown": 1}

_SUCCESS_WORDS = ("投递成功", "已发送招呼", "success", "delivered")


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _resolve(work_dir=None, name=""):
    wd = work_dir or WORK_DIR
    return os.path.join(wd, name)


def _norm_status(deliver_status, deliver_detail="", decision=""):

    ds = (deliver_status or "").strip().lower()
    detail = deliver_detail or ""
    if ds in ("success", "delivered", "ok"):
        return "success"
    if ds in ("fail", "failed", "error"):
        return "failed"
    if ds in ("skip", "skipped", "already", "dup", "duplicate"):

        if "规则排除" in detail or decision == "REJECT":
            return "rejected"
        return "skipped"
    if any(w in detail for w in _SUCCESS_WORDS[:2]):
        return "success"
    if decision == "REJECT":
        return "rejected"
    if ds:
        return ds
    return "unknown"


def _merge_record(store, job_id, status, payload):

    if not job_id:
        job_id = f"_anon_{len(store)}"
    cur = store.get(job_id)
    if cur is None:
        store[job_id] = payload
        return
    if _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(cur.get("status", "unknown"), 0):
        merged = dict(cur)
        merged.update({k: v for k, v in payload.items() if v not in (None, "", 0)})
        store[job_id] = merged
    else:
        for k, v in payload.items():
            if not cur.get(k) and v:
                cur[k] = v


def compute(work_dir=None, surface_patch=True):










    serial = _load_json(_resolve(work_dir, SERIAL_FILE), {}) or {}
    stream = _load_json(_resolve(work_dir, STREAM_FILE), {}) or {}
    surface = _load_json(_resolve(work_dir, SURFACE_FILE), []) or []


    surf_by_id = {}
    if surface_patch:
        for j in surface:
            jid = j.get("jobId")
            if jid:
                surf_by_id[jid] = j

    store = {}
    conflicts = 0


    raw_stream = 0
    for j in stream.get("delivered", []) or []:
        raw_stream += 1
        jid = j.get("jobId") or ""
        status = "success"
        rec = {
            "jobId": jid,
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "salary": j.get("salary", ""),
            "industry": j.get("industry", ""),
            "status": status,
            "detail": j.get("result", "success"),
            "source": "stream",
        }
        _merge_record(store, jid, status, rec)

    for j in stream.get("failed", []) or []:
        raw_stream += 1
        jid = j.get("jobId") or ""
        rec = {
            "jobId": jid,
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "salary": j.get("salary", ""),
            "industry": j.get("industry", ""),
            "status": "failed",
            "detail": j.get("reason", ""),
            "source": "stream",
        }
        _merge_record(store, jid, "failed", rec)


    raw_serial = 0
    session_ids = set()
    for r in serial.get("results", []) or []:
        raw_serial += 1
        jid = r.get("jobId") or ""
        status = _norm_status(r.get("deliver_status"), r.get("deliver_detail", ""), r.get("decision", ""))
        rec = {
            "jobId": jid,
            "title": r.get("title", ""),
            "company": "",
            "salary": "",
            "industry": "",
            "status": status,
            "detail": r.get("deliver_detail", "") or r.get("reason", ""),
            "decision": r.get("decision", ""),
            "index": r.get("index"),
            "page_order": r.get("page_order"),
            "reason": r.get("reason", ""),
            "source": "serial",
        }
        if jid in store:
            if store[jid].get("status") != status:
                conflicts += 1
            store[jid]["source"] = "serial+stream"
        _merge_record(store, jid, status, rec)
        session_ids.add(jid)


    for jid, rec in store.items():
        s = surf_by_id.get(jid)
        if not s:
            continue
        for k in ("company", "salary", "industry"):
            if not rec.get(k):
                rec[k] = s.get(k, "")

    def _bucket(records):
        out = {"success": [], "failed": [], "skipped": [], "rejected": [], "unknown": []}
        for rec in records:
            out.setdefault(rec.get("status", "unknown"), []).append(rec)
        return out

    all_records = list(store.values())
    cum = _bucket(all_records)
    ses = _bucket([r for r in all_records if r.get("jobId") in session_ids])

    def _counts(b):
        return {
            "success": len(b["success"]),
            "failed": len(b["failed"]),
            "skipped": len(b["skipped"]),
            "rejected": len(b["rejected"]),
            "unknown": len(b["unknown"]),
            "total": sum(len(v) for v in b.values()),
        }

    return {
        "cumulative": _counts(cum),
        "session": _counts(ses),
        "session_available": bool(serial.get("results")),
        "delivered": sorted(cum["success"], key=lambda r: r.get("index") or 0),
        "failed": cum["failed"],
        "rejected": cum["rejected"],
        "sources": {
            "stream_raw": raw_stream,
            "serial_raw": raw_serial,
            "dedup_total": len(all_records),
            "stream_only": len([r for r in all_records if r.get("source") == "stream"]),
        },
        "conflicts": conflicts,
    }


def summary_line(stats):

    c = stats["cumulative"]
    s = stats["session"]
    base = f"累计投递成功 {c['success']} 份 / 失败 {c['failed']} 份"
    if stats.get("session_available"):
        base += f"（本次会话成功 {s['success']}、排除 {s['rejected']}）"
    return base


def save_unified(stats, work_dir=None):
    path = _resolve(work_dir, UNIFIED_FILE)
    payload = dict(stats)

    payload.pop("delivered", None)
    payload.pop("failed", None)
    payload.pop("rejected", None)
    payload["delivered"] = stats.get("delivered", [])
    payload["failed"] = stats.get("failed", [])
    payload["rejected"] = stats.get("rejected", [])
    try:
        atomic_dump_json(path, payload, indent=2)
    except Exception:
        return None
    return path


def report_payload(work_dir=None):

    stats = compute(work_dir)
    return stats["delivered"], stats["failed"], stats


if __name__ == "__main__":
    import sys
    wd = sys.argv[1] if len(sys.argv) > 1 else None
    st = compute(wd)
    print(summary_line(st))
    print(json.dumps({k: v for k, v in st.items() if k not in ("delivered", "failed", "rejected")},
                     ensure_ascii=False, indent=2))
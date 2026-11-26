# -*- coding: utf-8 -*-






















import atexit
import json
import os
import sys
import time

LOCK_NAME = "run.lock"
_STILL_ACTIVE = 259



_PROC_START_TOLERANCE = 2.0




EMPTY_LOCK_GRACE = 10.0
_READ_RETRIES = 3
_READ_RETRY_SLEEP = 0.06


def _work_dir():

    from scripts.profile_loader import WORK_DIR
    return WORK_DIR


def lock_path(path=None):
    return path or os.path.join(_work_dir(), LOCK_NAME)


def _pid_alive(pid):

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:

        return k32.GetLastError() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong(0)
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _proc_start_epoch(pid):

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation, exit_t = wintypes.FILETIME(), wintypes.FILETIME()
        kernel, user = wintypes.FILETIME(), wintypes.FILETIME()
        ok = k32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                 ctypes.byref(kernel), ctypes.byref(user))
        if not ok:
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ticks / 1e7 - 11644473600.0
    finally:
        k32.CloseHandle(handle)


def _holder_alive(holder):






    pid = holder.get("pid")
    if not _pid_alive(pid):
        return False
    rec = holder.get("proc_start")
    if not rec:
        return True
    try:
        rec = float(rec)
    except (TypeError, ValueError):
        return True
    now = _proc_start_epoch(pid)
    if now is None:
        return True
    return abs(now - rec) <= _PROC_START_TOLERANCE


def _read_holder(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def _age_seconds(path):

    try:
        return max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return None


def _write_exclusive(path, payload):

    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return True


def describe(holder):

    if not holder:
        return "未知进程"
    return "pid {0} · 步骤 {1} · 开始于 {2}".format(
        holder.get("pid", "?"), holder.get("step", "?"), holder.get("started", "?"))


class RunLock(object):


    def __init__(self, path, payload):
        self.path = path
        self.payload = payload
        self._released = False
        atexit.register(self.release)

    def release(self):
        if self._released:
            return
        self._released = True

        holder = _read_holder(self.path)
        if holder.get("token") and holder.get("token") != self.payload.get("token"):
            return
        try:
            os.remove(self.path)
        except OSError:
            pass


def acquire(step, path=None, note=""):






    target = lock_path(path)
    payload = {
        "pid": os.getpid(),
        "step": step,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,

        "proc_start": _proc_start_epoch(os.getpid()),
        "token": "{0}-{1}".format(os.getpid(), int(time.time() * 1000)),
    }
    if _write_exclusive(target, payload):
        return RunLock(target, payload), None

    holder = _read_holder(target)
    if not holder.get("pid"):

        for _ in range(_READ_RETRIES):
            time.sleep(_READ_RETRY_SLEEP)
            holder = _read_holder(target)
            if holder.get("pid"):
                break
    if holder.get("pid"):
        if _holder_alive(holder):
            return None, holder
    else:
        age = _age_seconds(target)
        if age is None:

            if _write_exclusive(target, payload):
                return RunLock(target, payload), None
            return None, _read_holder(target)
        if age < EMPTY_LOCK_GRACE:

            return None, {"pid": "?", "step": "启动中（锁文件已建、内容尚未写入）",
                          "started": "刚刚"}


    try:
        os.remove(target)
    except OSError:
        pass
    if _write_exclusive(target, payload):
        print("[锁] 接管上一轮残留锁（原持有者已退出）: {0}".format(describe(holder)))
        return RunLock(target, payload), None
    return None, _read_holder(target)


def main(argv=None):

    if __package__ in (None, ""):

        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
    argv = list(sys.argv[1:] if argv is None else argv)
    target = lock_path()
    if "--clear" in argv:
        holder = _read_holder(target)


        if holder and _holder_alive(holder):
            if "--force" not in argv:
                print("[锁] 拒绝清理：持锁进程仍在运行 -> {0}".format(describe(holder)))
                print("      确认无人运行、或已确认该进程是死锁假活，才可加 --force 强清；")
                print("      否则两个进程会同时写台账，进度与已投记录可能互相覆盖。")
                return 3
            print("[锁] 警告：--force 强清运行中的锁（{0}），并发写台账风险由操作者承担".format(
                describe(holder)))
        try:
            os.remove(target)
            print("[锁] 已删除 {0}（原持有者: {1}）".format(target, describe(holder)))
        except OSError:
            print("[锁] 本来就没人持锁")
        return 0
    holder = _read_holder(target)
    if not holder:
        print("[锁] 空闲：{0}".format(target))
        return 0
    alive = _holder_alive(holder)
    print("[锁] 被占用: {0}".format(describe(holder)))
    if alive:
        print("      该进程仍在运行")
    elif holder.get("proc_start") and _pid_alive(holder.get("pid")):
        print("      该 pid 已被系统复用给别的进程（原持有者已退出，下次动作类步骤会自动接管）")
    else:
        print("      该进程已退出（残留锁，下次动作类步骤会自动接管）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
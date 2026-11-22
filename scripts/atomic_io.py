# -*- coding: utf-8 -*-














import json
import os


def atomic_write_text(path, text, encoding="utf-8"):




    path = os.fspath(path)
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def atomic_dump_json(path, data, indent=2):

    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=indent))


def atomic_load_json(path, default=None):

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
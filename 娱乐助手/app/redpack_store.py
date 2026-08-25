"""红包存储: 按群独立存储 (JSON 文件持久化)。

数据格式: {gid: {pack_id: data}}
群上下文: 与 points.py 共用 contextvars, 由 main.py 命令入口统一设置。
旧版全局数据自动迁移到 "__legacy__" 群。
"""

import contextvars
import json
import os
import threading
from pathlib import Path

from core.base.logger import PLUGIN, get_logger

log = get_logger(PLUGIN, "娱乐助手红包")

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_DATA_FILE = _PLUGIN_DIR / "data" / "redpacks.json"

_lock = threading.RLock()
_cache = None

_current_gid: "contextvars.ContextVar[str]" = contextvars.ContextVar("ent_gid", default="")


def set_group(gid):
    """与 points.set_group 一致, 设置当前命令所属群。"""
    _current_gid.set(str(gid or ""))


def _gid() -> str:
    return _current_gid.get() or "_no_group"


def _migrate(raw: dict) -> dict:
    """旧格式 {pack_id: data} → 新格式 {gid: {pack_id: data}}。

    检测: 顶层任一 value 是红包字典 (不含 'points', 且通常含 total/remaining)。
    """
    if not raw:
        return {}
    legacy = False
    for k, v in raw.items():
        if isinstance(v, dict) and ("total" in v or "remaining" in v or "sender_id" in v):
            legacy = True
            break
    if not legacy:
        return raw
    new: dict = {"__legacy__": dict(raw)}
    return new


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if _DATA_FILE.exists():
            try:
                with open(_DATA_FILE, "r", encoding="utf-8") as f:
                    _cache = _migrate(json.load(f))
                if "__legacy__" in (_cache or {}):
                    _save_locked()
            except Exception as e:
                log.warning("读取红包数据失败: %s", e)
                _cache = {}
        else:
            _cache = {}
        return _cache


def _save_locked():
    if _cache is None:
        return
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _DATA_FILE)


def _save():
    with _lock:
        _save_locked()


def _gid_store(gid=None):
    gid = str(gid if gid is not None else _gid())
    if not gid or gid == "_meta":
        gid = "_no_group"
    data = _load()
    store = data.get(gid)
    if store is None:
        if gid != "__legacy__" and isinstance(data.get("__legacy__"), dict):
            store = data.pop("__legacy__")
            data[gid] = store
        else:
            store = {}
            data[gid] = store
        _save()
    return store


def save(pack_id: str, data: dict, gid=None):
    with _lock:
        store = _gid_store(gid)
        store[pack_id] = data
        _cache = _load()
        _save()


def load(pack_id: str, gid=None) -> dict | None:
    return _gid_store(gid).get(pack_id)


def list_all(gid=None) -> dict:
    return dict(_gid_store(gid))


def list_groups() -> list:
    data = _load()
    return [str(k) for k in data if k != "_meta"]


def delete(pack_id: str, gid=None):
    with _lock:
        store = _gid_store(gid)
        store.pop(pack_id, None)
        _save()

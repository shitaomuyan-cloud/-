"""红包存储: JSON 文件持久化。"""

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


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if _DATA_FILE.exists():
            try:
                with open(_DATA_FILE, "r", encoding="utf-8") as f:
                    _cache = json.load(f)
            except Exception as e:
                log.warning("读取红包数据失败: %s", e)
                _cache = {}
        else:
            _cache = {}
        return _cache


def _save():
    with _lock:
        if _cache is None:
            return
        _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DATA_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _DATA_FILE)


def save(pack_id: str, data: dict):
    with _lock:
        store = _load()
        store[pack_id] = data
        _cache = store
        _save()


def load(pack_id: str) -> dict | None:
    return _load().get(pack_id)


def list_all() -> dict:
    return dict(_load())


def delete(pack_id: str):
    with _lock:
        store = _load()
        store.pop(pack_id, None)
        _cache = store
        _save()
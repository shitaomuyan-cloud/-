"""群独立规则配置。

数据文件 data/config.json 结构: {gid: {规则键: 值}, "_base": {全局默认}}
- 每个群有自己独立的规则数值 (签到区间/抽奖概率/红包成本等)
- _base 为默认模板, 新群初始配置 = _base
- 命令执行时通过 contextvars 获取当前群配置 (由 main.py 命令入口设置)
"""

import contextvars
import json
import threading
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _PLUGIN_DIR / "data" / "config.json"

_lock = threading.RLock()
_cache = None

_cur_cfg = contextvars.ContextVar("ent_cfg", default=None)

DEFAULTS = {
    "sign_lo": 50,
    "sign_hi": 200,
    "lottery_cost": 20,
    "lottery_lo": 0,
    "lottery_hi": 150,
    "lottery_win_rate": 0.6,
    "robbery_lo": 0,
    "robbery_hi": 150,
    "robbery_rate": 0.6,
    "mute_cost": 100,
    "revoke_cost": 50,
    "draw_cost": 50,
    "armor_cost": 100,
}


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if _CONFIG_FILE.exists():
            try:
                raw = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            _cache = _migrate(raw)
        else:
            _cache = {}
        return _cache


def _migrate(raw: dict) -> dict:
    """旧全局格式 {sign_lo: ...} → 新格式 {"_base": {...}}。"""
    if not raw or "_base" in raw:
        return raw
    # 顶层直接是规则键 (旧格式)
    if any(k in raw for k in ("sign_lo", "sign_hi", "lottery_cost")):
        new = {"_base": dict(raw)}
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
        return new
    return raw


def _save_locked():
    if _cache is None:
        return
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(_cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _save():
    with _lock:
        _save_locked()


def group_config(gid) -> dict:
    """返回某群的完整配置 (缺失键用默认值补齐)。"""
    gid = str(gid or "")
    data = _load()
    base = dict(data.get("_base") or DEFAULTS)
    cfg = dict(base)
    cfg.update(data.get(gid) or {})
    return cfg


def save_group_config(gid, cfg: dict):
    """保存某群配置 (完整覆盖该群配置)。"""
    gid = str(gid or "")
    if not gid or gid == "_base":
        return
    with _lock:
        data = _load()
        data[gid] = dict(cfg)
        _save_locked()


def list_configured_groups() -> list:
    """有独立配置的群 id 列表。"""
    data = _load()
    return [str(k) for k in data if k != "_base"]


def get_current() -> dict:
    """当前命令所在群的配置 (contextvars, 缺省返回默认)。"""
    cfg = _cur_cfg.get()
    return cfg if cfg is not None else DEFAULTS


def set_current(cfg: dict):
    _cur_cfg.set(cfg)

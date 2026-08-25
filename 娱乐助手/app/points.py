"""积分核心逻辑: 按群独立存储 (JSON 文件持久化)。

数据格式: {gid: {uid: {points, robbed, armor, ...}}, "_meta": {...}}
群上下文: 通过 contextvars 传递, 由 main.py 在每个命令入口设置 p.set_group(gid)。
旧版全局数据 ( {uid: user} ) 会自动迁移到 "__legacy__" 群, 并由第一个使用插件的群继承。
"""

import contextvars
import json
import os
import random
import threading
import time
from pathlib import Path

from core.base.logger import PLUGIN, get_logger

log = get_logger(PLUGIN, "娱乐助手")

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_DATA_FILE = _PLUGIN_DIR / "data" / "points.json"

_lock = threading.RLock()
_cache = None

# 群上下文 (asyncio 协程级隔离)
_current_gid: "contextvars.ContextVar[str]" = contextvars.ContextVar("ent_gid", default="")


def set_group(gid):
    """设置当前命令所属的群 (每个 handler 入口调用一次)。"""
    _current_gid.set(str(gid or ""))


def _gid() -> str:
    gid = _current_gid.get()
    return gid or "_no_group"


def _migrate(raw: dict) -> dict:
    """旧格式 {uid: user} → 新格式 {gid: {uid: user}}。

    检测: 顶层任一 value 是包含 'points' 的用户字典 → 旧格式。
    迁移结果放入 "__legacy__" 群, 首群使用时继承。
    """
    if not raw:
        return {}
    legacy = False
    for k, v in raw.items():
        if k == "_meta":
            continue
        if isinstance(v, dict) and "points" in v:
            legacy = True
            break
    if not legacy:
        return raw
    new: dict = {}
    for k, v in raw.items():
        if k == "_meta":
            new["_meta"] = v
        else:
            new.setdefault("__legacy__", {})[k] = v
    if not new.get("__legacy__"):
        new.pop("__legacy__", None)
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
                # 若发生迁移, 立即落盘
                if "__legacy__" in (_cache or {}):
                    _save_locked()
            except Exception as e:  # noqa: BLE001
                log.warning("读取积分数据失败, 重建: %s", e)
                _cache = {}
        else:
            _cache = {}
        return _cache


def _save_locked():
    """在持锁状态下写盘。"""
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
    """返回该群的用户字典; 不存在时创建 (并继承 __legacy__ 旧数据)。"""
    gid = str(gid if gid is not None else _gid())
    if not gid or gid == "_meta":
        return {}
    data = _load()
    store = data.get(gid)
    if store is None:
        if gid != "__legacy__" and isinstance(data.get("__legacy__"), dict):
            # 首群继承旧全局数据
            store = data.pop("__legacy__")
            data[gid] = store
        else:
            store = {}
            data[gid] = store
        _save()
    return store


def _ensure(user_id, gid=None) -> dict:
    uid = str(user_id)
    store = _gid_store(gid)
    user = store.get(uid)
    if user is None:
        user = {"points": 0, "robbed": 0, "armor": 0, "last_sign": "", "nickname": "", "appid": "", "qq": "", "avatar": ""}
        store[uid] = user
        _save()
    else:
        # 兼容旧数据：补充 robbed 字段（通过抢劫持有的积分）
        if "robbed" not in user:
            user["robbed"] = 0
            _save()
    return user


def clean_nick(name) -> str:
    """清洗昵称：去掉不可见字符（U+3164 填充符、零宽字符、控制符），返回去除首尾空白的结果。"""
    import unicodedata

    s = str(name or "")
    s = s.replace("\u3164", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("C"))
    return s.strip()


def touch(user_id, nickname=None, appid=None, qq=None, avatar=None) -> dict:
    """记录用户资料 (昵称/机器人appid/QQ号/头像), 便于 Web 显示真实身份。"""
    nickname = clean_nick(nickname) if nickname else ""
    user = _ensure(user_id)
    changed = False
    if nickname and user.get("nickname") != nickname:
        user["nickname"] = nickname
        changed = True
    if appid and user.get("appid") != appid:
        user["appid"] = appid
        changed = True
    if qq and user.get("qq") != str(qq):
        user["qq"] = str(qq)
        changed = True
    if avatar and user.get("avatar") != str(avatar):
        user["avatar"] = str(avatar)
        changed = True
    if changed:
        _save()
    return user


def get_appid() -> str:
    """从所有用户记录中回退获取一个可用的 appid (Web 显示头像用)。"""
    data = _load()
    return str(data.get("_meta", {}).get("appid") or "")


def nick(user_id) -> str:
    return clean_nick(_ensure(user_id).get("nickname", ""))


def set_nickname(user_id, nickname: str):
    user = _ensure(user_id)
    user["nickname"] = nickname
    _save()


def get_points(user_id) -> int:
    return _ensure(user_id).get("points", 0)


def get_robbed(user_id) -> int:
    """当前通过抢劫持有的积分。"""
    return int(_ensure(user_id).get("robbed", 0) or 0)


def add_robbed(user_id, amount: int) -> int:
    """抢劫成功时累加'抢劫所得'。"""
    user = _ensure(user_id)
    user["robbed"] = max(0, int(user.get("robbed", 0)) + int(amount))
    _save()
    return user["robbed"]


def settle_mutual(initiator_id, target_id):
    """原子结算'同归于尽'：在单一持锁区间内读取双方余额并扣除，彻底杜绝并发不同步。

    抢劫已经实时改动过双方余额（先来后到自然成立），此处直接按'当前余额'结算：
    双方各扣 min(双方余额) —— 余额少者归零，余额多者保留差额。
    返回 (deducted, initiator_total, target_total, ok)。
    """
    with _lock:
        store = _gid_store()
        iu = store.get(str(initiator_id))
        tu = store.get(str(target_id))
        if not iu or not tu:
            return 0, 0, 0, False
        i_pts = int(iu.get("points", 0))
        t_pts = int(tu.get("points", 0))
        deduct = min(i_pts, t_pts)
        if deduct <= 0:
            return 0, i_pts, t_pts, False
        iu["points"] = i_pts - deduct
        tu["points"] = t_pts - deduct
        _save()
        return deduct, iu["points"], tu["points"], True


def set_points(user_id, points: int):
    _ensure(user_id)["points"] = max(0, int(points))
    _save()


def add_points(user_id, delta: int) -> int:
    user = _ensure(user_id)
    user["points"] = max(0, user.get("points", 0) + int(delta))
    _save()
    return user["points"]


def buy_armor(user_id) -> bool:
    """购买反甲: 扣 100 积分, 积分为购置数量。（兼容旧接口）"""
    user = _ensure(user_id)
    if user.get("points", 0) < 100:
        return False
    user["points"] -= 100
    user["armor"] = user.get("armor", 0) + 1
    _save()
    return True


def add_armor(user_id, amount: int = 1) -> int:
    """直接累加反甲数量，不扣积分（用于已扣分后的入账）。返回新数量。"""
    user = _ensure(user_id)
    user["armor"] = user.get("armor", 0) + int(amount)
    _save()
    return user["armor"]


def armor_count(user_id) -> int:
    return int(_ensure(user_id).get("armor", 0) or 0)


def has_armor(user_id) -> bool:
    return armor_count(user_id) > 0


def consume_armor(user_id) -> bool:
    user = _ensure(user_id)
    if user.get("armor", 0) <= 0:
        return False
    user["armor"] -= 1
    _save()
    return True


def remove_user(user_id) -> bool:
    """删除用户积分记录，若存在则移除并保存。返回是否删除了。"""
    global _cache
    with _lock:
        store = _gid_store()
        uid = str(user_id)
        if uid in store and str(uid) != "_meta":
            del store[uid]
            _save()
            return True
        return False


def set_qq(user_id, qq: str):
    """绑定/修改用户显示的 QQ 号 (openid 无法自动获取，需手动登记)。"""
    _ensure(user_id)["qq"] = str(qq) if qq else ""
    _save()


def get_qq(user_id) -> str:
    return str(_ensure(user_id).get("qq", "") or "")


def set_last_sign_date(user_id, date_str: str):
    _ensure(user_id)["last_sign"] = date_str
    _save()


def last_sign_date(user_id) -> str:
    return str(_ensure(user_id).get("last_sign", "") or "")


def today_sign_key() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d")


def list_groups() -> list:
    """返回所有有数据的群 id (用于 Web 面板群选择器)。"""
    data = _load()
    return [str(k) for k in data if k != "_meta"]


def group_user_count(gid: str) -> int:
    store = _load().get(str(gid)) or {}
    return len([k for k in store if k != "_meta"])


def all_users(gid=None) -> dict:
    return dict(_gid_store(gid))


def top_list(limit: int = 10, gid=None):
    store = _gid_store(gid)
    users = []
    for uid, user in store.items():
        if str(uid) in ("_meta",):
            continue
        users.append(
            {
                "id": str(uid),
                "nickname": user.get("nickname", ""),
                "points": user.get("points", 0),
                "armor": user.get("armor", 0),
            }
        )
    users.sort(key=lambda x: x["points"], reverse=True)
    return users[:limit]


def random_points(lo: int, hi: int) -> int:
    return random.randint(lo, hi)


# ---------- 每日限次 + 冷却 ----------

DAILY_LIMIT = 5
COOLDOWN_SECONDS = 30


def check_and_record_limit(user_id, key: str, daily: int = DAILY_LIMIT, cooldown: int = COOLDOWN_SECONDS):
    """原子检查并记录'每日次数 + 冷却间隔'。

    返回 (ok, reason, extra, warned)：
      - (True, "ok", 剩余次数, False)
      - (False, "cooldown", 剩余秒数, warned)  冷却中；warned=False 为首次提示(仅提示),
        warned=True 为再次触发(此时应执行禁言惩罚)
      - (False, "daily", 今日上限, False)     今日次数已用完
    """
    today = today_sign_key()
    with _lock:
        user = _ensure(user_id)
        limits = user.setdefault("limits", {})
        st = limits.get(key) or {}
        day = st.get("day") or ""
        count = int(st.get("count") or 0)
        if day != today:
            count = 0
        last = float(st.get("last") or 0)
        now = time.time()
        if last and now - last < cooldown:
            sec = int(cooldown - (now - last)) + 1
            warned = bool(st.get("warned"))
            if not warned:
                st["warned"] = True
                _save()
            return False, "cooldown", sec, warned
        if count >= daily:
            return False, "daily", daily, False
        # 正常通过: 重建状态(同时清除上次的警告标记)
        limits[key] = {"day": today, "last": now, "count": count + 1}
        _save()
        return True, "ok", daily - count - 1, False

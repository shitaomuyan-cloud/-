"""娱乐助手 Web 面板路由。"""

import asyncio
import json
from pathlib import Path

from aiohttp import web
from core.base.logger import get_logger, PLUGIN
from core.plugin.web_pages import register_route

from . import games as g
from . import points as p
from . import redpack_store as rs

log = get_logger(PLUGIN, "娱乐助手面板")

PREFIX = "/api/ext/funhelper"
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_WEB_DIR = _PLUGIN_DIR / "web"
_CONFIG_FILE = _PLUGIN_DIR / "data" / "config.json"
_ASSETS = {
    "panel.css": "text/css; charset=utf-8",
    "panel.js": "text/javascript; charset=utf-8",
}

_registered = []
RELOADED = True


def _default_config() -> dict:
    return {
        "sign_lo": g.SIGN_LO,
        "sign_hi": g.SIGN_HI,
        "lottery_cost": g.LOTTERY_COST,
        "lottery_lo": g.LOTTERY_LO,
        "lottery_hi": g.LOTTERY_HI,
        "lottery_win_rate": g.LOTTERY_WIN_RATE,
        "robbery_lo": g.ROBBERY_LO,
        "robbery_hi": g.ROBBERY_HI,
        "robbery_rate": g.ROBBERY_SUCCESS_RATE,
        "mute_cost": g.MUTE_COST,
        "revoke_cost": g.REVOKE_COST,
        "draw_cost": g.DRAW_COST,
        "armor_cost": g.ARMOR_COST,
    }


def _sync_config(cfg: dict):
    g.SIGN_LO = int(cfg.get("sign_lo", g.SIGN_LO))
    g.SIGN_HI = int(cfg.get("sign_hi", g.SIGN_HI))
    g.LOTTERY_COST = int(cfg.get("lottery_cost", g.LOTTERY_COST))
    g.LOTTERY_LO = int(cfg.get("lottery_lo", g.LOTTERY_LO))
    g.LOTTERY_HI = int(cfg.get("lottery_hi", g.LOTTERY_HI))
    g.LOTTERY_WIN_RATE = float(cfg.get("lottery_win_rate", g.LOTTERY_WIN_RATE))
    g.ROBBERY_LO = int(cfg.get("robbery_lo", g.ROBBERY_LO))
    g.ROBBERY_HI = int(cfg.get("robbery_hi", g.ROBBERY_HI))
    g.ROBBERY_SUCCESS_RATE = float(cfg.get("robbery_rate", g.ROBBERY_SUCCESS_RATE))
    g.MUTE_COST = int(cfg.get("mute_cost", g.MUTE_COST))
    g.REVOKE_COST = int(cfg.get("revoke_cost", g.REVOKE_COST))
    g.DRAW_COST = int(cfg.get("draw_cost", g.DRAW_COST))
    g.ARMOR_COST = int(cfg.get("armor_cost", g.ARMOR_COST))


def load_config() -> dict:
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = _default_config()
            cfg.update(data)
            _sync_config(cfg)
            return cfg
        except Exception:
            pass
    cfg = _default_config()
    _sync_config(cfg)
    return cfg


def save_config(cfg: dict):
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    _sync_config(cfg)


async def _asset(request):
    filename = request.path.rsplit("/", 1)[-1]
    content_type = _ASSETS.get(filename)
    if not content_type:
        raise web.HTTPNotFound()
    path = _WEB_DIR / filename
    if not await asyncio.to_thread(path.is_file):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-cache", "Content-Type": content_type})


def _resolve_gid(request):
    """从 query 中取群 id 并设置群上下文; 未传时使用默认空群。"""
    gid = str(request.query.get("gid", "") or "").strip()
    p.set_group(gid)
    return gid


def register_routes():
    global _registered
    if _registered:
        return
    routes = [
        ("GET", "users", _api_users, True),
        ("GET", "user", _api_user, True),
        ("POST", "points", _api_set_points, True),
        ("POST", "points_add", _api_add_points, True),
        ("POST", "points_sub", _api_sub_points, True),
        ("POST", "delete", _api_delete_user, True),
        ("POST", "qq", _api_set_qq, True),
        ("GET", "config", _api_config, True),
        ("POST", "config", _api_save_config, True),
        ("GET", "redpacks", _api_redpacks, True),
        ("GET", "groups", _api_groups, True),
    ]
    for method, path, handler, auth in routes:
        register_route(method, f"{PREFIX}/{path}", handler, auth=auth)
        _registered.append(f"{method} {PREFIX}/{path}")
    for name in _ASSETS:
        register_route("GET", f"{PREFIX}/assets/{name}", _asset, auth=False)
        _registered.append(f"GET {PREFIX}/assets/{name}")
    log.info("娱乐助手 Web 路由已注册: /api/ext/funhelper/*")


def _avatar_url(uid, user_data=None):
    """构造用户真实 Avatar URL。优先事件捕获到的真实头像, 再 QQ号, 再 openid+appid。"""
    user_data = user_data or {}
    stored = str(user_data.get("avatar", "") or "").strip()
    if stored.startswith("http"):
        return stored
    qq = str(user_data.get("qq", "") or "").strip()
    if qq.isdigit():
        return f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
    if user_data.get("appid"):
        return f"https://q.qlogo.cn/qqapp/{user_data['appid']}/{uid}/640"
    appid = p.get_appid()
    if appid:
        return f"https://q.qlogo.cn/qqapp/{appid}/{uid}/640"
    return ""


async def _api_users(request):
    gid = _resolve_gid(request)
    try:
        all_users = p.all_users()
    except Exception:
        all_users = {}
    users_list = []
    for uid, user_data in all_users.items():
        if uid in ("_meta",):
            continue
        users_list.append({
            "id": uid,
            "nickname": p.clean_nick(user_data.get("nickname", "")),
            "qq": user_data.get("qq", ""),
            "appid": user_data.get("appid", ""),
            "avatar": _avatar_url(uid, user_data),
            "points": user_data.get("points", 0),
            "armor": user_data.get("armor", 0),
            "last_sign": user_data.get("last_sign", ""),
        })
    users_list.sort(key=lambda x: x["points"], reverse=True)
    return web.json_response({"success": True, "data": users_list})





async def _api_user(request):
    gid = _resolve_gid(request)

    uid = request.query.get("user_id", "")
    if not uid:
        return web.json_response({"success": False, "error": "user_id required"}, status=400)
    user = p._ensure(uid)
    return web.json_response({"success": True, "data": {
        "id": uid,
        "nickname": p.clean_nick(user.get("nickname", "")),
        "qq": user.get("qq", ""),
        "appid": user.get("appid", ""),
        "avatar": _avatar_url(uid, user),
        "points": user.get("points", 0),
        "armor": user.get("armor", 0),
        "last_sign": user.get("last_sign", ""),
    }})


async def _api_set_points(request):
    gid = _resolve_gid(request)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid json"}, status=400)
    uid = str(body.get("user_id", ""))
    points = int(body.get("points", 0))
    if not uid:
        return web.json_response({"success": False, "error": "user_id required"}, status=400)
    p.set_points(uid, points)
    return web.json_response({"success": True, "data": {"id": uid, "points": p.get_points(uid)}})


async def _api_add_points(request):
    gid = _resolve_gid(request)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid json"}, status=400)
    uid = str(body.get("user_id", ""))
    amount = int(body.get("amount", 0))
    if not uid:
        return web.json_response({"success": False, "error": "user_id required"}, status=400)
    if amount <= 0:
        return web.json_response({"success": False, "error": "amount must > 0"}, status=400)
    total = p.add_points(uid, amount)
    return web.json_response({"success": True, "data": {"id": uid, "added": amount, "points": total}})


async def _api_sub_points(request):
    gid = _resolve_gid(request)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid json"}, status=400)
    uid = str(body.get("user_id", ""))
    amount = int(body.get("amount", 0))
    if not uid:
        return web.json_response({"success": False, "error": "user_id required"}, status=400)
    if amount <= 0:
        return web.json_response({"success": False, "error": "amount must > 0"}, status=400)
    current = p.get_points(uid)
    actual = min(amount, current)
    total = p.add_points(uid, -actual)
    return web.json_response({"success": True, "data": {"id": uid, "removed": actual, "points": total}})


async def _api_delete_user(request):
    gid = _resolve_gid(request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid json"}, status=400)
    uid = str(body.get("user_id", ""))
    if not uid:
        return web.json_response({"success": False, "error": "user_id required"}, status=400)
    if uid == "_meta":
        return web.json_response({"success": False, "error": "不能删除系统记录"}, status=400)
    removed = p.remove_user(uid)
    return web.json_response({"success": removed, "data": {"id": uid, "removed": removed}})


async def _api_set_qq(request):
    gid = _resolve_gid(request)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid json"}, status=400)
    uid = str(body.get("user_id", ""))
    qq = str(body.get("qq", "")).strip()
    if not uid:
        return web.json_response({"success": False, "error": "user_id required"}, status=400)
    p.set_qq(uid, qq)
    return web.json_response({"success": True, "data": {"id": uid, "qq": qq}})


async def _api_config(request):
    return web.json_response({"success": True, "data": load_config()})


async def _api_save_config(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid json"}, status=400)
    cfg = load_config()
    for key in cfg:
        if key in body:
            cfg[key] = body[key]
    save_config(cfg)
    return web.json_response({"success": True, "data": load_config()})


def _list_joined_groups():
    """获取机器人已加入的所有群 [{group_id, group_name, member_count}], 失败返回空列表。"""
    out = []
    try:
        from core.application import get_app

        app = get_app()
        bots = getattr(app, "_bots", {}) if app else {}
    except Exception:
        return out
    seen = set()
    for appid, bot in bots.items():
        try:
            rows = bot.log_service.query_data(
                "SELECT group_id, group_name, group_member_num, in_group "
                "FROM groups_users WHERE group_id != ? AND in_group = 1",
                ("",),
            )
        except Exception:
            continue
        for row in rows or []:
            gid = str(row.get("group_id") or "").strip()
            if not gid or gid in seen:
                continue
            seen.add(gid)
            out.append({
                "group_id": gid,
                "group_name": str(row.get("group_name") or ""),
                "member_count": int(row.get("group_member_num") or 0),
            })
    return out


async def _api_groups(request):
    """返回机器人当前已加入的所有群; 已退出的群自动移除并清空数据。

    成员数实时刷新: 逐个调用 bot.get_group_info 拉取最新群资料 (QQ API),
    踢人/进人后打开面板即显示最新人数。
    """
    joined = _list_joined_groups()
    joined_ids = {g["group_id"] for g in joined}
    # 清理: 本地有数据但机器人已不在该群 → 清空
    for gid in list(p.list_groups()):
        if str(gid) not in joined_ids:
            p.remove_group(gid)
    for gid in list(rs.list_groups()):
        if str(gid) not in joined_ids:
            rs.remove_group(gid)
    # 实时刷新群成员数
    if joined:
        try:
            from core.application import get_app

            app = get_app()
            bots = getattr(app, "_bots", {}) if app else {}

            async def _refresh_one(gid):
                for bot in bots.values():
                    sender = getattr(bot, "sender", None)
                    if sender is None or not hasattr(sender, "get_group_info"):
                        continue
                    try:
                        await sender.get_group_info(gid, return_error=True)
                        break
                    except Exception as e:
                        log.warning("群刷新异常 %s: %s", str(gid)[:8], e)
                        continue

            await asyncio.gather(*(_refresh_one(g["group_id"]) for g in joined))
            joined = _list_joined_groups()
        except Exception:
            pass
    data = []
    for g in joined:
        gid = g["group_id"]
        data.append({
            "id": gid,
            "name": g.get("group_name", ""),
            "members": g.get("member_count", 0),
            "users": p.group_user_count(gid),
            "legacy": False,
        })
    # 排序: 有数据的优先, 然后按群名
    data.sort(key=lambda x: (0 if x["users"] > 0 else 1, x.get("name") or x["id"]))
    return web.json_response({"success": True, "data": data})


async def _api_redpacks(request):
    gid = _resolve_gid(request)
    packs = g.list_redpacks()
    data = []
    for x in packs:
        total = int(x.get("total") or 0)
        count = int(x.get("count") or 1)
        data.append({
            "id": x.get("id"),
            "sender_id": x.get("sender_id"),
            "sender_name": p.nick(x.get("sender_id")) if x.get("sender_id") else "",
            "total": total,
            "count": count,
            "remaining": int(x.get("remaining") or 0),
            "left": int(x.get("remaining") or 0),
            "amount": int(total / max(1, count)),
        })
    return web.json_response({"success": True, "data": data})
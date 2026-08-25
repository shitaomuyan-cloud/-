"""王者荣耀查询 - 基于远梦API (https://api.qzqi.com/api-doc/Wzry.html)。

接口: GET https://api.qzqi.com/api/v1/Wzry
参数: type=profile|battles|hero-info|search-camp-users|skins|equip-recommend 等
响应: {success, data(string 原始JSON), error, error_type}
"""
import json

import httpx

_WZRY_API = "https://api.qzqi.com/api/v1/Wzry"
_TIMEOUT = 20.0


async def _call(params: dict) -> dict:
    """调用 API, 统一返回 {ok, data, error}。"""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_WZRY_API, params=params)
            resp.raise_for_status()
            body = resp.json()
    except Exception as e:
        return {"ok": False, "error": f"网络请求失败: {e}"}
    if not body.get("success"):
        return {"ok": False, "error": str(body.get("error") or "未知错误")}
    return {"ok": True, "data": body.get("data")}


def _load(data):
    """data 字段是字符串形式的原始数据, 尝试解析为 Python 对象。"""
    if isinstance(data, str):
        try:
            return json.loads(data)
        except Exception:
            return data
    return data


def _fmt(obj, max_len: int = 1000) -> str:
    """把解析后的数据转成可读文本(截断)。"""
    if obj is None:
        return "(无数据)"
    if isinstance(obj, (dict, list)):
        try:
            text = json.dumps(obj, ensure_ascii=False, indent=1)
        except Exception:
            text = str(obj)
    else:
        text = str(obj)
    return text[:max_len] + ("\n…(已截断)" if len(text) > max_len else "")


def _pick(obj, keys, default=""):
    """从 dict 里依次尝试取字段(支持 a.b 嵌套)。"""
    if not isinstance(obj, dict):
        return default
    for k in keys:
        node = obj
        ok = True
        for part in str(k).split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                ok = False
                break
        if ok and node not in (None, "", [], {}):
            return node
    return default


async def search_user(name: str) -> dict:
    """按昵称搜索营地用户。"""
    return await _call({"type": "search-camp-users", "name": name})


async def get_profile(camp_id: str) -> dict:
    """查询营地主页信息。"""
    return await _call({"type": "profile", "camp_id": camp_id})


async def get_battles(camp_id: str) -> dict:
    """查询最近战绩。"""
    return await _call({"type": "battles", "camp_id": camp_id})


async def get_hero(hero_name: str) -> dict:
    """查询英雄信息。"""
    return await _call({"type": "hero-info", "hero_name": hero_name})


def format_search(data) -> str:
    """搜索营地用户 → 用户列表。"""
    obj = _load(data)
    if isinstance(obj, list):
        lines = []
        for i, u in enumerate(obj[:10], 1):
            name = _pick(u, ["name", "nickname", "user_name"], "?")
            cid = _pick(u, ["camp_id", "id", "uid", "user_id"], "")
            area = _pick(u, ["area", "zone", "server"], "")
            tail = f" ({area})" if area else ""
            lines.append(f"{i}. {name}  `{cid}`{tail}")
        if not lines:
            return "🔍 未找到该昵称的营地用户"
        return "🔍 搜索结果:\n" + "\n".join(lines)
    return "🔍 搜索结果:\n" + _fmt(obj, 600)


def format_profile(data) -> str:
    """主页信息。"""
    obj = _load(data)
    if isinstance(obj, dict):
        name = _pick(obj, ["nickname", "name", "user_name"], "?")
        lines = [f"🎮 {name}"]
        for key, label in [
            ("grade", "段位"), ("rank", "段位"), ("level", "等级"),
            ("win_rate", "胜率"), ("total_games", "总场次"),
            ("hero_num", "英雄数"), ("skin_num", "皮肤数"),
            ("week_star", "本周之星"),
        ]:
            v = _pick(obj, [key], "")
            if v:
                lines.append(f"- {label}: {v}")
        if len(lines) == 1:
            lines.append(_fmt(obj, 600))
        return "\n".join(lines)
    return "🎮 主页信息:\n" + _fmt(obj, 600)


def format_battles(data) -> str:
    """最近战绩。"""
    obj = _load(data)
    if isinstance(obj, dict):
        # 常见包装: {battles: [...]} / {list: [...]} / {data: [...]}
        items = None
        for k in ("battles", "list", "data", "items", "records"):
            if isinstance(obj.get(k), list):
                items = obj[k]
                break
        if items is not None:
            lines = ["⚔️ 最近战绩"]
            for i, b in enumerate(items[:10], 1):
                hero = _pick(b, ["hero_name", "hero", "name"], "?")
                result = _pick(b, ["result", "win", "is_win"], "?")
                result_icon = {"win": "✅", "胜利": "✅", "true": "✅", "True": "✅", "1": "✅"}.get(str(result), "❌")
                kda = _pick(b, ["kda", "score"], "")
                mvps = _pick(b, ["mvp", "is_mvp"], "")
                line = f"{i}. {result_icon} {hero}"
                if kda:
                    line += f" | {kda}"
                if mvps:
                    line += " | 🏆MVP"
                lines.append(line)
            if len(items) == 0:
                return "⚔️ 暂无战绩"
            return "\n".join(lines)
    return "⚔️ 战绩:\n" + _fmt(obj, 600)


def format_hero(data) -> str:
    """英雄信息。"""
    obj = _load(data)
    if isinstance(obj, dict):
        name = _pick(obj, ["hero_name", "name", "title"], "?")
        lines = [f"🗡️ {name}"]
        for key, label in [
            ("position", "定位"), ("role", "定位"), ("difficulty", "难度"),
            ("win_rate", "胜率"), ("ban_rate", "禁用率"), ("pick_rate", "出场率"),
            ("tier", "强度"),
        ]:
            v = _pick(obj, [key], "")
            if v:
                lines.append(f"- {label}: {v}")
        if len(lines) == 1:
            lines.append(_fmt(obj, 600))
        return "\n".join(lines)
    return "🗡️ 英雄信息:\n" + _fmt(obj, 600)

"""娱乐助手：签到、抽奖、抢劫、反甲、同归于尽、红包、禁言、撤回、生图、积分 + Web。"""
import asyncio
import contextlib
import glob
import json
import os
import re
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timedelta

import httpx
import io
from PIL import Image, ImageDraw, ImageFont

from core.base.logger import PLUGIN, get_logger
from core.message._http import MSG_TYPE_MARKDOWN
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page

from .app import webpanel
from .app import games as g
from .app import points as p
from .app import entconfig

# 台风查询 (并入娱乐助手: 导入即注册 台风/台风查询/台风列表 等指令)
from .台风 import *  # noqa: F401,F403

__plugin_meta__ = {
    "name": "娱乐助手",
    "author": "慕言 慕北",
    "description": "群娱乐玩法全家桶：每日签到/抽奖/反甲/抢劫/同归于尽/积分红包/禁言/引用撤回/生图扣积分，含 Web 管理后台，积分按群独立",
    "version": "2.3.7",
    "github": "https://github.com/shitaomuyan-cloud/-",
}
log = get_logger(PLUGIN, "娱乐助手")
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PANEL_HTML = os.path.join(_PLUGIN_DIR, "web", "panel.html")
_PAGE_KEY = "entertainment"
_DRAW_API = "http://api.qwq.nki.pw/API/AI/BaiDuDraw.php"
_DOG_API = "https://gulangsc.cn/API/zt/dsg.php"
_MONEY_API = "https://gulangsc.cn/API/zt/yao.php"
_DOG_KEY = "lNHw9dR"


def _identity(event) -> dict:
    """从事件里尽量取到用户的 QQ 号与头像 (openid 类 bot 可能拿不到)。"""
    qq = str(
        getattr(event, "qq", "")
        or getattr(getattr(event, "sender", None), "qq", "")
        or getattr(getattr(event, "author", None), "user_id", "")
        or ""
    ).strip()
    avatar = str(
        getattr(event, "avatar", "")
        or getattr(event, "member_avatar", "")
        or getattr(getattr(event, "sender", None), "avatar", "")
        or getattr(getattr(event, "author", None), "avatar", "")
    ).strip()
    if not qq.isdigit():
        qq = ""
    return {"qq": qq, "avatar": avatar}


def _gid_handler(fn):
    """命令装饰器: 入口设置群上下文 (积分/红包按群隔离)。"""
    import functools

    @functools.wraps(fn)
    async def wrapper(event, match):
        gid = str(getattr(event, "group_id", "") or "")
        p.set_group(gid)
        entconfig.set_current(entconfig.group_config(gid))
        return await fn(event, match)

    return wrapper




def _first_line(text, limit=20):
    """描述截断: 只保留第一行, 长度超 limit 加省略号. 用于生图 caption 等 '描述只能在第一行' 场景。"""
    text = str(text or "").strip()
    # 移除换行, 只留第一行内容
    head = text.splitlines()[0] if text else ""
    if len(head) > limit:
        return head[:limit].rstrip() + "…"
    return head

# 娱乐帮助宫格图生成（参考 QQ 应用菜单风格）
_FONT_B_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
_FONT_R_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


def _ft(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def render_help_image(rows):
    """生成娱乐菜单按键图: rows = [(指令名, 描述, 分类标签), ...]
    样式: 按键一行3个, 指令名居中, 分类底色, 右上角小徽章
    """
    COLS = 3
    MARGIN_X, MARGIN_Y = 20, 20
    GAP_X, GAP_Y = 14, 14
    BTN_W, BTN_H = 340, 126  # 按键尺寸

    rows_count = (len(rows) + COLS - 1) // COLS
    W = MARGIN_X * 2 + BTN_W * COLS + GAP_X * (COLS - 1)
    H = MARGIN_Y * 2 + rows_count * BTN_H + (rows_count - 1) * GAP_Y

    img = Image.new("RGB", (W, H), (250, 251, 253))
    d = ImageDraw.Draw(img)

    # 分类 -> (按键底色, 文字深色, 徽章底, 徽章字)
    btn_colors = {
        "积分": ((235, 241, 255), (56, 82, 210), (216, 228, 255), (88, 110, 255)),
        "互动": ((244, 238, 255), (108, 78, 220), (235, 225, 255), (130, 100, 250)),
        "红包": ((255, 238, 238), (220, 70, 70), (255, 224, 224), (255, 90, 90)),
        "消耗": ((246, 246, 248), (120, 125, 135), (238, 238, 240), (150, 155, 165)),
        "管理": ((249, 246, 236), (160, 130, 60), (244, 238, 220), (180, 150, 80)),
        "系统": ((236, 246, 255), (50, 120, 200), (222, 240, 255), (60, 140, 220)),
    }
    _default = ((246, 246, 248), (120, 125, 135), (238, 238, 240), (150, 155, 165))

    for i, (name, desc, tag) in enumerate(rows):
        col = i % COLS
        row = i // COLS
        x = MARGIN_X + col * (BTN_W + GAP_X)
        y = MARGIN_Y + row * (BTN_H + GAP_Y)

        bg, fg, tag_bg, tag_fg = btn_colors.get(tag, _default)

        # ---- 按键底(圆角矩形) ----
        d.rounded_rectangle((x, y, x + BTN_W, y + BTN_H), radius=12, fill=bg)

        # ---- 指令名居中 ----
        d.text((x + BTN_W // 2, y + 40), name, font=_ft(_FONT_B_PATH, 26), fill=fg, anchor="mm")

        # ---- 描述小字居中 ----
        _desc_font = _ft(_FONT_R_PATH, 15)
        max_desc_width = BTN_W - 44
        lines = []
        line = ""
        for ch in str(desc):
            test = line + ch
            bbox = d.textbbox((0, 0), test, font=_desc_font)
            if bbox[2] - bbox[0] <= max_desc_width:
                line = test
            else:
                if line:
                    lines.append(line)
                line = ch
        if line:
            lines.append(line)
        _dy = y + 82
        for li, ln in enumerate(lines[:1]):
            d.text((x + BTN_W // 2, _dy + li * 22), ln, font=_desc_font, fill=(140, 146, 160), anchor="mm")

        # ---- 右上角小徽章 ----
        _tag_font = _ft(_FONT_R_PATH, 13)
        tbbox = d.textbbox((0, 0), tag, font=_tag_font)
        tw, th = tbbox[2] - tbbox[0] + 14, tbbox[3] - tbbox[1] + 6
        tx, ty = x + BTN_W - tw - 12, y + 12
        d.rounded_rectangle((tx, ty, tx + tw, ty + th), radius=6, fill=tag_bg)
        d.text((tx + tw // 2, ty + th // 2), tag, font=_tag_font, fill=tag_fg, anchor="mm")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# 生图互斥: 同一群同时只允许一个生图任务 (gid -> bool)
_draw_busy: dict = {}
_draw_busy_lock = threading.Lock()


def _draw_acquire(gid) -> bool:
    """尝试占用生图任务, 已被占用返回 False。"""
    global _draw_busy
    with _draw_busy_lock:
        if _draw_busy.get(gid):
            return False
        _draw_busy[gid] = True
        return True


def _draw_release(gid):
    global _draw_busy
    with _draw_busy_lock:
        _draw_busy.pop(gid, None)


def _uid(event):
    uid = str(getattr(event, "user_id", "") or "")
    if uid:
        ident = _identity(event)
        with contextlib.suppress(Exception):
            p.touch(
                uid,
                nickname=str(
                    getattr(event, "username", "")
                    or getattr(getattr(event, "sender", None), "nickname", "")
                    or ""
                ).strip(),
                appid=str(getattr(event, "appid", "") or "").strip(),
                qq=ident["qq"],
                avatar=ident["avatar"],
            )
    return uid


def _touch_target(event, target_id):
    """被动参与者（被抢/被禁言/被操作等）也补录资料，确保 Web 能显示其头像（appid+openid）。
    昵称不在此覆盖——以对方主动参与时记录的真实昵称为准。"""
    if not target_id:
        return
    with contextlib.suppress(Exception):
        p.touch(str(target_id), appid=str(getattr(event, "appid", "") or "").strip())


def _at(uid):
    return f"<@{uid}>"


def _num(text):
    """从文本中提取第一个整数, 返回 (值, 是否找到数字)。"""
    if not text:
        return 0, False
    m = re.search(r"\d+", str(text))
    if not m:
        return 0, False
    return int(m.group()), True


def _clean_text(event):
    """去掉消息里的 @ 提及(<@openid>), 返回剩余纯文本, 便于从全文中取数字/描述。
    只剥离结构化提及(<@...>)，不剥离手打的 @文字——否则 '@指令1000' 这类写法会把数字吞掉。"""
    raw = (getattr(event, "content", "") or "") + " " + (getattr(event, "url_content", "") or "")
    raw = re.sub(r"<@[^>]*>", " ", raw)
    return raw


def _all_numbers(event):
    """从整条消息(已剥离 @ 提及)中提取全部整数, 顺序即出现顺序。"""
    return [int(x) for x in re.findall(r"\d+", _clean_text(event))]


def _after_keyword(event, keyword):
    """取关键词之后的全部文本(剥离 @ 提及), 用于生图描述等。"""
    raw = (getattr(event, "content", "") or "") + " " + (getattr(event, "url_content", "") or "")
    idx = raw.find(keyword)
    if idx < 0:
        return ""
    rest = raw[idx + len(keyword):]
    rest = re.sub(r"<@[^>]*>", " ", rest)
    rest = re.sub(r"@[\u4e00-\u9fa5\w]+", " ", rest)
    return rest.strip()


def _mentions(event, include_self=False):
    """返回 events.mentions 中被 @ 的普通成员 id 列表（排除自己/机器人/@全体）。
    include_self=True 时允许解析到发送者自己（管理员给自己加积分等场景）。"""
    ids = []
    for mention in getattr(event, "mentions", None) or []:
        if not isinstance(mention, dict):
            continue
        mid = str(
            mention.get("id")
            or mention.get("member_openid")
            or mention.get("user_id")
            or ""
        ).strip()
        if (
            mid
            and not mention.get("is_you")
            and not mention.get("bot")
            and mention.get("scope") != "all"
            and (include_self or mid != _uid(event))
        ):
            ids.append(mid)
    return ids


def _mention_text(event, include_self=False):
    """从原始文本中抓取 <@openid> 或显式 openid，作为 mentions 缺失时的兜底。"""
    raw = (getattr(event, "content", "") or "") + " " + (getattr(event, "url_content", "") or "")
    visible = [m.group(1) for m in re.finditer(r"<@([^>]+)>", raw)]
    visible = [v for v in visible if include_self or v != _uid(event)]
    if visible:
        return visible[0]
    for token in re.findall(r"[A-Za-z0-9_\-]{16,}", raw):
        if token != _uid(event) or include_self:
            return token
    return ""


def _first_mention(event, include_self=False):
    """解析 @目标：优先 events.mentions，其次原文兜底。
    include_self=True 时允许目标为发送者自己（管理员给自己加积分）。"""
    for mid in _mentions(event, include_self=include_self):
        return mid
    return _mention_text(event, include_self=include_self)


async def _is_admin(event):
    if getattr(event, "member_role", "") in ("admin", "owner"):
        return True
    # owner_ids 判定（失败静默，不影响后续兜底）
    with contextlib.suppress(Exception):
        from core.base.config import cfg

        bot_cfg = cfg.get_bot_config(getattr(event, "appid", ""))
        owner = {str(x) for x in (bot_cfg.get("owner_ids") or [])} if bot_cfg else set()
        if _uid(event) in owner:
            return True
    # 兜底: openid 场景 event.member_role 可能为空, 从群成员记录读取角色
    if getattr(event, "group_id", ""):
        try:
            record = await event.get_group_record(event.group_id)
            users = record.get("users") if isinstance(record, dict) else None
            if isinstance(users, list):
                target = _uid(event)
                for item in users:
                    if not isinstance(item, dict):
                        continue
                    mid = str(item.get("userid") or item.get("user_id") or item.get("id") or "")
                    if mid == target:
                        return str(item.get("member_role") or "member") in ("admin", "owner")
        except Exception:
            return False
    return False


def _avatar(event, uid=None):
    uid = uid or _uid(event)
    return f"https://q.qlogo.cn/qqapp/{getattr(event, 'appid', '') or '100000000'}/{uid}/640"


def _prefix_at(event):
    """返回「头像 + @」markdown 前缀: 圆形头像 + @提及 (群管同款写法, QQ markdown 渲染)。"""
    uid = str(getattr(event, "user_id", "") or "")
    if not uid:
        return ""
    avatar = _avatar(event, uid)
    return f"![头像 #30px #30px]({avatar}) <@{uid}>\n\n"




def _c(v):
    """数值占位（保留纯文本，不使用代码样式）。"""
    return str(v)


async def _md(event, text, at=True):
    """发送回复；at=True 时前置「头像 + @」一行 (群管同款, QQ markdown 渲染)。"""
    if at:
        uid = str(getattr(event, "user_id", "") or "")
        if uid and not text.startswith(("![头像", "<@")):
            text = f"{_prefix_at(event)}{text}"
    try:
        await event.reply(text)
    except Exception:
        with contextlib.suppress(Exception):
            await event.reply(text)


def _r(title, rows):
    """统一回复模板：emoji 标题 + 引用块内容（QQ markdown 灰色竖线卡片装饰）。"""
    return title + "\n" + "\n".join(f"> {r}" for r in rows)

async def _card(event, title, items=None, desc="", at=True):
    """卡片感回复：头像+@ 一行，标题 + 引用块内容（QQ markdown 渲染为浅色卡片样式）。
    Ark 原生卡片在当前通道发送失败，故用 markdown 实现同等卡片观感。"""
    uid = str(getattr(event, "user_id", "") or "")
    head = ""
    if at and uid:
        head = _prefix_at(event)
    await _md(event, head + _r(title, items or []), at=False)


async def _limit_mute(event, uid, minutes: int = 2):
    """惩罚性禁言（频繁触发限次指令时使用），失败静默。"""
    expire = (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    try:
        await event.sender.set_group_member_mute(
            event.group_id,
            [{"op": "add", "member_openid": uid, "mute_expire_at": expire}],
        )
    except Exception:
        pass


def _msg_timestamp(message_id: str) -> str:
    """从消息日志查某 message_id 的入库时间, 查不到返回空。"""
    if not message_id:
        return ""
    log_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "log")
    for db in sorted(glob.glob(os.path.join(log_root, "*", "*", "message.db")), reverse=True):
        try:
            con = sqlite3.connect(db)
            row = con.execute("SELECT timestamp FROM log WHERE message_id=? ORDER BY id DESC LIMIT 1", (message_id,)).fetchone()
            con.close()
            if row and row[0]:
                return str(row[0])
        except Exception:
            continue
    return ""


def _message_id_by_refidx(ref_idx: str, before_ts: str = "", exclude_mid: str = "", fingerprints: list = None) -> str:
    """按引用的 REFIDX 反查真实 message_id（无时间限制）。

    文字消息: REFIDX 前后缀稳定, 全串匹配即可。
    图片/表情包: 引用时 REFIDX 前缀临时变化, 用「共享段 + 多指纹(文本/faceId/fileid)」匹配 raw_message, 无时间窗口限制。
    """
    if not ref_idx:
        return ""
    candidates = {ref_idx, urllib.parse.unquote(ref_idx), urllib.parse.quote(ref_idx, safe="")}
    # 通用共享段: REFIDX 末尾固定尾巴(各消息共享), 取最后 60 字符
    seg = ref_idx[-60:] if len(ref_idx) > 60 else ref_idx
    fps = [f for f in (fingerprints or []) if f]
    log_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "log")
    for db in sorted(glob.glob(os.path.join(log_root, "*", "*", "message.db")), reverse=True):
        try:
            con = sqlite3.connect(db)
            cur = con.cursor()
            # 1) 全串
            for cand in candidates:
                cur.execute("SELECT message_id FROM log WHERE reference_id=? ORDER BY id DESC LIMIT 1", (cand,))
                row = cur.fetchone()
                if row and row[0] and (not exclude_mid or row[0] != exclude_mid):
                    con.close()
                    return str(row[0])
            # 2) 共享段 + 指纹SQL过滤 (每个指纹独立查询, 不依赖 LIMIT 截断)
            if seg and fps:
                for fp in fps:
                    fp_n = fp.replace("\s+", "").replace("%", "\\%").replace("_", "\\_")
                    if len(fp_n) < 2:
                        continue
                    sql = "SELECT message_id, content, raw_message FROM log WHERE reference_id LIKE ? AND (content LIKE ? OR raw_message LIKE ?)"
                    params: list = [f"%{seg}", f"%{fp_n}%", f"%{fp_n}%"]
                    if exclude_mid:
                        sql += " AND message_id<>?"
                        params.append(exclude_mid)
                    sql += " ORDER BY id DESC LIMIT 5"
                    for mid, content, raw_msg in cur.execute(sql, params).fetchall():
                        if not mid:
                            continue
                        # 跳过"撤回命令"特征的消息(避免命中其他撤回命令)
                        c_txt = (content or "").strip()
                        if c_txt == "撤回" or re.match(r"^<@[^>]*>\s*撤回$", c_txt):
                            continue
                        con.close()
                        return str(mid)
            # 3) 共享段兜底 (仅当没有任何指纹时)
            elif seg:
                sql = "SELECT message_id FROM log WHERE reference_id LIKE ?"
                params: list = [f"%{seg}"]
                if exclude_mid:
                    sql += " AND message_id<>?"
                    params.append(exclude_mid)
                sql += " ORDER BY id DESC LIMIT 5"
                row2 = cur.execute(sql, params).fetchone()
                if row2 and row2[0]:
                    con.close()
                    return str(row2[0])
            con.close()
        except Exception:
            continue
    return ""


def _ref_msg_id_from_raw(event) -> str:
    """从被引用消息的 ref_msg_idx 反查 log 表, 拿到被引用消息的真实 message_id"""
    ref_idx = _scene_ref_id(event) or str(getattr(event, "message_reference_id", "") or "").strip()
    if not ref_idx:
        return ""
    # 提取被引用消息的多个指纹: 文本 / faceId / fileid(QQ 把被引用消息元素塞在 msg_elements)
    fingerprints: list = []
    for el in getattr(event, "msg_elements", None) or []:
        if not isinstance(el, dict):
            continue
        c = (el.get("content") or "").strip()
        # face 新格式 <faceType=1,faceId="317",ext="..."> -> [face id=317]
        for fid in re.findall(r'faceId\s*=\s*"?\s*(\d+)', c):
            fingerprints.append(f"[face id={fid}]")
            fingerprints.append(f"face id={fid}")
        if c:
            fingerprints.append(re.sub(r"\s+", "", c)[:24])
        for att in (el.get("attachments") or []):
            if not isinstance(att, dict):
                continue
            u = att.get("url") or ""
            fm = re.search(r"fileid=([^&]+)", u)
            if fm:
                fingerprints.append(fm.group(1)[:48])
            fn = (att.get("filename") or "")[:40]
            if fn:
                fingerprints.append(fn)
    ts = str(getattr(event, "timestamp", "") or "").strip()
    mid = str(getattr(event, "message_id", "") or "")
    return _message_id_by_refidx(ref_idx, before_ts=ts, exclude_mid=mid, fingerprints=fingerprints)


def _scene_ref_id(event) -> str:
    """从事件 message_scene.ext 提取被引用消息的 REFIDX(ref_msg_idx)，取不到返回空。"""
    for item in (getattr(event, "message_scene", {}) or {}).get("ext", []) or []:
        m = re.search(r"(?:^|[?&])ref_msg_idx=([^&\s]+)", str(item))
        if m:
            return m.group(1)
    return ""


def _referenced_content(event) -> str:
    """从 event.msg_elements 提取被引用消息的内容(换一种识别方式: 内容匹配)。"""
    parts = []
    for el in getattr(event, "msg_elements", None) or []:
        if isinstance(el, dict):
            c = el.get("content")
            if c:
                parts.append(str(c))
        elif isinstance(el, str):
            parts.append(el)
    return " ".join(parts).strip()


def _find_message_by_content(group_id: str, content: str, before_ts: str, limit: int = 5) -> str:
    """按被引用消息内容(规范化子串)在日志中匹配最近一条消息的 message_id（不限发送方）。"""
    if not content or not group_id:
        return ""
    needle = re.sub(r"\s+", "", content)
    needle = needle[:24].replace("%", "\\%").replace("_", "\\_")
    if len(needle) < 4:
        return ""
    ts_norm = str(before_ts or "").replace("T", " ")[:19]
    log_root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "log")
    for db in sorted(glob.glob(os.path.join(log_root, "*", "*", "message.db")), reverse=True):
        try:
            con = sqlite3.connect(db)
            cur = con.cursor()
            rows = cur.execute(
                "SELECT message_id, content FROM log WHERE group_id=? "
                "AND substr(replace(timestamp,'T',' '),1,19)<=? AND replace(content,' ','') LIKE ? ORDER BY id DESC LIMIT ?",
                (group_id, ts_norm, f"%{needle}%", limit * 3),
            ).fetchall()
            con.close()
            for mid, c in rows:
                if mid and re.sub(r"\s+", "", c or "") == re.sub(r"\s+", "", content):
                    return mid
            if rows:
                # 无完全一致则退回第一条
                return str(rows[0][0])
        except Exception:
            continue
    return ""


def _disp_w(s) -> int:
    """估算字符串显示宽度：中文/全角按 2，半角按 1。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(s))


def _pad_name(s: str, width: int) -> str:
    """用全角空格把名称补齐到指定显示宽度，使积分列对齐。"""
    return str(s) + "　" * max(0, width - _disp_w(s))


def _nick(uid) -> str:
    """显示用户昵称(替换 <@openid>, 文本不渲染@提及)。"""
    return p.nick(uid)[:10]


async def _limit_guard(event, key: str, name: str, cooldown: int = p.COOLDOWN_SECONDS) -> bool:
    """限次守卫：通过返回 True；否则提示并（冷却期）执行惩罚，返回 False。
    冷却期内第一次频繁仅提示，再次触发才禁言 2 分钟。cooldown=0 表示只检查每日次数。"""
    ok, reason, extra, warned = p.check_and_record_limit(_uid(event), key, cooldown=cooldown)
    if ok:
        return True
    if reason == "cooldown":
        if not warned:
            await _md(event, f"⏳ {name}操作太频繁，请 {_c(extra)} 秒后再试")
            return False
        await _md(event, f"⏳ 太频繁啦，已禁言 2 分钟")
        await _limit_mute(event, _uid(event))
        return False
    await _md(event, f"📅 {name}今日次数已用完（每日 {_c(p.DAILY_LIMIT)} 次）")
    return False


@on_load
async def _init():
    with contextlib.suppress(Exception):
        webpanel.load_config()
    with contextlib.suppress(Exception):
        register_page(key=_PAGE_KEY, label="娱乐助手", source="plugin", source_name="entertainment", html_file=_PANEL_HTML)
    with contextlib.suppress(Exception):
        webpanel.register_routes()


@on_unload
async def _cleanup():
    with contextlib.suppress(Exception):
        webpanel.unregister_routes()
    with contextlib.suppress(Exception):
        unregister_page(_PAGE_KEY)


# 按键菜单: 每个按键点击后显示的用法说明 (data 走「菜单:指令」通道)
_MENU_INFO = {
    "签到": "📅 **签到**\n每日 1 次，随机得积分\n\n发送「签到」执行",
    "抽奖": "🎰 **抽奖**\n花积分抽奖，带中奖率（每日5次）\n\n发送「抽奖」执行",
    "我的": "👤 **我的**\n查看积分 / 反甲 / 排名 / 今日签到状态\n\n发送「我的」执行",
    "积分排行": "🏆 **积分排行**\n查看全群积分排行榜 Top10\n\n发送「积分排行」执行",
    "抢劫": "💰 **抢劫**\n随机抢对方的积分，失败会被反扣\n\n发送「抢劫 @某人」执行",
    "同归于尽": "💥 **同归于尽**\n双方各扣积分，同归于尽\n\n发送「同归于尽 @某人」执行",
    "购买反甲": "🛡️ **购买反甲**\n花积分买护盾，被抢时自动反弹\n\n发送「购买反甲 [数量]」执行\n例：购买反甲2",
    "单身狗": "🐶 **单身狗**\n生成恶搞配图\n\n发送「单身狗 @某人」或「单身狗 QQ号」执行",
    "马内": "💰 **马内**\n生成「我想要马内」求财配图\n\n发送「马内 @某人」或「马内 QQ号」执行",
    "发红包": "🧧 **发红包**\n发出积分红包，口令 1~4 位数字，30 分钟未领完退回\n\n发送「发红包 总积分 份数 口令」执行\n例：发红包 100 3 12",
    "抢红包": "🧧 **抢红包**\n抢群内可抢的红包\n\n发送「抢红包」直接抢，或「抢红包 口令」按口令抢",
    "红包列表": "📋 **红包列表**\n查看当前可抢的红包\n\n发送「红包列表」执行",
    "禁言": "🔇 **禁言**\n花积分禁言群成员，默认 1 分钟\n\n发送「禁言 @某人 [分钟]」执行",
    "撤回": "🗑️ **撤回**\n撤回机器人发过的消息（扣积分）\n\n先引用机器人消息，再发送「撤回」执行",
    "生图": "🎨 **生图**\n花积分 AI 绘图，可选比例\n\n发送「生图 描述」执行\n例：生图 一只猫",
}


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*(娱乐菜单|娱乐帮助|娱乐指令)(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="娱乐菜单", desc="查看娱乐助手全部指令(按键菜单)", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_help(event, match):
    # 按键菜单: 一行3个, 点击后在输入框预填指令模板, 用户补全参数后发送
    # QQ 内联键盘限制最多 5 行, 故精简为 15 个按键 (5行×3列)
    # 管理指令(添加/删除积分)仅管理员用, 手动输入即可; 娱乐菜单按键冗余移除
    # 按键菜单: QQ 内联键盘限制最多 5 行, 每行最多 5 个 → 用 4行×4列 放 16 个按键
    btns = [
        ("签到", "签到"),
        ("抽奖", "抽奖"),
        ("我的", "我的"),
        ("积分排行", "积分排行"),
        ("抢劫", "抢劫 @某人"),
        ("同归于尽", "同归于尽 @某人"),
        ("购买反甲", "购买反甲 数量"),
        ("单身狗", "单身狗 @某人"),
        ("马内", "马内 @某人"),
        ("发红包", "发红包 积分 份 口令"),
        ("抢红包", "抢红包 口令"),
        ("红包列表", "红包列表"),
        ("禁言", "禁言 @某人 分钟"),
        ("撤回", "撤回"),
        ("台风", "台风"),
        ("生图", "生图 描述"),
    ]
    # enter=True: 点击后模板填入输入框 (用户补全参数再发送), 不模拟发送; 每行4个
    button_rows = [[{"text": t, "data": d, "enter": True} for t, d in btns[i:i + 4]] for i in range(0, len(btns), 4)]
    # 头像+@ + 居中标题图(自带分割线), markdown 图片块级居中显示; 去掉头像行后空行
    content = _prefix_at(event).rstrip() + "\n" + "![🎮 娱乐菜单 #640px #150px](https://ac10238b5a674cd83.app.workbuddy.link/web/yy_menu_title2.png)"
    try:
        await event.reply(content, buttons=button_rows)
    except Exception as e:
        log.warning("发送按键菜单失败, 降级为图片: %s", e)
        _c = entconfig.get_current()
        rows = [
            ("签到", "每日1次，随机得积分", "积分"),
            ("抽奖", "花积分抽奖，带中奖率", "积分"),
            ("我的", "查看积分/反甲/排名/签到", "积分"),
            ("积分排行", "查看全群排行榜", "积分"),
            ("抢劫 @某人", "随机抢积分，失败反扣", "互动"),
            ("同归于尽 @某人", "双方各扣积分少者的全部", "互动"),
            ("购买反甲 [数量]", "花积分购护盾防抢，如 购买反甲2", "互动"),
            ("单身狗 @某人 或 QQ号", "生成单身狗恶搞配图", "互动"),
            ("马内 @某人 或 QQ号", "生成求财配图", "互动"),
            ("发红包 积分 份数 口令", "口令1~4位数字，30分钟未领完退回", "红包"),
            ("抢红包 [口令]", "直接抢或按口令抢", "红包"),
            ("红包列表", "查看可抢红包", "红包"),
            ("禁言 @某人 [分钟]", "花积分禁言，默认1分钟", "消耗"),
            ("撤回", "引用消息后发送本指令撤回", "消耗"),
            ("生图 描述", "花积分AI绘图，弹按钮选比例", "消耗"),
            ("添加积分", "管理员加积分", "管理"),
            ("删除积分", "管理员扣积分", "管理"),
            ("台风", "查询当前最强台风 + 路径图", "系统"),
            ("台风查询 名称/编号", "查详情与路径图", "系统"),
            ("台风列表 [年份]", "活跃/按年列表", "系统"),
            ("娱乐菜单", "查看全部指令", "系统"),
        ]
        try:
            img = render_help_image(rows)
            await event.reply_image(img, content="🎮 娱乐助手 · 指令菜单")
        except Exception as e2:
            log.warning("降级图片也失败, 改为文本: %s", e2)
            lines = ["🎮 娱乐助手 · 指令", "", "| 指令 | 说明 |", "| :---: | :---: |"]
            for cmd, desc, _ in rows:
                lines.append(f"| {cmd} | {desc} |")
            await _md(event, "\n".join(lines))


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*(菜单|按键)[:：]\s*([\u4e00-\u9fa5A-Za-z0-9]+)\s*$", name="菜单说明", desc="按键点击后回复对应指令用法", priority=58, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_menu_info(event, match):
    key = (match.group(2) or "").strip()
    info = _MENU_INFO.get(key)
    if info:
        await _md(event, info)
        return
    # 未知按键 → 重发菜单
    await cmd_help(event, match)


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*签到(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="签到", desc="每日签到, 得积分并显示头像", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_sign(event, match):
    gained, total, already = g.sign(_uid(event))
    if already:
        await _card(event, "📅 今日已签到", items=[f"当前积分：{_c(total)}"])
    else:
        await _card(event, "✅ 签到成功", items=[f"获得积分：{_c(chr(43)+str(gained))}", f"当前积分：{_c(total)}"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*抽奖(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="抽奖", desc="花积分抽奖, 中积分 (每日5次, 30秒间隔)", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_lottery(event, match):
    if not await _limit_guard(event, "lottery", "抽奖", cooldown=0):
        return
    _lc = entconfig.get_current()["lottery_cost"]
    won, total, insuf, is_win = g.lottery(_uid(event))
    if insuf:
        return await _md(event, f"⚠️ 积分不足\n抽奖需要 {_c(_lc)} 积分")
    if is_win:
        await _card(event, "🎉 中奖啦", items=[f"获得积分：{_c(chr(43)+str(won))}", f"当前积分：{_c(total)}"])
    else:
        await _card(event, "😔 没中奖", items=[f"消耗：{_c(_lc)} 积分", f"当前积分：{_c(total)}"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*我的(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="我的", desc="查看个人积分/反甲/排名/签到状态", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_me(event, match):
    uid = _uid(event)
    pts = p.get_points(uid)
    armor = p.armor_count(uid)
    signed = p.last_sign_date(uid) == p.today_sign_key()
    rnk = 1
    for i, row in enumerate(p.top_list(9999), 1):
        if row["id"] == uid:
            rnk = i
            break
    status = "✅ 已签到" if signed else "❌ 未签到"
    await _card(event, "👤 我的信息", items=[f"积分：{_c(pts)}", f"反甲：{_c(armor)} 个", f"排名：{_c(chr(35)+str(rnk))}", f"今日：{status}"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*(积分排行|排行|排行榜)(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="积分排行", desc="查看全群积分排行榜", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_rank(event, match):
    rows = p.top_list(10)
    if not rows:
        return await _md(event, "📭 暂无数据，快去签到吧")
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    pts_w = max((len(str(r["points"])) for r in rows), default=1)
    lines = ["🏆 积分排行榜", "", "| 排名 | 昵称 | 积分 |", "| :---: | :---: | :---: |"]
    for i, row in enumerate(rows, 1):
        medal = medals.get(i, f"{i}.")
        nick = str(row.get("nickname") or "")[:10]
        code = f"{row['points']} 分"
        # 固定宽度等宽代码块：列居中但代码块右边缘一致 → 分字一条线
        lines.append(f"| {medal} | {nick} | `{code:>{pts_w + 2}}` |")
    await _md(event, "\n".join(lines))


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*购买反甲\s*(\d+)?\s*$", name="购买反甲", desc="花积分购买反甲护盾 (支持数量: 购买反甲2)", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_armor(event, match):
    cfg = entconfig.get_current()
    cost = int(cfg.get("armor_cost", 100))
    num = int(match.group(1) or "1")
    num = max(1, min(num, 999))
    total = cost * num
    uid = _uid(event)
    if not g.can_afford(uid, total):
        return await _md(event, f"⚠️ 积分不足\n购买 {_c(num)} 个反甲需要 {_c(total)} 积分（当前积分 {_c(p.get_points(uid))}）")
    g.charge(uid, total)
    cnt = p.add_armor(uid, num)
    pts = p.get_points(uid)
    await _card(event, "🛡️ 购买成功", items=[f"反甲：{_c(num)} 个（现有 {_c(cnt)}）", f"花费：{_c(total)} 积分", f"剩余积分：{_c(pts)}", "提示：有人抢劫你时会自动反弹"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*抢劫(?=\s|$|<|@)", name="抢劫", desc="抢劫 @某人 (每日5次, 30秒间隔)", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_rob(event, match):
    target = _first_mention(event)
    if not target:
        return await _md(event, "⚠️ 请 @ 要抢劫的对象")
    att = _uid(event)
    if target == att:
        return await _md(event, "⚠️ 不能抢自己")
    _touch_target(event, target)
    if not await _limit_guard(event, "rob", "抢劫"):
        return
    stolen, atk, _, ok, armored = g.robbery(att, target)
    if ok:
        await _card(event, "💰 抢劫成功", items=[f"{_at(target)} 被抢走 {_c(chr(43)+str(stolen))}", f"你的积分：{_c(atk)}"])
    elif armored:
        await _card(event, "🛡️ 反甲反弹", items=[f"{_at(target)} 反弹你 {_c(stolen)} 积分", f"你的积分：{_c(atk)}"])
    else:
        await _card(event, "😵 抢劫失败", items=[f"{_at(target)} 反抢你 {_c(stolen)} 积分", f"你的积分：{_c(atk)}"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*同归于尽(?=\s|$|<|@)", name="同归于尽", desc="同归于尽 @对方 (每日5次, 30秒间隔)", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_boom(event, match):
    target = _first_mention(event)
    if not target:
        return await _md(event, "⚠️ 请 @ 同归于尽的对象")
    if target == _uid(event):
        return await _md(event, "⚠️ 不能对自己使用")
    _touch_target(event, target)
    if not await _limit_guard(event, "boom", "同归于尽"):
        return
    d, i0, t0, ok = g.mutual_destruction(_uid(event), target)
    if not ok:
        return await _md(event, "⚠️ 双方积分都需大于 0 才能同归于尽")
    await _card(event, "💥 同归于尽", items=[f"双方各扣：{_c(d)} 积分", f"你：{_c(i0)}", f"{_at(target)}：{_c(t0)}"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*发红包(?=\s|$|<|@)", name="发红包", desc="发红包 总积分 份数 口令(1~4位数字), 顺序无所谓", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_send(event, match):
    nums = _all_numbers(event)
    if len(nums) < 3:
        return await _md(event, "⚠️ 用法：发红包 总积分 份数 口令（口令 1~4 位数字）\n例：发红包 100 3 12")
    ok, msg, pid = g.create_redpack(_uid(event), int(nums[0]), int(nums[1]), password=str(nums[2]))
    if not ok:
        return await _md(event, f"⚠️ {msg}")
    await _card(event, "🧧 红包已发出", items=[f"总额：{_c(nums[0])} 积分 · {_c(nums[1])} 份", f"口令：{_c(pid)}", f"提示：发送「抢红包 {_c(pid)}」领取"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*抢红包(?=\s|$|<|@)", name="抢红包", desc="抢红包 (可直接抢或带口令)", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_claim(event, match):
    uid = _uid(event)
    nums = _all_numbers(event)
    pid = str(nums[0]) if nums else ""
    if pid:
        ok, amt, msg, _ = g.claim_redpack(uid, pid)
    else:
        ok, amt, msg, _ = g.claim_any_redpack(uid)
    if ok:
        await _card(event, "🧧 抢到红包", items=[f"获得：{_c(chr(43)+str(amt))} 积分"])
    else:
        await _md(event, f"⚠️ {msg}")


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*红包列表(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="红包列表", desc="查看可抢红包", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_packs(event, match):
    packs = g.list_redpacks()
    if not packs:
        return await _md(event, "📭 当前没有可抢的红包")
    items = [f"· {_nick(x['sender_id'])}　剩 {_c(str(x['remaining'])+chr(47)+str(x['count']))} 份　口令 {_c(x['id'])}" for x in packs[:10]]
    await _card(event, "🧧 当前可抢红包", items=items + ["", "提示：发送「抢红包 口令」即可抢"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*禁言(?!菜单|列表)(?=\s|$|<|@)", name="禁言", desc="禁言 @对方 [分钟], 每分钟100积分, 默认1分钟, 每日5次", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_mute(event, match):
    if not await _limit_guard(event, "mute", "禁言", cooldown=0):
        return
    target = _first_mention(event)
    if not target:
        return await _md(event, "⚠️ 请 @ 要禁言的对象")
    if target == _uid(event):
        return await _md(event, "⚠️ 不能禁言自己")
    _touch_target(event, target)
    uid = _uid(event)
    nums = _all_numbers(event)
    minutes = nums[0] if nums else 1
    minutes = max(1, min(43200, minutes))  # 1分钟 ~ 30天
    cost = minutes * entconfig.get_current()["mute_cost"]
    if not g.can_afford(uid, cost):
        return await _md(event, f"⚠️ 积分不足\n禁言 {_c(minutes)} 分钟需要 {_c(cost)} 积分")
    g.charge(uid, cost)
    expire = (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    try:
        raw = await event.sender.set_group_member_mute(
            event.group_id,
            [{"op": "add", "member_openid": target, "mute_expire_at": expire}],
        )
    except Exception as e:
        g.refund(uid, cost)
        return await _md(event, f"⚠️ 禁言失败：{e}（积分已退还）")
    ok = raw
    if isinstance(raw, (tuple, list)):
        ok = raw[0] if raw else False
    elif isinstance(raw, dict):
        ok = bool(raw.get("ok") or raw.get("code") in (0, 200) or raw.get("success"))
    if not ok:
        g.refund(uid, cost)
        return await _md(event, "⚠️ 禁言失败（需机器人是群管理员），积分已退还")
    await _card(event, "🔇 禁言成功", items=[f"{_at(target)} 已禁言 {_c(minutes)} 分钟", f"消耗：{_c(cost)} 积分"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*撤回(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="撤回", desc="引用机器人消息后发送撤回, 扣积分, 每日5次", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_recall(event, match):
    uid = _uid(event)
    if not await _limit_guard(event, "recall", "撤回", cooldown=0):
        return
    if not g.charge(uid, entconfig.get_current()["revoke_cost"]):
        return await _md(event, f"⚠️ 积分不足\n撤回需要 {_c(entconfig.get_current()['revoke_cost'])} 积分")
    # 识别被引用消息: 1) ref_msg_idx 反查  2) msg_elements 内容匹配（均仅限机器人发送的消息）
    msg_id = _ref_msg_id_from_raw(event)
    if not msg_id:
        g.refund(uid, entconfig.get_current()["revoke_cost"])
        return await _md(event, "⚠️ 无法撤回：该消息未同步到机器人（贴纸/系统消息QQ不推送，或消息日志已过期），积分已退还")
    log.info("撤回请求: msg_id=%s", msg_id)
    sender = getattr(event, "_sender", None)
    endpoint = event.recall_endpoint
    # 撤回执行: 失败自动重试3次 (网络抖动/限频/瞬时错误), 尽量稳定
    ok, data = False, {}
    last_err = ""
    for attempt in range(1, 4):
        try:
            if sender and endpoint:
                ok, data = await sender.delete(endpoint.format(message_id=msg_id))
            else:
                ok, data = await event.recall(message_id=msg_id), {}
            log.info("撤回响应[%d]: ok=%r data=%s", attempt, ok, (str(data)[:120] if data else ""))
            if ok:
                break
            last_err = (data.get("message") or data.get("msg") or "") if isinstance(data, dict) else ""
            await asyncio.sleep(0.6)
        except Exception as e:
            last_err = str(e)
            log.warning("撤回异常[%d]: %s", attempt, e)
            await asyncio.sleep(0.6)
    # 业务校验: QQ 即使 HTTP 200 也可能 retcode != 0
    business_ok = bool(ok)
    if isinstance(data, dict):
        rc = data.get("retcode")
        if rc is not None and rc != 0:
            business_ok = False
    if not business_ok:
        g.refund(uid, entconfig.get_current()["revoke_cost"])
        msg = (data.get("msg") or data.get("message") or last_err or "无权限或消息不存在") if isinstance(data, dict) else (last_err or "未知")
        return await _md(event, f"⚠️ 撤回失败：{msg}（积分已退还）")
    await _card(event, "🗑️ 撤回成功", items=[f"消耗：{_c(entconfig.get_current()['revoke_cost'])} 积分", f"剩余积分：{_c(p.get_points(uid))}"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*单身狗(?=\s|$|<|@)", name="单身狗", desc="恶搞QQ头像生成单身狗配图", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_dog(event, match):
    target = _first_mention(event)
    nums = _all_numbers(event)
    qq = str(nums[0]) if nums else ""
    params = {"key": _DOG_KEY}
    if qq:
        params["qq"] = qq
    else:
        tqq = p.get_qq(target) if target else ""
        if target and tqq.isdigit():
            params["qq"] = tqq
        elif target:
            params["url"] = _avatar(event, target)
        else:
            return await _md(event, "⚠️ 请 @ 要恶搞的对象或输入 QQ 号\n例：单身狗 @某人 / 单身狗 123456")
    await _md(event, "🐶 正在生成单身狗配图…")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(_DOG_API, params=params)
            data = resp.content
    except Exception as e:
        return await _md(event, f"⚠️ 生成失败：{e}")
    if not data:
        return await _md(event, "⚠️ 生成失败，请稍后再试")
    try:
        await event.reply_image(data, content="🐶 单身狗配图已生成")
    except Exception:
        await _md(event, "🐶 单身狗配图已生成")


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*马内(?=\s|$|<|@)", name="马内", desc="恶搞头像生成我想要马内求财配图", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_money(event, match):
    target = _first_mention(event)
    nums = _all_numbers(event)
    qq = str(nums[0]) if nums else ""
    params = {"key": _DOG_KEY}
    if qq:
        params["qq"] = qq
    else:
        tqq = p.get_qq(target) if target else ""
        if target and tqq.isdigit():
            params["qq"] = tqq
        elif target:
            params["url"] = _avatar(event, target)
        else:
            return await _md(event, "⚠️ 请 @ 要恶搞的对象或输入 QQ 号\n例：马内 @某人 / 马内 123456")
    await _md(event, "💰 正在生成求财配图…")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(_MONEY_API, params=params)
            data = resp.content
    except Exception as e:
        return await _md(event, f"⚠️ 生成失败：{e}")
    if not data:
        return await _md(event, "⚠️ 生成失败，请稍后再试")
    try:
        await event.reply_image(data, content="💰 「我想要马内」配图已生成")
    except Exception:
        await _md(event, "💰 「我想要马内」配图已生成")


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*生图\s*(?P<rest>.+?)\s*$", name="生图", desc="扣积分 AI绘图 (可点按钮选比例)", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_draw(event, match):
    uid = _uid(event)
    rest = (match.group("rest") or "").strip()
    # 解析 prompt|size (按钮回调格式: "生图 美女|1024x1024")
    if "|" in rest:
        prompt, size = rest.split("|", 1)
        prompt = prompt.strip()
        size = size.strip()
    else:
        prompt = rest
        size = ""
    if not prompt:
        return await _md(event, "🎨 请附上绘图描述\n例：生图 一只猫咪")
    cfg = entconfig.get_current()
    draw_cost = int(cfg.get("draw_cost", 50))
    api_base = (cfg.get("draw_api_base") or "").strip()
    api_key = (cfg.get("draw_api_key") or "").strip()
    # 未指定比例
    if not size:
        # 已配置自定义接口 → 弹比例按钮 (先检查积分不扣, 选完再扣)
        if api_base and api_key:
            if not g.can_afford(uid, draw_cost):
                return await _md(event, f"⚠️ 积分不足\n生图需要 {_c(draw_cost)} 积分")
            return await _show_ratio_buttons(event, prompt, draw_cost)
        # 未配置接口 → 内置百度绘图 (仅 1:1), 直接生成
        if not g.charge(uid, draw_cost):
            return await _md(event, f"⚠️ 积分不足\n生图需要 {_c(draw_cost)} 积分")
        return await _draw_legacy(event, prompt, draw_cost, size="")
    # 已选比例 → 扣分 + 直接生成 (互斥: 同群同一时刻只允许一个生图任务)
    if not g.charge(uid, draw_cost):
        return await _md(event, f"⚠️ 积分不足\n生图需要 {_c(draw_cost)} 积分")
    gid = str(getattr(event, "group_id", "") or "")
    if not _draw_acquire(gid):
        g.refund(uid, draw_cost)
        return await _md(event, "⚠️ 该群已有生图任务进行中\n请等待完成后再试")
    try:
        if api_base and api_key:
            return await _draw_openai(event, prompt, draw_cost, cfg, api_base, api_key, size=size)
        return await _draw_legacy(event, prompt, draw_cost, size=size)
    finally:
        _draw_release(gid)


async def _show_ratio_buttons(event, prompt, draw_cost):
    """发送比例选择按钮卡片 (用户点击后通过 dispatcher 回到 cmd_draw 执行)。"""
    # 检查积分够不够 (提示但不扣, 等用户选完再扣)
    if not g.can_afford(_uid(event), draw_cost):
        return await _md(event, f"⚠️ 积分不足\n生图需要 {_c(draw_cost)} 积分")
    p_safe = prompt.replace("|", " ")  # 防注入 (极少)
    button_rows = [
        [
            {"text": "1:1 正方形", "data": f"生图 {p_safe}|1024x1024", "reply": True},
            {"text": "16:9 横屏", "data": f"生图 {p_safe}|1792x1024", "reply": True},
        ],
        [
            {"text": "9:16 竖屏", "data": f"生图 {p_safe}|1024x1792", "reply": True},
            {"text": "🖥 PC壁纸", "data": f"生图 {p_safe}|1920x1080", "reply": True},
        ],
    ]
    content = _prefix_at(event) + f"🎨 选择生图比例\n描述：「{_first_line(prompt, 8)}」\n扣：{_c(draw_cost)} 积分"
    try:
        await event.reply(content, buttons=button_rows)
    except Exception:
        # 框架不支持按钮时回退 markdown 提示
        await _md(event, f"🎨 当前框架不支持按钮, 请手动加比例参数:\n生图 {prompt} 1024x1024\n生图 {prompt} 1792x1024\n生图 {prompt} 1024x1792")




async def _post_draw_done(event, prompt, draw_cost, size, img_bytes, fallback_url=""):
    """生图完成: 一条图片消息 (图片 + caption 纯文本说明).
    QQ 图片消息 caption 协议为纯文本 (不支持 <@uid>/头像内嵌), bot 头像由消息列表头部自动显示.
    这是 QQ 协议下最稳定的生图展示方案."""
    title = f"🎨 生成完成「{_first_line(prompt, 8)}」"
    meta = "\n".join(filter(None, [f"比例：{size}" if size else "", f"消耗：{draw_cost} 积分"]))
    caption = "\n".join(filter(None, [title, meta]))
    if img_bytes:
        try:
            await event.reply_image(img_bytes, content=caption)
            return
        except Exception:
            pass
    if fallback_url:
        try:
            await event.reply_image(fallback_url, content=caption)
            return
        except Exception:
            pass
    await _md(event, _prefix_at(event) + caption + (f"\n{fallback_url}" if fallback_url else "\n生图失败"))


async def _draw_legacy(event, prompt, draw_cost, size=""):
    """内置绘图接口 (百度绘图, 兼容旧行为)。"""
    await _md(event, f"🎨 正在生成「{_first_line(prompt, 8)}」…")
    try:
        params = {"keyword": prompt}
        if size:
            params["size"] = size
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(_DRAW_API, params=params)
            try:
                data = resp.json()
            except Exception:
                try:
                    data = json.loads(resp.text)
                except Exception:
                    data = resp.text
    except Exception as e:
        g.refund(_uid(event), draw_cost)
        return await _md(event, f"⚠️ 生图服务异常：{e}（积分已退还）")
    url = ""
    if isinstance(data, dict):
        raw = data.get("url") or data.get("image") or data.get("img") or data.get("data") or ""
        url = raw[0] if isinstance(raw, list) and raw else str(raw).strip()
    elif isinstance(data, list):
        url = data[0] if data else ""
    elif isinstance(data, str):
        url = data.strip()
    if not url.startswith("http"):
        g.refund(_uid(event), draw_cost)
        return await _md(event, "⚠️ 生图失败，积分已退还")
    title = f"生成完成「{_first_line(prompt, 8)}」"
    info_lines = []
    if size:
        info_lines.append(f"比例：{size}")
    info_lines.append(f"消耗：{draw_cost} 积分")
    text = f"🎨 {title}\n" + "\n".join(info_lines)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            img_bytes = (await client.get(url)).content
        await _post_draw_done(event, prompt, draw_cost, size or "1024x1024", img_bytes, fallback_url=url)
    except Exception:
        await _md(event, _prefix_at(event) + f"🎨 「{_first_line(prompt, 8)}」\n{url}")


async def _draw_openai(event, prompt, draw_cost, cfg, api_base, api_key, size="1024x1024"):
    """OpenAI 兼容生图接口 (配置在 Web 面板「生图服务」)。"""
    model = (cfg.get("draw_model") or "").strip() or "gpt-image-2"
    proxy = (cfg.get("draw_proxy") or "").strip()
    # 取消回调
    if size == "cancel":
        return await _md(event, f"已取消生图「{prompt}」")
    await _md(event, f"🎨 正在生成「{_first_line(prompt, 8)}」({size})…")
    try:
        kwargs = {"proxy": proxy} if proxy else {}
        async with httpx.AsyncClient(timeout=120, **kwargs) as client:
            resp = await client.post(
                f"{api_base.rstrip('/')}/images/generations",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "prompt": prompt, "n": 1, "size": size},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        g.refund(_uid(event), draw_cost)
        return await _md(event, f"⚠️ 生图服务异常：{e}（积分已退还）")
    items = (data.get("data") or []) if isinstance(data, dict) else []
    if not items:
        g.refund(_uid(event), draw_cost)
        return await _md(event, "⚠️ 生图失败，积分已退还")
    b64 = str(items[0].get("b64_json") or "")
    url = str(items[0].get("url") or "")
    if b64:
        try:
            import base64 as _b64
            img_bytes = _b64.b64decode(b64)
            await _post_draw_done(event, prompt, draw_cost, size, img_bytes, fallback_url=url)
            return
        except Exception:
            pass
    if url.startswith("http"):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                img_bytes = (await client.get(url)).content
            await _post_draw_done(event, prompt, draw_cost, size, img_bytes, fallback_url=url)
            return
        except Exception:
            await _md(event, _prefix_at(event) + f"🎨 「{_first_line(prompt, 8)}」\n{url}")
            return
    g.refund(_uid(event), draw_cost)
    await _md(event, "⚠️ 生图失败，积分已退还")


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*添加积分(?=\s|$|<|@)", name="添加积分", desc="管理员给 @对方 加积分", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_add(event, match):
    uid = _uid(event)
    if not await _is_admin(event):
        return await _md(event, "👑 仅管理员可使用此指令")
    target = _first_mention(event, include_self=True)
    nums = _all_numbers(event)
    val = nums[0] if nums else 0
    if not target or val < 1:
        return await _md(event, "➕ 用法：添加积分 @对方 数量（@自己也可）")
    _touch_target(event, target)
    total = p.add_points(target, val)
    await _card(event, "➕ 添加成功", items=[f"{_at(target)} {_c(chr(43)+str(val))}", f"当前积分：{_c(total)}"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*删除积分(?=\s|$|<|@)", name="删除积分", desc="管理员给 @对方 扣积分", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_sub(event, match):
    uid = _uid(event)
    if not await _is_admin(event):
        return await _md(event, "👑 仅管理员可使用此指令")
    target = _first_mention(event, include_self=True)
    nums = _all_numbers(event)
    val = nums[0] if nums else 0
    if not target or val < 1:
        return await _md(event, "➖ 用法：删除积分 @对方 数量（@自己也可）")
    _touch_target(event, target)
    cur = p.get_points(target)
    actual = min(val, cur)
    total = p.add_points(target, -actual)
    await _card(event, "➖ 扣除成功", items=[f"{_at(target)} {_c(chr(45)+str(actual))}", f"当前积分：{_c(total)}"])

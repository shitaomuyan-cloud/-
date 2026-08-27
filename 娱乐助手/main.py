"""娱乐助手：签到、抽奖、抢劫、反甲、同归于尽、红包、禁言、撤回、生图、积分 + Web。"""
import asyncio
import contextlib
import glob
import contextvars
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
import threading
import urllib.parse
from datetime import datetime, timedelta

import httpx
import io
from PIL import Image, ImageDraw, ImageFont

from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_load, on_unload
from core.plugin.web_pages import register_page, unregister_page


__plugin_meta__ = {
    "name": "娱乐助手",
    "author": "慕言 慕北",
    "description": "群娱乐玩法全家桶：每日签到/抽奖/反甲/抢劫/同归于尽/积分红包/禁言/引用撤回/生图扣积分/台风查询/二次元插画，含 Web 管理后台，积分按群独立",
    "version": "2.4.3",
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
        points.set_group(gid)
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
            points.touch(
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
        points.touch(str(target_id), appid=str(getattr(event, "appid", "") or "").strip())


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
    return points.nick(uid)[:10]


async def _limit_guard(event, key: str, name: str, cooldown=None):
    if cooldown is None:
        cooldown = points.COOLDOWN_SECONDS  # 运行时查默认值
    """限次守卫：通过返回 True；否则提示并（冷却期）执行惩罚，返回 False。
    冷却期内第一次频繁仅提示，再次触发才禁言 2 分钟。cooldown=0 表示只检查每日次数。"""
    ok, reason, extra, warned = points.check_and_record_limit(_uid(event), key, cooldown=cooldown)
    if ok:
        return True
    if reason == "cooldown":
        if not warned:
            await _md(event, f"⏳ {name}操作太频繁，请 {_c(extra)} 秒后再试")
            return False
        await _md(event, f"⏳ 太频繁啦，已禁言 2 分钟")
        await _limit_mute(event, _uid(event))
        return False
    await _md(event, f"📅 {name}今日次数已用完（每日 {_c(points.DAILY_LIMIT)} 次）")
    return False


@on_load
async def _init():
    with contextlib.suppress(Exception):
        webpanel.load_config()
    with contextlib.suppress(Exception):
        register_page(key=_PAGE_KEY, label="娱乐助手", source="plugin", source_name="entertainment", html_file=_PANEL_HTML)
    with contextlib.suppress(Exception):
        webpanel.register_routes()
    # 插画图库定时自动更新 (内置; playwright 缺失时自动跳过)
    _chahua_start_refresh_task()


@on_unload
async def _cleanup():
    _chahua_stop_refresh_task()
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
    "插画": "🖼️ **插画**\n随机一张二次元插画，图库每小时自动更新\n\n发送「插画」或「插画 二次元随机」执行",
}


# ============ 娱乐菜单分页 ============
_menu_state: dict = {}
_MENU_STATE_LOCK = threading.Lock()


def _menu_remember(uid, resp, page):
    """记录某用户当前菜单消息ID (用于翻页时撤回)"""
    mid = ""
    if isinstance(resp, dict):
        mid = str(resp.get("id") or resp.get("msg_id") or resp.get("message_id") or "")
    elif isinstance(resp, (tuple, list)) and resp:
        mid = str(resp[0] or "")
    with _MENU_STATE_LOCK:
        _menu_state[uid] = {"msg_id": mid, "page": page}


async def _menu_recall(event, uid):
    """撤回该用户上一次菜单消息"""
    with _MENU_STATE_LOCK:
        st = _menu_state.get(uid) or {}
        mid = st.get("msg_id") or ""
    if not mid:
        return
    try:
        await event.recall(message_id=mid)
    except Exception:
        pass


_MENU_P1_BTNS = [
    ("签到", "签到"), ("抽奖", "抽奖"), ("我的", "我的"), ("积分排行", "积分排行"),
    ("抢劫", "抢劫 @某人"), ("同归于尽", "同归于尽 @某人"), ("购买反甲", "购买反甲 数量"), ("单身狗", "单身狗 @某人"),
    ("马内", "马内 @某人"), ("发红包", "发红包 积分 份 口令"), ("抢红包", "抢红包 口令"), ("红包列表", "红包列表"),
    ("禁言", "禁言 @某人 分钟"), ("撤回", "撤回"), ("台风", "台风"), ("生图", "生图 描述"),
]
_MENU_P2_BTNS = [
    ("域名信息", "域名 baidu.com"), ("添加积分", "添加积分 @"), ("删除积分", "删除积分 @"), ("图床", "图床"),
    ("插画", "插画"),
]


def _menu_rows(btns, nav):
    """构造按键行: 指令区(每行4个) + 底部导航行"""
    rows = [[{"text": t, "data": d, "enter": True} for t, d in btns[i:i + 4]] for i in range(0, len(btns), 4)]
    if nav:
        rows.append([{"text": t, "data": d, "reply": True} for t, d in nav])
    return rows


def _menu_content(event, title):
    """菜单标题: markdown 一级标题 + 分割线 (无表格边框, 零外部依赖)"""
    return _prefix_at(event) + f"# {title}\n***"


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*(娱乐菜单|娱乐帮助|娱乐指令)(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="娱乐菜单", desc="查看娱乐助手全部指令(按键菜单)", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_help(event, match):
    uid = _uid(event)
    await _menu_recall(event, uid)
    content = _menu_content(event, "🎮 娱乐菜单")
    rows = _menu_rows(_MENU_P1_BTNS, [("下一页", "菜单页:2")])
    try:
        resp = await event.reply(content, buttons=rows)
        _menu_remember(uid, resp, 1)
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
            ("域名信息 域名", "Whois 域名查询", "系统"),
            ("插画", "随机二次元插画，图库每小时更新", "系统"),
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


@handler(r"^\s*(?:<@[^>]*>\s*)*(?:菜单页|page)[:：]?\s*(\d+)\s*$", name="菜单翻页", desc="娱乐菜单翻页(撤回当前页换页)", priority=57, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_menu_page(event, match):
    uid = _uid(event)
    page = int((match.group(1) or "1").strip() or "1")
    if page not in (1, 2):
        page = 1  # 超出范围循环回第一页
    await _menu_recall(event, uid)
    if page == 1:
        content = _menu_content(event, "🎮 娱乐菜单")
        rows = _menu_rows(_MENU_P1_BTNS, [("下一页", "菜单页:2")])
    else:
        content = _menu_content(event, "🎮 娱乐菜单二")
        rows = _menu_rows(_MENU_P2_BTNS, [("上一页", "菜单页:1")])
    try:
        resp = await event.reply(content, buttons=rows)
        _menu_remember(uid, resp, page)
    except Exception as e:
        log.warning("菜单翻页失败: %s", e)


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
    gained, total, already = games.sign(_uid(event))
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
    won, total, insuf, is_win = games.lottery(_uid(event))
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
    pts = points.get_points(uid)
    armor = points.armor_count(uid)
    signed = points.last_sign_date(uid) == points.today_sign_key()
    rnk = 1
    for i, row in enumerate(points.top_list(9999), 1):
        if row["id"] == uid:
            rnk = i
            break
    status = "✅ 已签到" if signed else "❌ 未签到"
    await _card(event, "👤 我的信息", items=[f"积分：{_c(pts)}", f"反甲：{_c(armor)} 个", f"排名：{_c(chr(35)+str(rnk))}", f"今日：{status}"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*(积分排行|排行|排行榜)(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="积分排行", desc="查看全群积分排行榜", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_rank(event, match):
    rows = points.top_list(10)
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
    if not games.can_afford(uid, total):
        return await _md(event, f"⚠️ 积分不足\n购买 {_c(num)} 个反甲需要 {_c(total)} 积分（当前积分 {_c(points.get_points(uid))}）")
    games.charge(uid, total)
    cnt = points.add_armor(uid, num)
    pts = points.get_points(uid)
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
    stolen, atk, _, ok, armored = games.robbery(att, target)
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
    d, i0, t0, ok = games.mutual_destruction(_uid(event), target)
    if not ok:
        return await _md(event, "⚠️ 双方积分都需大于 0 才能同归于尽")
    await _card(event, "💥 同归于尽", items=[f"双方各扣：{_c(d)} 积分", f"你：{_c(i0)}", f"{_at(target)}：{_c(t0)}"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*发红包(?=\s|$|<|@)", name="发红包", desc="发红包 总积分 份数 口令(1~4位数字), 顺序无所谓", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_send(event, match):
    nums = _all_numbers(event)
    if len(nums) < 3:
        return await _md(event, "⚠️ 用法：发红包 总积分 份数 口令（口令 1~4 位数字）\n例：发红包 100 3 12")
    ok, msg, pid = games.create_redpack(_uid(event), int(nums[0]), int(nums[1]), password=str(nums[2]))
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
        ok, amt, msg, _ = games.claim_redpack(uid, pid)
    else:
        ok, amt, msg, _ = games.claim_any_redpack(uid)
    if ok:
        await _card(event, "🧧 抢到红包", items=[f"获得：{_c(chr(43)+str(amt))} 积分"])
    else:
        await _md(event, f"⚠️ {msg}")


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*红包列表(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="红包列表", desc="查看可抢红包", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_packs(event, match):
    packs = games.list_redpacks()
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
    if not games.can_afford(uid, cost):
        return await _md(event, f"⚠️ 积分不足\n禁言 {_c(minutes)} 分钟需要 {_c(cost)} 积分")
    games.charge(uid, cost)
    expire = (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    try:
        raw = await event.sender.set_group_member_mute(
            event.group_id,
            [{"op": "add", "member_openid": target, "mute_expire_at": expire}],
        )
    except Exception as e:
        games.refund(uid, cost)
        return await _md(event, f"⚠️ 禁言失败：{e}（积分已退还）")
    ok = raw
    if isinstance(raw, (tuple, list)):
        ok = raw[0] if raw else False
    elif isinstance(raw, dict):
        ok = bool(raw.get("ok") or raw.get("code") in (0, 200) or raw.get("success"))
    if not ok:
        games.refund(uid, cost)
        return await _md(event, "⚠️ 禁言失败（需机器人是群管理员），积分已退还")
    await _card(event, "🔇 禁言成功", items=[f"{_at(target)} 已禁言 {_c(minutes)} 分钟", f"消耗：{_c(cost)} 积分"])


@handler(r"^\s*(?:<@[^>]*>\s*|@[\u4e00-\u9fa5\w]*\s*)*撤回(?:\s*(?:<@[^>]*>|@[\u4e00-\u9fa5\w]+))*\s*$", name="撤回", desc="引用机器人消息后发送撤回, 扣积分, 每日5次", priority=60, block=True, ignore_at_check=True)
@_gid_handler
async def cmd_recall(event, match):
    uid = _uid(event)
    if not await _limit_guard(event, "recall", "撤回", cooldown=0):
        return
    if not games.charge(uid, entconfig.get_current()["revoke_cost"]):
        return await _md(event, f"⚠️ 积分不足\n撤回需要 {_c(entconfig.get_current()['revoke_cost'])} 积分")
    # 识别被引用消息: 1) ref_msg_idx 反查  2) msg_elements 内容匹配（均仅限机器人发送的消息）
    msg_id = _ref_msg_id_from_raw(event)
    if not msg_id:
        games.refund(uid, entconfig.get_current()["revoke_cost"])
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
        games.refund(uid, entconfig.get_current()["revoke_cost"])
        msg = (data.get("msg") or data.get("message") or last_err or "无权限或消息不存在") if isinstance(data, dict) else (last_err or "未知")
        return await _md(event, f"⚠️ 撤回失败：{msg}（积分已退还）")
    await _card(event, "🗑️ 撤回成功", items=[f"消耗：{_c(entconfig.get_current()['revoke_cost'])} 积分", f"剩余积分：{_c(points.get_points(uid))}"])


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
        tqq = points.get_qq(target) if target else ""
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
        tqq = points.get_qq(target) if target else ""
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
            if not games.can_afford(uid, draw_cost):
                return await _md(event, f"⚠️ 积分不足\n生图需要 {_c(draw_cost)} 积分")
            return await _show_ratio_buttons(event, prompt, draw_cost)
        # 未配置接口 → 内置百度绘图 (仅 1:1), 直接生成
        if not games.charge(uid, draw_cost):
            return await _md(event, f"⚠️ 积分不足\n生图需要 {_c(draw_cost)} 积分")
        return await _draw_legacy(event, prompt, draw_cost, size="")
    # 已选比例 → 扣分 + 直接生成 (互斥: 同群同一时刻只允许一个生图任务)
    if not games.charge(uid, draw_cost):
        return await _md(event, f"⚠️ 积分不足\n生图需要 {_c(draw_cost)} 积分")
    gid = str(getattr(event, "group_id", "") or "")
    if not _draw_acquire(gid):
        games.refund(uid, draw_cost)
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
    if not games.can_afford(_uid(event), draw_cost):
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
        games.refund(_uid(event), draw_cost)
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
        games.refund(_uid(event), draw_cost)
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
        games.refund(_uid(event), draw_cost)
        return await _md(event, f"⚠️ 生图服务异常：{e}（积分已退还）")
    items = (data.get("data") or []) if isinstance(data, dict) else []
    if not items:
        games.refund(_uid(event), draw_cost)
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
    games.refund(_uid(event), draw_cost)
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
    total = points.add_points(target, val)
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
    cur = points.get_points(target)
    actual = min(val, cur)
    total = points.add_points(target, -actual)
    await _card(event, "➖ 扣除成功", items=[f"{_at(target)} {_c(chr(45)+str(actual))}", f"当前积分：{_c(total)}"])





# ==================== 合并的子模块代码 ====================

# ---------- from app/entconfig.py ----------


_PLUGIN_DIR = Path(__file__).resolve().parent
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
    # 生图服务 (OpenAI 兼容接口; 密钥/代理仅存本地 data/ 配置, 不进仓库)
    "draw_api_base": "",
    "draw_api_key": "",
    "draw_model": "gpt-image-2",
    "draw_proxy": "",
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


# 顶层 entconfig 对象 (供外部 entconfig.xxx 调用)
from types import SimpleNamespace
entconfig = SimpleNamespace(
    DEFAULTS=DEFAULTS,
    get_current=get_current,
    set_current=set_current,
    group_config=group_config,
    save_group_config=save_group_config,
    list_configured_groups=list_configured_groups,
)


# ---------- from app/games.py ----------

import random
import time


# 红包有效期: 30 分钟
REDPACK_TTL = 30 * 60

# ---------- 通用积分开销 (默认值, 实际按群配置) ----------
MUTE_COST = entconfig.DEFAULTS["mute_cost"]
REVOKE_COST = entconfig.DEFAULTS["revoke_cost"]
ARMOR_COST = entconfig.DEFAULTS["armor_cost"]
DRAW_COST = entconfig.DEFAULTS["draw_cost"]


def can_afford(user_id, cost) -> bool:
    return points.get_points(user_id) >= cost


def charge(user_id, cost) -> bool:
    """尝试扣除 cost 积分，成功返回 True。"""
    if not can_afford(user_id, cost):
        return False
    points.add_points(user_id, -cost)
    return True


def refund(user_id, cost) -> None:
    """退还积分（用于失败回滚）。"""
    points.add_points(user_id, cost)


# ---------- 签到 ----------

SIGN_LO = entconfig.DEFAULTS["sign_lo"]
SIGN_HI = entconfig.DEFAULTS["sign_hi"]


def sign(user_id, today_str: str = None):
    """签到: 每日一次, 随机积分 (按群配置)。返回 (gained, total, already_signed)。"""
    if today_str is None:
        today_str = points.today_sign_key()
    if points.last_sign_date(user_id) == today_str:
        return 0, points.get_points(user_id), True
    cfg = entconfig.get_current()
    gained = points.random_points(cfg["sign_lo"], cfg["sign_hi"])
    total = points.add_points(user_id, gained)
    points.set_last_sign_date(user_id, today_str)
    return gained, total, False


# ---------- 抽奖 ----------

LOTTERY_COST = entconfig.DEFAULTS["lottery_cost"]
LOTTERY_LO = entconfig.DEFAULTS["lottery_lo"]
LOTTERY_HI = entconfig.DEFAULTS["lottery_hi"]
LOTTERY_WIN_RATE = entconfig.DEFAULTS["lottery_win_rate"]


def lottery(user_id):
    """抽奖: 扣积分, 按胜率随机赢积分 (数值按群配置)。
    返回 (won, total, insufficient, is_win)。won 为 0 表示本次未中奖。"""
    cfg = entconfig.get_current()
    cost = cfg["lottery_cost"]
    if not can_afford(user_id, cost):
        return 0, points.get_points(user_id), True, False
    charge(user_id, cost)
    is_win = random.random() < cfg["lottery_win_rate"]
    if is_win:
        won = points.random_points(cfg["lottery_lo"], cfg["lottery_hi"])
        total = points.add_points(user_id, won)
    else:
        won = 0
        total = points.get_points(user_id)
    return won, total, False, is_win


# ---------- 抢劫 ----------

ROBBERY_LO = entconfig.DEFAULTS["robbery_lo"]
ROBBERY_HI = entconfig.DEFAULTS["robbery_hi"]
ROBBERY_SUCCESS_RATE = entconfig.DEFAULTS["robbery_rate"]


def robbery(attacker_id, defender_id):
    """抢劫: 随机积分 (数值按群配置)。
    成功: 攻击者 +分, 防守者 -分。
    失败: 攻击者 -分 (对方加对应分)。
    防守者持有反甲: 攻击必然失败, 攻击者支付 stolen 给防守者并消耗反甲。
    返回 (stolen, attacker_total, defender_total, success, armored)。
    armored=True 表示本次触发防守者反甲反弹。
    """
    cfg = entconfig.get_current()
    stolen = points.random_points(cfg["robbery_lo"], cfg["robbery_hi"])
    if stolen == 0:
        return 0, points.get_points(attacker_id), points.get_points(defender_id), False, False

    # 反甲反弹
    if points.has_armor(defender_id):
        points.consume_armor(defender_id)
        actual = min(stolen, points.get_points(attacker_id))
        if actual > 0:
            points.add_points(attacker_id, -actual)
            points.add_points(defender_id, actual)
        return actual, points.get_points(attacker_id), points.get_points(defender_id), False, True

    success = random.random() < entconfig.get_current()["robbery_rate"]
    if success:
        defender_pts = points.get_points(defender_id)
        actual = min(stolen, defender_pts)
        points.add_points(attacker_id, actual)
        points.add_robbed(attacker_id, actual)
        points.add_points(defender_id, -actual)
        return actual, points.get_points(attacker_id), points.get_points(defender_id), True, False

    attacker_pts = points.get_points(attacker_id)
    actual = min(stolen, attacker_pts)
    if actual == 0:
        return 0, attacker_pts, points.get_points(defender_id), False, False
    points.add_points(attacker_id, -actual)
    points.add_points(defender_id, actual)
    return actual, points.get_points(attacker_id), points.get_points(defender_id), False, False


# ---------- 同归于尽 ----------

def mutual_destruction(initiator_id, target_id):
    """同归于尽：双方按'当前余额'结算（抢劫已实时改过余额，先来后到自然成立）。
    双方各扣 min(双方余额)：余额少者归零，余额多者保留差额。
    任意一方余额不足 1 则无法发动。原子结算见 points.settle_mutual。
    返回 (deducted, initiator_total, target_total, ok)。
    """
    return points.settle_mutual(initiator_id, target_id)


# ---------- 红包 ----------

def create_redpack(sender_id, total_points: int, count: int, password: str = ""):
    """创建一个红包。口令为 1~4 位数字(作为红包标识)。返回 (ok, msg, pack_id)。"""

    if total_points < count:
        return False, "红包总积分不能少于份数", None
    if count < 1 or count > 100:
        return False, "份数须在 1~100 之间", None
    password = str(password or "").strip()
    if not (password.isdigit() and 1 <= len(password) <= 4):
        return False, "口令需为 1~4 位数字", None
    if load(password) is not None:
        return False, f"口令 {password} 已被占用, 换一个试试", None
    if not can_afford(sender_id, total_points):
        return False, "积分不足", None
    charge(sender_id, total_points)

    # 拼手气随机分配
    amounts = []
    remain = total_points
    remain_cnt = count
    for _ in range(count):
        if remain_cnt == 1:
            amounts.append(remain)
        else:
            # 保证剩余每份至少 1: 当前份在 [1, remain-(remain_cnt-1)] 区间随机
            amt = random.randint(1, max(1, remain - remain_cnt + 1))
            amounts.append(amt)
            remain -= amt
            remain_cnt -= 1
    random.shuffle(amounts)

    pack_id = password
    save(pack_id, {
        "sender_id": str(sender_id),
        "total": total_points,
        "count": count,
        "amounts": amounts,
        "claimed": {},
        "created_at": datetime.now().isoformat(),
        "expires_at": time.time() + REDPACK_TTL,
    })
    return True, f"红包已发出, 共 {total_points} 积分, {count} 份", pack_id


def _is_expired(data) -> bool:
    return time.time() > float(data.get("expires_at") or 0)


def _expire_refund(pack_id, data) -> int:
    """红包超时未领完: 将剩余积分退回发送者, 返回退还数量。"""
    amounts = data.get("amounts") or []
    unclaimed = sum(amounts)
    data["amounts"] = []

    save(pack_id, data)
    if unclaimed > 0:
        points.add_points(data.get("sender_id"), unclaimed)
    return unclaimed


def claim_redpack(user_id, pack_id: str):
    """抢红包: 返回 (ok, amount, msg, remaining)。"""

    data = load(pack_id)
    if data is None:
        return False, 0, "红包不存在或已过期", 0
    if _is_expired(data):
        _expire_refund(pack_id, data)
        return False, 0, "红包已超时, 剩余积分已退回", 0
    if str(user_id) == data.get("sender_id"):
        return False, 0, "不能抢自己的红包", len(data.get("amounts") or [])
    claimed = data.get("claimed") or {}
    if str(user_id) in claimed:
        return False, 0, "你已经抢过这个红包了", len(data.get("amounts") or [])
    amounts = data.get("amounts") or []
    if not amounts:
        return False, 0, "红包已抢完", 0
    amount = amounts.pop(0)
    claimed[str(user_id)] = amount
    data["claimed"] = claimed
    data["amounts"] = amounts
    save(pack_id, data)
    points.add_points(user_id, amount)
    return True, amount, f"抢到 {amount} 积分", len(amounts)


def claim_any_redpack(user_id):
    """不指定口令, 随机抢一个可抢礼包。返回与 claim_redpack 相同。"""
    packs = list_redpacks()
    for pack in packs:
        if str(pack.get("sender_id")) == str(user_id):
            continue
        ok, amount, msg, remaining = claim_redpack(user_id, pack["id"])
        if ok:
            return ok, amount, msg, remaining
    return False, 0, "当前没有可抢的红包", 0


def list_redpacks():
    """列出所有可用红包。"""

    available = []
    for pack_id, data in list_all().items():
        if _is_expired(data):
            _expire_refund(pack_id, data)
            continue
        if data.get("amounts"):
            available.append({
                "id": pack_id,
                "sender_id": data.get("sender_id"),
                "total": data.get("total"),
                "remaining": len(data["amounts"]),
                "count": data.get("count"),
            })
    return available

# ---------- from app/points.py ----------


from core.base.logger import PLUGIN, get_logger

_p_log = get_logger(PLUGIN, "娱乐助手")

_PLUGIN_DIR = Path(__file__).resolve().parent
_DATA_FILE = _PLUGIN_DIR / "data" / "points.json"

_p_lock = threading.RLock()
_p_cache = None

# 群上下文 (asyncio 协程级隔离)
_current_gid: "contextvars.ContextVar[str]" = contextvars.ContextVar("ent_gid", default="")


def set_group(gid):
    """设置当前命令所属的群 (每个 handler 入口调用一次)。"""
    _current_gid.set(str(gid or ""))


def _gid() -> str:
    gid = _current_gid.get()
    return gid or "_no_group"


def _p_migrate(raw: dict) -> dict:
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


def _p_load() -> dict:
    global _p_cache
    with _p_lock:
        if _p_cache is not None:
            return _p_cache
        if _DATA_FILE.exists():
            try:
                with open(_DATA_FILE, "r", encoding="utf-8") as f:
                    _p_cache = _p_migrate(json.load(f))
                # 若发生迁移, 立即落盘
                if "__legacy__" in (_p_cache or {}):
                    _p_save_locked()
            except Exception as e:  # noqa: BLE001
                _p_log.warning("读取积分数据失败, 重建: %s", e)
                _p_cache = {}
        else:
            _p_cache = {}
        return _p_cache


def _p_save_locked():
    """在持锁状态下写盘。"""
    if _p_cache is None:
        return
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_p_cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _DATA_FILE)


def _p_save():
    with _p_lock:
        _p_save_locked()


def _gid_store(gid=None):
    """返回该群的用户字典; 不存在时创建 (并继承 __legacy__ 旧数据)。"""
    gid = str(gid if gid is not None else _gid())
    if not gid or gid == "_meta":
        return {}
    data = _p_load()
    store = data.get(gid)
    if store is None:
        if gid != "__legacy__" and isinstance(data.get("__legacy__"), dict):
            # 首群继承旧全局数据
            store = data.pop("__legacy__")
            data[gid] = store
        else:
            store = {}
            data[gid] = store
        _p_save()
    return store


def _ensure(user_id, gid=None) -> dict:
    uid = str(user_id)
    store = _gid_store(gid)
    user = store.get(uid)
    if user is None:
        user = {"points": 0, "robbed": 0, "armor": 0, "last_sign": "", "nickname": "", "appid": "", "qq": "", "avatar": ""}
        store[uid] = user
        _p_save()
    else:
        # 兼容旧数据：补充 robbed 字段（通过抢劫持有的积分）
        if "robbed" not in user:
            user["robbed"] = 0
            _p_save()
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
        _p_save()
    return user


def get_appid() -> str:
    """从所有用户记录中回退获取一个可用的 appid (Web 显示头像用)。"""
    data = _p_load()
    return str(data.get("_meta", {}).get("appid") or "")


def nick(user_id) -> str:
    return clean_nick(_ensure(user_id).get("nickname", ""))


def set_nickname(user_id, nickname: str):
    user = _ensure(user_id)
    user["nickname"] = nickname
    _p_save()


def get_points(user_id) -> int:
    return _ensure(user_id).get("points", 0)


def get_robbed(user_id) -> int:
    """当前通过抢劫持有的积分。"""
    return int(_ensure(user_id).get("robbed", 0) or 0)


def add_robbed(user_id, amount: int) -> int:
    """抢劫成功时累加'抢劫所得'。"""
    user = _ensure(user_id)
    user["robbed"] = max(0, int(user.get("robbed", 0)) + int(amount))
    _p_save()
    return user["robbed"]


def settle_mutual(initiator_id, target_id):
    """原子结算'同归于尽'：在单一持锁区间内读取双方余额并扣除，彻底杜绝并发不同步。

    抢劫已经实时改动过双方余额（先来后到自然成立），此处直接按'当前余额'结算：
    双方各扣 min(双方余额) —— 余额少者归零，余额多者保留差额。
    返回 (deducted, initiator_total, target_total, ok)。
    """
    with _p_lock:
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
        _p_save()
        return deduct, iu["points"], tu["points"], True


def set_points(user_id, points: int):
    _ensure(user_id)["points"] = max(0, int(points))
    _p_save()


def add_points(user_id, delta: int) -> int:
    user = _ensure(user_id)
    user["points"] = max(0, user.get("points", 0) + int(delta))
    _p_save()
    return user["points"]


def buy_armor(user_id) -> bool:
    """购买反甲: 扣 100 积分, 积分为购置数量。（兼容旧接口）"""
    user = _ensure(user_id)
    if user.get("points", 0) < 100:
        return False
    user["points"] -= 100
    user["armor"] = user.get("armor", 0) + 1
    _p_save()
    return True


def add_armor(user_id, amount: int = 1) -> int:
    """直接累加反甲数量，不扣积分（用于已扣分后的入账）。返回新数量。"""
    user = _ensure(user_id)
    user["armor"] = user.get("armor", 0) + int(amount)
    _p_save()
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
    _p_save()
    return True


def remove_user(user_id) -> bool:
    """删除用户积分记录，若存在则移除并保存。返回是否删除了。"""
    global _p_cache
    with _p_lock:
        store = _gid_store()
        uid = str(user_id)
        if uid in store and str(uid) != "_meta":
            del store[uid]
            _p_save()
            return True
        return False


def remove_group(gid) -> bool:
    """删除某个群的全部积分数据 (机器人退出该群时调用)。返回是否删除了。"""
    global _p_cache
    with _p_lock:
        data = _p_load()
        key = str(gid)
        if key in data and key != "_meta":
            del data[key]
            _p_save()
            return True
        return False


def set_qq(user_id, qq: str):
    """绑定/修改用户显示的 QQ 号 (openid 无法自动获取，需手动登记)。"""
    _ensure(user_id)["qq"] = str(qq) if qq else ""
    _p_save()


def get_qq(user_id) -> str:
    return str(_ensure(user_id).get("qq", "") or "")


def set_last_sign_date(user_id, date_str: str):
    _ensure(user_id)["last_sign"] = date_str
    _p_save()


def last_sign_date(user_id) -> str:
    return str(_ensure(user_id).get("last_sign", "") or "")


def today_sign_key() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d")


def list_groups() -> list:
    """返回所有有数据的群 id (用于 Web 面板群选择器)。"""
    data = _p_load()
    return [str(k) for k in data if k != "_meta"]


def group_user_count(gid: str) -> int:
    store = _p_load().get(str(gid)) or {}
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
    with _p_lock:
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
                _p_save()
            return False, "cooldown", sec, warned
        if count >= daily:
            return False, "daily", daily, False
        # 正常通过: 重建状态(同时清除上次的警告标记)
        limits[key] = {"day": today, "last": now, "count": count + 1}
        _p_save()
        return True, "ok", daily - count - 1, False


# ---------- from app/redpack_store.py ----------


from core.base.logger import PLUGIN, get_logger

_rp_log = get_logger(PLUGIN, "娱乐助手红包")

_PLUGIN_DIR = Path(__file__).resolve().parent
_rp_DATA_FILE = _PLUGIN_DIR / "data" / "redpacks.json"

_rp_lock = threading.RLock()
_rp_cache = None

_current_gid: "contextvars.ContextVar[str]" = contextvars.ContextVar("ent_gid", default="")


def _rp_set_group(gid):
    """与 points._rp_set_group 一致, 设置当前命令所属群。"""
    _current_gid.set(str(gid or ""))


def _rp_gid() -> str:
    return _current_gid.get() or "_no_group"


def _rp_migrate(raw: dict) -> dict:
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


def _rp_load() -> dict:
    global _rp_cache
    with _rp_lock:
        if _rp_cache is not None:
            return _rp_cache
        if _rp_DATA_FILE.exists():
            try:
                with open(_rp_DATA_FILE, "r", encoding="utf-8") as f:
                    _rp_cache = _rp_migrate(json.load(f))
                if "__legacy__" in (_rp_cache or {}):
                    _rp_save_locked()
            except Exception as e:
                _rp_log.warning("读取红包数据失败: %s", e)
                _rp_cache = {}
        else:
            _rp_cache = {}
        return _rp_cache


def _rp_save_locked():
    if _rp_cache is None:
        return
    _rp_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _rp_DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_rp_cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _rp_DATA_FILE)


def _rp_save():
    with _rp_lock:
        _rp_save_locked()


def _rp_gid_store(gid=None):
    gid = str(gid if gid is not None else _rp_gid())
    if not gid or gid == "_meta":
        return {}
    data = _rp_load()
    store = data.get(gid)
    if store is None:
        if gid != "__legacy__" and isinstance(data.get("__legacy__"), dict):
            store = data.pop("__legacy__")
            data[gid] = store
        else:
            store = {}
            data[gid] = store
        _rp_save()
    return store


def save(pack_id: str, data: dict, gid=None):
    with _rp_lock:
        store = _rp_gid_store(gid)
        store[pack_id] = data
        _rp_cache = _rp_load()
        _rp_save()


def load(pack_id: str, gid=None) -> dict | None:
    return _rp_gid_store(gid).get(pack_id)


def list_all(gid=None) -> dict:
    return dict(_rp_gid_store(gid))


def _rp_list_groups() -> list:
    data = _rp_load()
    return [str(k) for k in data if k != "_meta"]


def delete(pack_id: str, gid=None):
    with _rp_lock:
        store = _rp_gid_store(gid)
        store.pop(pack_id, None)
        _rp_save()


def _rp_remove_group(gid) -> bool:
    """删除某个群的全部红包数据 (机器人退出该群时调用)。返回是否删除了。"""
    global _rp_cache
    with _rp_lock:
        data = _rp_load()
        key = str(gid)
        if key in data and key != "_meta":
            del data[key]
            _rp_save()
            return True
        return False


# ---------- from app/webpanel.py ----------

import yaml
from aiohttp import web
from core.base.logger import get_logger, PLUGIN
from core.plugin.web_pages import register_route


PREFIX = "/api/ext/funhelper"
_PLUGIN_DIR = Path(__file__).resolve().parent
_WEB_DIR = _PLUGIN_DIR / "web"
_wp_CONFIG_FILE = _PLUGIN_DIR / "data" / "config.json"
_ASSETS = {
    "panel.css": "text/css; charset=utf-8",
    "panel.js": "text/javascript; charset=utf-8",
}

_registered = []
RELOADED = True


def _default_config() -> dict:
    """默认规则配置 (entconfig 提供)。"""
    return dict(entconfig.DEFAULTS)


def _sync_config(cfg: dict):
    """兼容保留: 同步到 games 模块默认 (新逻辑 games 直接读 entconfig 群配置)。"""
    games.SIGN_LO = int(cfg.get("sign_lo", games.SIGN_LO))
    games.SIGN_HI = int(cfg.get("sign_hi", games.SIGN_HI))
    games.LOTTERY_COST = int(cfg.get("lottery_cost", games.LOTTERY_COST))
    games.LOTTERY_LO = int(cfg.get("lottery_lo", games.LOTTERY_LO))
    games.LOTTERY_HI = int(cfg.get("lottery_hi", games.LOTTERY_HI))
    games.LOTTERY_WIN_RATE = float(cfg.get("lottery_win_rate", games.LOTTERY_WIN_RATE))
    games.ROBBERY_LO = int(cfg.get("robbery_lo", games.ROBBERY_LO))
    games.ROBBERY_HI = int(cfg.get("robbery_hi", games.ROBBERY_HI))
    games.ROBBERY_SUCCESS_RATE = float(cfg.get("robbery_rate", games.ROBBERY_SUCCESS_RATE))
    games.MUTE_COST = int(cfg.get("mute_cost", games.MUTE_COST))
    games.REVOKE_COST = int(cfg.get("revoke_cost", games.REVOKE_COST))
    games.DRAW_COST = int(cfg.get("draw_cost", games.DRAW_COST))
    games.ARMOR_COST = int(cfg.get("armor_cost", games.ARMOR_COST))


def load_config(gid=None) -> dict:
    """按群加载配置 (entconfig)。"""
    return entconfig.group_config(gid)


def save_config(cfg: dict, gid=None):
    """按群保存配置 (entconfig)。"""
    entconfig.save_group_config(gid, cfg)


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
    points.set_group(gid)
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
        ("GET", "hosting", _api_hosting, True),
        ("POST", "hosting", _api_save_hosting, True),
        ("GET", "chahua_stats", _api_chahua_stats, True),
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
    appid = points.get_appid()
    if appid:
        return f"https://q.qlogo.cn/qqapp/{appid}/{uid}/640"
    return ""


async def _api_users(request):
    gid = _resolve_gid(request)
    try:
        all_users = points.all_users()
    except Exception:
        all_users = {}
    users_list = []
    for uid, user_data in all_users.items():
        if uid in ("_meta",):
            continue
        users_list.append({
            "id": uid,
            "nickname": points.clean_nick(user_data.get("nickname", "")),
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
    user = points._ensure(uid)
    return web.json_response({"success": True, "data": {
        "id": uid,
        "nickname": points.clean_nick(user.get("nickname", "")),
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
    points.set_points(uid, points)
    return web.json_response({"success": True, "data": {"id": uid, "points": points.get_points(uid)}})


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
    total = points.add_points(uid, amount)
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
    current = points.get_points(uid)
    actual = min(amount, current)
    total = points.add_points(uid, -actual)
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
    removed = points.remove_user(uid)
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
    points.set_qq(uid, qq)
    return web.json_response({"success": True, "data": {"id": uid, "qq": qq}})


async def _api_config(request):
    gid = _resolve_gid(request)
    return web.json_response({"success": True, "data": load_config(gid)})


async def _api_save_config(request):
    gid = _resolve_gid(request)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid json"}, status=400)
    cfg = load_config(gid)
    for key in cfg:
        if key in body:
            cfg[key] = body[key]
    save_config(cfg, gid)
    return web.json_response({"success": True, "data": load_config(gid)})


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
    for gid in list(points.list_groups()):
        if str(gid) not in joined_ids:
            points.remove_group(gid)
    for gid in list(_rp_list_groups()):
        if str(gid) not in joined_ids:
            _rp_remove_group(gid)
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
            "users": points.group_user_count(gid),
            "legacy": False,
        })
    # 排序: 有数据的优先, 然后按群名
    data.sort(key=lambda x: (0 if x["users"] > 0 else 1, x.get("name") or x["id"]))
    return web.json_response({"success": True, "data": data})


_BOT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_HOSTING_YAML = os.path.join(_BOT_ROOT, "modules", "image_hosting", "data", "config.yaml")


def _hosting_cfg() -> dict:
    """读取图床配置 yaml。"""
    if not os.path.isfile(_HOSTING_YAML):
        return {}
    with open(_HOSTING_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _coerce_value(current, value):
    """按原配置字段类型转换前端提交的值。"""
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return current
    return str(value or "")


async def _api_hosting(request):
    """GET: 返回图床全部配置。"""
    try:
        return web.json_response({"success": True, "data": _hosting_cfg()})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})


def _chahua_model_ready() -> bool:
    """检测模型是否可用: 已加载实例 或 nudenet 包已安装 (惰性加载, 未加载也算就绪)"""
    if _nsfw_detector is not None:
        return True
    try:
        import importlib.util
        return importlib.util.find_spec("nudenet") is not None
    except Exception:  # noqa: BLE001
        return False


async def _api_chahua_stats(request):
    """GET: 插画图库采集与检测统计 (Web 首页展示)。"""
    try:
        cards = _chahua_load_cards()
        now = time.time()
        next_in = 0
        task = _chahua_refresh_task
        if task and not task.done():
            if _chahua_stats["last_refresh"]:
                next_in = _CHAHUA_REFRESH_INTERVAL - (now - _chahua_stats["last_refresh"])
            else:
                next_in = _CHAHUA_REFRESH_FIRST_DELAY - (now - 0)
                if next_in < 0:
                    next_in = 0
            if next_in < 0:
                next_in = 0
        return web.json_response({"success": True, "data": {
            "cards": len(cards),
            "blacklist": len(_chahua_blacklist),
            "last_refresh": _chahua_stats["last_refresh"],
            "refresh_result": _chahua_stats["refresh_result"],
            "refresh_running": _chahua_stats["refresh_running"],
            "next_refresh_in": int(next_in),
            "detect_total": _chahua_stats["detect_total"],
            "detect_blocked": _chahua_stats["detect_blocked"],
            "violations": _chahua_stats["violations"],
            "model_ready": _chahua_model_ready(),
            "model_loaded": _nsfw_detector is not None,
            "interval_min": _CHAHUA_REFRESH_INTERVAL // 60,
        }})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)})


async def _api_save_hosting(request):
    """POST: body = {bed: {field: value}} 合并写回图床配置 yaml。"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"success": False, "error": "invalid json"}, status=400)
    try:
        data = _hosting_cfg()
        for bed, vals in (body or {}).items():
            if bed not in data or not isinstance(vals, dict):
                continue
            for k, v in vals.items():
                if k in data[bed]:
                    data[bed][k] = _coerce_value(data[bed][k], v)
        with open(_HOSTING_YAML, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return web.json_response({"success": True, "data": data})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def _api_redpacks(request):
    gid = _resolve_gid(request)
    packs = games.list_redpacks()
    data = []
    for x in packs:
        total = int(x.get("total") or 0)
        count = int(x.get("count") or 1)
        data.append({
            "id": x.get("id"),
            "sender_id": x.get("sender_id"),
            "sender_name": points.nick(x.get("sender_id")) if x.get("sender_id") else "",
            "total": total,
            "count": count,
            "remaining": int(x.get("remaining") or 0),
            "left": int(x.get("remaining") or 0),
            "amount": int(total / max(1, count)),
        })
    return web.json_response({"success": True, "data": data})

# 顶层 webpanel 对象 (供 @on_load 调用)
def _unregister_routes():
    global _registered
    _registered.clear()


from types import SimpleNamespace as _SNS
webpanel = _SNS(
    load_config=load_config,
    save_config=save_config,
    register_routes=register_routes,
    unregister_routes=_unregister_routes,
)


# ---------- from 图床.py ----------



from core.network.http_compat import AsyncHttpClient
from core.plugin.decorators import handler, interceptor, on_unload

_WAIT_TTL = 180.0
_GATHER_GAP = 6.0  # 批量收集窗口: 最后一条媒体后静默 N 秒才汇总输出
_waiting: dict[str, float] = {}  # key -> 等待超时时间戳
_batches: dict[str, dict] = {}   # key -> {'event': event, 'items': [(url, is_image, kind)], 'last': float}
_MESSAGE_EVENTS = (
    'GROUP_AT_MESSAGE_CREATE',
    'GROUP_MESSAGE_CREATE',
    'C2C_MESSAGE_CREATE',
    'DIRECT_MESSAGE_CREATE',
)
_client: AsyncHttpClient | None = None


async def _http() -> AsyncHttpClient:
    global _client
    if _client is None or _client.is_closed:
        _client = AsyncHttpClient(timeout=120.0)
    return _client


@on_unload
async def _cleanup():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


def _wait_key(event) -> str:
    if getattr(event, 'is_group', False) and getattr(event, 'group_id', None):
        return f'g:{event.group_id}:{event.user_id}'
    return f'u:{event.user_id}'


def _bed_at(event) -> str:
    """头像 + @ 前缀 (与娱乐助手风格一致)"""
    uid = str(getattr(event, 'raw_user_id', None) or getattr(event, 'user_id', '') or '').strip()
    if not uid:
        return ''
    appid = str(getattr(event, 'appid', '') or '100000000')
    avatar = f"https://q.qlogo.cn/qqapp/{appid}/{uid}/640"
    return f"![头像 #30px #30px]({avatar}) <@{uid}>\n\n"


def _get_bot(event):
    from core.application import get_app
    app = get_app()
    if not app:
        return None
    return app.get_bot(getattr(event, 'appid', None))


def _get_hosting():
    from core.application import get_app
    app = get_app()
    if not app:
        return None
    mm = getattr(app, 'module_manager', None)
    return mm.get('image_hosting') if mm else None


def _media_from_event(event):
    items = []
    for att in getattr(event, 'attachments', None) or []:
        if not isinstance(att, dict):
            continue
        url = att.get('url')
        if not isinstance(url, str) or not url:
            continue
        ct = str(att.get('content_type') or '').lower()
        clean = url.split('?')[0].lower()
        if ct.startswith('video') or clean.endswith(
            ('.mp4', '.webm', '.mov', '.avi', '.mkv', '.flv')):
            items.append((url, False, 'video'))
        elif ct.startswith('image') or clean.endswith(
            ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')):
            items.append((url, True, 'image'))
    if not items and getattr(event, 'image_url', None):
        items.append((str(event.image_url), True, 'image'))
    return items


async def _download(url: str) -> bytes:
    c = await _http()
    headers = {'User-Agent': 'Mozilla/5.0'}
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname
    if host:
        headers['Referer'] = f'https://{host}/'
    resp = await c.get(url, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f'媒体下载失败 HTTP {resp.status_code}')
    return resp.content


async def _upload_file(event, url, is_image, kind) -> str:
    data = await _download(url)
    hosting = _get_hosting()
    if hosting is None:
        raise RuntimeError('未启用 image_hosting 图床模块')
    bot = _get_bot(event)
    # 从 URL 提取真实扩展名 (视频 mp4/webm/mov/avi/mkv/flv; 图片 png/jpg/gif/webp/bmp),
    # 否则无扩展名文件会被部分图床 (如 Chevereto) 拒收
    from urllib.parse import urlsplit
    path = urlsplit(url).path
    ext = path.rsplit('.', 1)[-1].lower() if '.' in path else ''
    if ext not in ('png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'mp4', 'webm', 'mov', 'avi', 'mkv', 'flv'):
        ext = 'mp4' if not is_image else 'png'
    filename = f'upload_{int(time.time())}_{kind}.{ext}'
    # relay=False: 返回图床原始链接 (不中转自身图床)
    return await hosting.upload_any(
        data, filename,
        token_manager=bot.token_manager if bot else None,
        relay=False,
    )


@handler(r'^图床$', name='图床上传', desc='发送图床后等待图片/视频上传', ignore_at_check=True)
async def start_wait(event, match=None):
    uid = str(event.user_id)
    _waiting[_wait_key(event)] = time.monotonic()
    return await event.reply(
        f'{_bed_at(event)}请发送图片或视频（180秒内有效），发送后自动上传；发送「取消」可退出等待。')


@handler(r'^取消$', name='取消等待', desc='取消当前图床等待会话', ignore_at_check=True)
async def cancel_wait(event, match=None):
    key = _wait_key(event)
    _waiting.pop(key, None)
    _batches.pop(key, None)
    return await event.reply(f'{_bed_at(event)}已取消等待上传。')


_flush_tasks: set = set()  # 持有后台任务引用, 防止被 GC 回收


async def _flush_batch(key: str):
    """汇总输出一批已收集的媒体上传结果: 一条消息, 链接之间用 ↓ 分隔。"""
    batch = _batches.pop(key, None)
    _waiting.pop(key, None)
    if not batch or not batch['items']:
        return
    event = batch['event']
    uid = str(event.user_id)
    results = []
    for i, (url, is_image, kind) in enumerate(batch['items'], 1):
        try:
            result = await _upload_file(event, url, is_image, f'{kind}_{i}')
            results.append(str(result))
        except Exception as exc:
            results.append(f'第 {i} 个上传失败：{type(exc).__name__}: {exc}')
    try:
        # 标题一行, 链接之间用 ↓ 分隔 (标题下不跟 ↓)
        parts = [f'{_bed_at(event)}收到 {len(batch["items"])} 个媒体，上传成功']
        if results:
            parts.append('\n↓\n'.join(results))
        await event.reply('\n'.join(parts))
    except Exception:
        pass


async def _maybe_flush(key: str):
    """静默窗口结束后若没有新媒体则汇总输出; 期间收到新媒体则继续等 (自续循环)。"""
    try:
        while True:
            await asyncio.sleep(_GATHER_GAP)
            batch = _batches.get(key)
            if not batch:
                return
            if time.monotonic() - batch.get('last', 0) < _GATHER_GAP - 1:
                continue  # 刚又收到新媒体, 继续等
            await _flush_batch(key)
            return
    finally:
        batch = _batches.get(key)
        if batch:
            batch['task'] = None


@interceptor(priority=85)
async def catch_pending_media(event):
    """会话等待期内拦截随后的媒体消息: 批量收集, 静默窗口结束后统一上传输出。"""
    if event.event_type not in _MESSAGE_EVENTS:
        return False
    key = _wait_key(event)
    until = _waiting.get(key)
    if not until:
        return False
    now = time.monotonic()
    if now - until > _WAIT_TTL:
        _waiting.pop(key, None)
        _batches.pop(key, None)
        return False
    items = _media_from_event(event)
    if not items:
        return False

    # 收集: 延长等待时间, 追加到批次
    _waiting[key] = now + _WAIT_TTL
    batch = _batches.setdefault(key, {'event': event, 'items': [], 'task': None})
    batch['items'].extend(items)
    batch['last'] = now
    # 仅当没有正在运行的任务时创建汇总任务 (防止重复循环)
    if batch.get('task') is None or batch['task'].done():
        task = asyncio.get_running_loop().create_task(_maybe_flush(key))
        batch['task'] = task
        _flush_tasks.add(task)
        task.add_done_callback(_flush_tasks.discard)
    return True

# ---------- from 域名信息.py ----------




from core.network.http_compat import AsyncHttpClient
from core.plugin.decorators import handler, on_unload

_API = "http://api.qwq.nki.pw/API/Tools/Web/Whois.php"
_BTN = [[{"text": "再查一个", "data": "域名", "enter": False, "style": 1}]]

# 提取纯域名（去掉 http:// https:// / 路径等）
_DOMAIN_RE = re.compile(r"^(https?://)?(www\.)?([^/\s]+)", re.IGNORECASE)

_client: AsyncHttpClient | None = None


async def _whois_http():
    global _client
    if _client is None or _client.is_closed:
        _client = AsyncHttpClient(timeout=15.0)
    return _client


@on_unload
async def _cleanup():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None


def _extract_domain(raw: str) -> str | None:
    """从输入中提取域名"""
    raw = raw.strip()
    if not raw:
        return None
    # 直接匹配域名格式
    m = _DOMAIN_RE.match(raw)
    if m:
        return m.group(3).rstrip(".")
    # 简单检查是否像域名
    if "." in raw and not any(c in raw for c in (" ", "/", "\\", "\"", "'")):
        return raw.rstrip(".")
    return None


@handler(r"^域名(.*)$", name="域名信息", desc="查询域名Whois信息", ignore_at_check=True)
@handler(r"^whois(.*)$", name="whois", desc="查询域名Whois信息（别名）", ignore_at_check=True)
async def query_whois(event, match):
    uid = str(event.user_id)
    raw = match.group(1).strip()
    if not raw:
        return await event.reply(
            f"<@{uid}>🌐 **域名信息查询**\n\n"
            f"> 📌 用法：`域名 example.com`\n"
            f"> 📌 也支持 `whois xxx` / `域名 www.xxx.com`\n"
            f"> 📌 可输入完整 URL 自动提取域名",
            buttons=_BTN,
        )

    domain = _extract_domain(raw)
    if not domain:
        return await event.reply(
            f"<@{uid}>⚠️ 无法识别域名「{raw[:30]}」，请输入有效域名如 `baidu.com`",
            buttons=_BTN,
        )

    try:
        c = await _whois_http()
        resp = await c.get(f"{_API}?domain={urllib.parse.quote(domain)}")
        result = resp.json()
    except Exception:
        return await event.reply(
            f"<@{uid}>⚠️ 请求超时，请稍后重试",
            buttons=_BTN,
        )

    code = result.get("code") if isinstance(result, dict) else None
    msg = result.get("msg", "") if isinstance(result, dict) else ""
    data = result.get("data", {}) if isinstance(result, dict) else {}

    if code != 200 or not data:
        err_msg = msg or "未知错误"
        return await event.reply(
            f"<@{uid}>❌ 查询失败：{err_msg}",
            buttons=_BTN,
        )

    # ---- 格式化输出（卡片风格） ----
    d = data
    reg_days = d.get("已注册天数", "?")
    left_days = d.get("剩余天数", "?")
    expired = d.get("是否过期", "?")
    is_ok = expired == "未过期"
    badge = "🟢 正常" if is_ok else "🔴 已过期"

    # 标题行：域名 + 状态徽章
    header = f"<@{uid}>🌐 **{d.get('域名', domain)}** `{badge}`"

    # 信息区：用引用块包裹，紧凑排列
    info_parts = [
        f"📋 **注册商**  {d.get('注册商', '未知')}",
        f"📅 **注册**  {d.get('注册时间', '?')}",
        f"⏰ **到期**  {d.get('到期时间', '?')}",
        f"⏱️ **已注册 {reg_days}天** ｜ **剩余 {left_days}天**",
    ]

    # DNS 服务器
    dns_list = d.get("DNS服务器", [])
    if dns_list:
        dns_str = "  ".join(f"`{ns}`" for ns in dns_list[:4])
        if len(dns_list) > 4:
            dns_str += f"  +{len(dns_list)-4}"
        info_parts.append(f"🌍 **DNS**  {dns_str}")

    # 域名状态 → 只在有实际限制时显示（过滤掉"正常"/"ok"等无信息状态）
    status_list = d.get("域名状态", [])
    meaningful = []
    skip_words = ("正常", "ok", "active", "good", "未过期")
    for s in status_list:
        if "（" in s and "）" in s:
            short = s.split("（")[1].split("）")[0] if "）" in s.split("（")[1] else s
        else:
            short = s
        if short.lower() not in skip_words and short.strip():
            meaningful.append(short)
    if meaningful:
        tags = "  ".join(f"`{t}`" for t in meaningful[:6])
        if len(meaningful) > 6:
            tags += f"  ...+{len(meaningful)-6}"
        info_parts.append(f"🔒 **状态**  {tags}")

    body = "\n".join(info_parts)
    await event.reply(f"{header}\n\n> {body}", buttons=_BTN)



# ===== 自动构建 games / points 命名空间 (合并后模块对象) =====
import re as _re
import types as _types


def _collect_module_ns(prefix: str, src: str):
    """收集源码中 prefix.xxx 引用的所有 xxx 全局函数/变量, 构建命名空间对象"""
    ns = {}
    for _m in _re.finditer(rf'\b{prefix}\.(\w+)', src):
        _n = _m.group(1)
        if _n not in ns and _n in globals():
            ns[_n] = globals()[_n]
    return _types.SimpleNamespace(**ns)


_src_text = open(__file__, encoding='utf-8').read()
games = _collect_module_ns('games', _src_text)
points = _collect_module_ns('points', _src_text)


# ============ 插画 (mikagogo P站美图) ============
# 数据源: https://mikagogo.com/vip-illustration (本地 JSON, 每小时自动刷新)
# 图片: 直链 townimg.com (httpx 下载直接发图, 不用图床)
# 指令: 插画 / 插画 二次元随机
# 过滤: 尺寸≥500、webp≥120KB/jpg≥60KB、色彩数>400 (排除空白/纯色/压缩糊图)
_CHAHUA_JSON = os.path.join(_PLUGIN_DIR, "data", "mikagogo_1_25.json")

_CHAHUA_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

_chahua_cards: list = []
_chahua_cards_loaded = False
_chahua_cards_mtime = 0


# ============ 插画内容安全 (违规防护) ============
# 目标: 降低 QQ 内容审核(40034006)触发率, 违规后自动换图并冷却, 避免高频踩雷被封
# 1) 发送前: 本地 nudenet NSFW 检测, 暴露/擦边图直接跳过
# 2) 发送后: 违规图 URL 进黑名单(持久化), 自动换图重试(最多3轮)
# 3) 连续违规 3 次 → 指令冷却 10 分钟; 每日 30 次 / 每用户 15 秒频控
_CHAHUA_BLACKLIST_PATH = os.path.join(_PLUGIN_DIR, "data", "chahua_blacklist.json")
_CHAHUA_VIOLATION_LIMIT = 3       # 连续违规次数阈值
_CHAHUA_COOLDOWN_SEC = 10 * 60    # 违规冷却时长 (秒)
_CHAHUA_BURST_LIMIT = 5           # 每用户每 5 次为一组
_CHAHUA_BURST_COOLDOWN = 15       # 发满 5 次后限制 15 秒, 然后解除
_NSFW_HARD_CLASSES = frozenset({  # nudenet 3.x 敏感暴露类别 → QQ 必拦, 直接跳过
    "female_breast_exposed", "female_genitalia_exposed", "male_genitalia_exposed",
    "anus_exposed", "buttocks_exposed", "male_breast_exposed",
})
_NSFW_HARD_TH = 0.85              # 硬过滤阈值: 只拦最高置信度暴露 (QQ 必拦级), 其余交给 QQ 审核+黑名单兜底
_REVEAL_CLASSES = frozenset({     # 清凉信号: 露腋/露肚/露腿
    "armpits_exposed", "belly_exposed", "feet_exposed",
})
_REVEAL_MIN = 0.40                # 清凉判定阈值: 任一信号 ≥ 此值视为"不严实"

# 插画统计 (供 Web 首页展示)
_chahua_stats = {
    "detect_total": 0,        # AI 检测总次数
    "detect_blocked": 0,      # AI 拦截数 (最高置信暴露)
    "violations": 0,          # QQ 违规总次数
    "last_refresh": 0.0,      # 最近一次采集完成时间戳
    "refresh_result": "",     # 最近采集结果描述
    "refresh_running": False, # 采集进行中
}

_chahua_blacklist: set = set()
_chahua_blacklist_loaded = False
_chahua_violations = 0
_chahua_cooldown_until = 0.0
_chahua_user_count: dict = {}          # uid -> 当前组内已发次数
_chahua_user_lock_until: dict = {}     # uid -> 组间 15 秒锁定解除时间
_nsfw_detector = None
_nsfw_lock = threading.Lock()
_chahua_send_url = contextvars.ContextVar("chahua_send_url", default="")
_chahua_send_outcome = contextvars.ContextVar("chahua_send_outcome", default="")
_CHAHUA_HOOK_OWNER = "娱乐助手/插画"


def _chahua_load_blacklist():
    global _chahua_blacklist, _chahua_blacklist_loaded
    try:
        with open(_CHAHUA_BLACKLIST_PATH, encoding="utf-8") as f:
            _chahua_blacklist = set(json.load(f).get("urls", []))
        _chahua_blacklist_loaded = True
    except Exception:  # noqa: BLE001
        _chahua_blacklist = set()


def _chahua_save_blacklist():
    try:
        with open(_CHAHUA_BLACKLIST_PATH, "w", encoding="utf-8") as f:
            json.dump({"urls": sorted(_chahua_blacklist)}, f, ensure_ascii=False, indent=1)
    except Exception as e:  # noqa: BLE001
        log.warning("插画黑名单保存失败: %s", e)


def _chahua_is_blacklisted(url: str) -> bool:
    if not _chahua_blacklist_loaded:
        _chahua_load_blacklist()
    return url in _chahua_blacklist


def _chahua_mark_violation(url: str):
    """违规图入黑名单 + 连续违规计数, 超限进入冷却"""
    global _chahua_violations, _chahua_cooldown_until
    _chahua_stats["violations"] += 1
    if url:
        _chahua_blacklist.add(url)
        _chahua_save_blacklist()
    _chahua_violations += 1
    if _chahua_violations >= _CHAHUA_VIOLATION_LIMIT:
        _chahua_cooldown_until = time.time() + _CHAHUA_COOLDOWN_SEC
        _chahua_violations = 0
        log.warning("插画连续违规 %d 次, 指令冷却 %d 分钟", _CHAHUA_VIOLATION_LIMIT, _CHAHUA_COOLDOWN_SEC // 60)
    else:
        log.warning("插画图片违规(%d/%d): %s", _chahua_violations, _CHAHUA_VIOLATION_LIMIT, url[:80])


def _chahua_in_cooldown() -> bool:
    return time.time() < _chahua_cooldown_until


def _chahua_check_rate(uid: str) -> str | None:
    """频控: 不限总次数; 每用户每发满 5 次, 限制 15 秒后自动解除。
    返回 None 通过, 否则返回拒绝提示文案"""
    now = time.time()
    lock_until = _chahua_user_lock_until.get(uid, 0)
    if now < lock_until:
        return f"⏳ 插画发太快啦，请 {int(lock_until - now) + 1} 秒后再试"
    cnt = _chahua_user_count.get(uid, 0) + 1
    if cnt >= _CHAHUA_BURST_LIMIT:
        # 发满 5 次 → 锁定 15 秒, 计数清零; 本次放行
        _chahua_user_lock_until[uid] = now + _CHAHUA_BURST_COOLDOWN
        _chahua_user_count[uid] = 0
        return None
    _chahua_user_count[uid] = cnt
    return None


def _chahua_analyze(data: bytes):
    """本地检测: 返回 (is_nsfw, reveal_score)
    - is_nsfw: 敏感暴露(QQ必拦) → True 需跳过
    - reveal_score: 清凉度 0~1, 越高越清凉 (露腋/露肚/露腿信号最大值)
    检测失败/模型不可用 → (False, 0.0) 放行, 不阻塞发图"""
    global _nsfw_detector
    try:
        with _nsfw_lock:
            if _nsfw_detector is None:
                from nudenet import NudeDetector
                _nsfw_detector = NudeDetector()
        tmp = os.path.join(tempfile.gettempdir(), f"chahua_nsfw_{int(time.time() * 1000)}.jpg")
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            results = _nsfw_detector.detect(tmp)
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)
        if not results:
            return False, 0.0
        nsfw = False
        reveal = 0.0
        for item in results:
            cls = str(item.get("class", "")).lower()
            score = float(item.get("score", 0) or 0)
            if cls in _NSFW_HARD_CLASSES and score >= _NSFW_HARD_TH:
                nsfw = True
            if cls in _REVEAL_CLASSES and score > reveal:
                reveal = score
        # 统计
        _chahua_stats["detect_total"] += 1
        if nsfw:
            _chahua_stats["detect_blocked"] += 1
        return nsfw, reveal
    except Exception as e:  # noqa: BLE001
        log.warning("插画 NSFW 检测异常(放行): %s", e)
        return False, 0.0


def _chahua_is_nsfw(data: bytes) -> bool:
    """兼容入口: 是否敏感暴露 (QQ必拦级别)"""
    nsfw, _ = _chahua_analyze(data)
    return nsfw


async def _on_send_failed(data):
    """send_failed 钩子: 插画图片违规被拦截时静默记录黑名单, 不弹框架错误模板"""
    code = data.get("code") or ""
    resp = data.get("data") or {}
    err_code = resp.get("err_code") if isinstance(resp, dict) else None
    if code != 40034006 and err_code != 40034006:
        return data  # 非违规, 不处理
    url = _chahua_send_url.get()
    if not url:
        return data  # 不是插画发起的发送
    _chahua_mark_violation(url)
    _chahua_send_outcome.set("violation")  # 跨 contextvar 通知指令内换图重试
    log.info("插画违规已被静默处理(黑名单+自动换图)")
    return None  # 已处理 → 框架不弹 api_error 模板


def _chahua_register_hooks():
    try:
        from core.module.hook import get_hook_manager
        hm = get_hook_manager()
        hm.unregister_owner(_CHAHUA_HOOK_OWNER)  # 热重载防重复注册
        hm.register("send_failed", _on_send_failed, owner=_CHAHUA_HOOK_OWNER, priority=90)
    except Exception as e:  # noqa: BLE001
        log.warning("插画 send_failed 钩子注册失败: %s", e)


_chahua_register_hooks()


def _chahua_load_cards() -> list:
    global _chahua_cards, _chahua_cards_loaded, _chahua_cards_mtime
    try:
        mtime = os.path.getmtime(_CHAHUA_JSON)
    except OSError:
        return _chahua_cards
    if _chahua_cards_loaded and _chahua_cards and mtime == _chahua_cards_mtime:
        return _chahua_cards
    try:
        with open(_CHAHUA_JSON, encoding="utf-8") as f:
            data = json.load(f)
        _chahua_cards = [c for c in data.get("cards", []) if c.get("img")]
        _chahua_cards_mtime = mtime
        _chahua_cards_loaded = True
        log.info("插画图库已加载: %d 张图", len(_chahua_cards))
    except Exception as e:  # noqa: BLE001
        log.warning("插画图库加载失败: %s", e)
    return _chahua_cards


async def _chahua_download(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(headers=_CHAHUA_UA, timeout=20) as c:
            r = await c.get(url)
            if r.status_code == 200 and len(r.content) >= 1000:
                return r.content
    except Exception as e:  # noqa: BLE001
        log.warning("插画图片下载失败: %s", e)
    return None


def _chahua_has_content(data: bytes) -> bool:
    """质量过滤: 尺寸≥500、webp≥120KB/jpg≥60KB、色彩数>400 (排除压缩糊图/空白)
    numpy 缺失时自动降级: 仅做尺寸/大小过滤, 不影响发图"""
    try:
        if len(data) < 30000:
            return False
        is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        if is_webp and len(data) < 120000:
            return False  # webp 高压缩小文件 → 糊
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        if w < 500 or h < 500:
            return False
        try:
            import numpy as np
        except Exception:  # noqa: BLE001
            return True  # numpy 缺失: 跳过色彩统计
        arr = np.asarray(img.resize((64, 64)))
        uniq = len(np.unique(arr.reshape(-1, 3), axis=0))
        return uniq > 400
    except Exception:  # noqa: BLE001
        return False


def _chahua_to_jpeg(data: bytes) -> bytes:
    """webp → JPEG (QQ 兼容); 其他格式原样返回"""
    try:
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            from PIL import Image
            img = Image.open(io.BytesIO(data)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88)
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        pass
    return data


def _chahua_truncate_title(title: str, n: int = 30) -> str:
    """标题截断, 避免内容溢出"""
    t = (title or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[:n - 1] + "…"


async def _chahua_upload(data: bytes) -> str | None:
    """走图床服务模块上传: upload_any 按启用顺序尝试, 第一个失败自动切备用床;
    picgo.net 等被 QQ 拦截域名自动转存自身图床 (relay=True)。
    全部图床失败返回 None, 由调用方回退直传。"""
    try:
        hosting = _get_hosting()
        if hosting is None:
            log.warning("插画图床: image_hosting 模块未加载")
            return None
        url = await hosting.upload_any(
            data, f"chahua_{int(time.time())}.jpg",
            token_manager=None, sender=None, relay=True,
        )
        if url:
            log.info("插画图床上传成功: %s", url[:80])
        else:
            log.warning("插画图床: 所有图床均上传失败")
        return url
    except Exception as e:  # noqa: BLE001
        log.warning("插画图床上传异常: %s", e)
        return None


async def _chahua_pick(max_try: int = 10, require_reveal: bool = True):
    """随机取一张图: 优先清凉(露腋/露肚/露腿)且不违规; 找不到则退回不违规的普通图。
    返回 (jpeg, title, url); 无可用图返回 (None, None, None)"""
    cards = _chahua_load_cards()
    if not cards:
        return None, None, None
    hi = [c for c in cards if not c["img"].endswith(".webp")]
    pool = list(hi if len(hi) >= max_try // 2 else cards)
    random.shuffle(pool)
    # 第一轮: 优先清凉图 (非违规 + 清凉分达标)
    for card in pool[:max_try]:
        url = card["img"]
        if _chahua_is_blacklisted(url):
            continue
        data = await _chahua_download(url)
        if not data or not _chahua_has_content(data):
            continue
        nsfw, reveal = _chahua_analyze(data)
        if nsfw:
            log.info("插画跳过违规图: %s", url[:80])
            continue
        if require_reveal and reveal < _REVEAL_MIN:
            continue  # 太严实, 第一轮跳过 (等第二轮兜底)
        return _chahua_to_jpeg(data), card.get("title", ""), url
    # 兜底: 只要不违规就收 (保证出图, 穿得严实也发)
    for card in random.sample(cards, min(8, len(cards))):
        url = card["img"]
        if _chahua_is_blacklisted(url):
            continue
        data = await _chahua_download(url)
        if data and len(data) >= 30000:
            nsfw, _ = _chahua_analyze(data)
            if nsfw:
                continue
            return _chahua_to_jpeg(data), card.get("title", ""), url
    return None, None, None


@handler(
    r"^/?插画(?:\s+二次元随机)?\s*$",
    name="插画",
    desc="插画 二次元随机 → 随机一张二次元插画 (mikagogo)",
    ignore_at_check=True,
    priority=50,
    block=True,
)
async def cmd_illustration(event, match):
    if _chahua_in_cooldown():
        return await event.reply("🛡️ 插画内容正在审核冷却中，请 10 分钟后再试")
    uid = _uid(event)
    rate_msg = _chahua_check_rate(uid)
    if rate_msg:
        return await event.reply(rate_msg)
    # 提示: 正在检测 (选图/检测/上传可能需数秒)
    try:
        await event.reply("⚠️ 图片正在检测中，请稍等…")
    except Exception:  # noqa: BLE001
        pass
    # 最多 3 轮: 违规后自动换图重试
    for attempt in range(3):
        jpeg, title, url = await _chahua_pick()
        if not jpeg:
            if attempt == 0:
                return await event.reply("❌ 暂时没找到合适的图，再试一次吧")
            break
        short = _chahua_truncate_title(title, 30)
        try:
            # 重置错误标记 + 违规标记, 避免上一轮残留导致误判
            with contextlib.suppress(Exception):
                event.error = None
            out_token = _chahua_send_outcome.set("")
            # 标记当前发送的图 URL (send_failed 钩子据此识别插画违规)
            token = _chahua_send_url.set(url)
            try:
                img_url = await _chahua_upload(jpeg)
                if img_url:
                    await event.reply_image(img_url, content=short)
                else:
                    await event.reply_image(jpeg, content=short)
            finally:
                _chahua_send_url.reset(token)
                _chahua_send_outcome.reset(out_token)
        except Exception as e:  # noqa: BLE001
            log.warning("插画发图失败: %s", e)
            return await event.reply("❌ 图片发送失败，再试一次吧")
        # 检查发送是否违规被拦截 (双保险: send_failed 钩子标记 + event.error 字段)
        outcome = _chahua_send_outcome.get()
        err = getattr(event, "error", None)
        err_code = err.get("code") if isinstance(err, dict) else None
        err_nested = (err.get("data") or {}).get("err_code") if isinstance(err, dict) else None
        is_violation = (
            outcome == "violation"
            or err_code == 40034006
            or err_nested == 40034006
        )
        if is_violation:
            log.info("插画违规已换图重试 (第 %d 轮)", attempt + 1)
            continue
        if isinstance(err, dict) and err:
            # 非违规发送失败 → 明确提示, 不静默
            log.warning("插画发送失败: %s", err)
            return await event.reply("❌ 图片发送失败，再试一次吧")
        return None
    return await event.reply("🛡️ 图片内容被平台拦截，已自动换图仍失败，请稍后再试")


# ============ 插画图库自动采集与定时更新 (内置, 免外部脚本) ============
# 别人安装后无需任何外部脚本: 插件每小时自动采集 mikagogo 更新图库。
# playwright 为可选依赖: 未安装时自动跳过更新, 图库保持已有数据, 不影响发图。
_chahua_refresh_task = None
_chahua_refresh_lock = asyncio.Lock()
_CHAHUA_REFRESH_INTERVAL = 3600         # 更新周期: 每小时
_CHAHUA_REFRESH_FIRST_DELAY = 300       # 插件加载后 5 分钟首次采集
_CHAHUA_LIST_PAGES = 25                 # 采集列表页数
_CHAHUA_LIST_BASE = "https://mikagogo.com/vip-illustration/page"
_CHAHUA_DETAIL_IMG_RE = re.compile(
    r"https://(?:cc|bu|mk)-img\.townimg\.com/uploads/[^\"'\s]+\.(?:webp|jpg|jpeg|png)"
)
_CHAHUA_THUMB_RE = re.compile(r"-\d+x\d+\.")


def _chahua_start_refresh_task():
    global _chahua_refresh_task
    if _chahua_refresh_task and not _chahua_refresh_task.done():
        return
    _chahua_refresh_task = asyncio.ensure_future(_chahua_refresh_loop())


def _chahua_stop_refresh_task():
    global _chahua_refresh_task
    task = _chahua_refresh_task
    _chahua_refresh_task = None
    if task and not task.done():
        task.cancel()


async def _chahua_fetch_list_pages() -> list:
    """playwright 抓列表页 → [{url, title}]；playwright 不可用返回 []"""
    try:
        from playwright.async_api import async_playwright
    except Exception as e:  # noqa: BLE001
        log.warning("插画采集: playwright 未安装, 本轮跳过自动更新 (%s)", e)
        return []
    items = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = await browser.new_context(user_agent=_CHAHUA_UA["User-Agent"], viewport={"width": 1280, "height": 900}, locale="zh-CN")
            page = await ctx.new_page()
            for pg in range(1, _CHAHUA_LIST_PAGES + 1):
                try:
                    await page.goto(f"{_CHAHUA_LIST_BASE}/{pg}", wait_until="domcontentloaded", timeout=25000)
                    await page.wait_for_timeout(1000)
                    cards = await page.eval_on_selector_all("img.cardImage", """els => els.map(e => {
                        const a = e.closest('a');
                        return {href: a ? a.getAttribute('href') : '', title: e.getAttribute('alt') || ''};
                    })""")
                    for c in cards:
                        href = c["href"]
                        if href:
                            if not href.startswith("http"):
                                href = "https://mikagogo.com" + href
                            items.append({"url": href, "title": c["title"].strip()})
                    log.info("插画采集 page/%d: %d 条", pg, len(cards))
                except Exception as e:  # noqa: BLE001
                    log.warning("插画采集 page/%d 失败: %s", pg, str(e)[:80])
                await asyncio.sleep(1.2)
            await browser.close()
    except Exception as e:  # noqa: BLE001
        log.warning("插画采集: 浏览器启动失败, 本轮跳过 (%s)", e)
        return []
    seen, uniq = set(), []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"])
            uniq.append(it)
    return uniq


async def _chahua_fetch_detail_images(url: str) -> list:
    """httpx 抓详情页, 返回全部正文大图 URL (去重保序, 排除缩略图)"""
    try:
        async with httpx.AsyncClient(headers=_CHAHUA_UA, follow_redirects=True, timeout=15) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return []
            out = []
            for u in _CHAHUA_DETAIL_IMG_RE.findall(r.text):
                if not _CHAHUA_THUMB_RE.search(u) and "logo" not in u and u not in out:
                    out.append(u)
            return out
    except Exception:  # noqa: BLE001
        return []


async def _chahua_refresh_once():
    """执行一轮采集并原子更新图库 JSON; 任何失败不影响现有图库"""
    if _chahua_refresh_lock.locked():
        return
    async with _chahua_refresh_lock:
        _chahua_stats["refresh_running"] = True
        log.info("插画图库自动更新: 开始采集")
        try:
            items = await _chahua_fetch_list_pages()
            if not items:
                _chahua_stats["refresh_result"] = "列表采集为空, 本轮跳过"
                log.warning("插画图库自动更新: 列表采集为空, 本轮跳过")
                return
            sem = asyncio.Semaphore(8)
            cards = []

            async def one(it):
                async with sem:
                    imgs = await _chahua_fetch_detail_images(it["url"])
                    return [{"img": u, "title": it["title"], "post": it["url"]} for u in imgs]

            results = await asyncio.gather(*(one(it) for it in items))
            for rs in results:
                cards.extend(rs)
            seen, uniq = set(), []
            for c in cards:
                if c["img"] not in seen:
                    seen.add(c["img"])
                    uniq.append(c)
            payload = {"site": "https://mikagogo.com/vip-illustration", "pages": _CHAHUA_LIST_PAGES,
                       "count": len(uniq), "cards": uniq, "detail_only": True}
            try:
                os.makedirs(os.path.dirname(_CHAHUA_JSON), exist_ok=True)
                tmp = _CHAHUA_JSON + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                os.replace(tmp, _CHAHUA_JSON)  # 原子替换, 采集失败/中断不会损坏图库
                _chahua_stats["last_refresh"] = time.time()
                _chahua_stats["refresh_result"] = f"成功 {len(uniq)} 张 ({len(items)} 详情页)"
                log.info("插画图库自动更新完成: %d 张 (%d 个详情页)", len(uniq), len(items))
            except Exception as e:  # noqa: BLE001
                _chahua_stats["refresh_result"] = f"写入失败: {e}"
                log.warning("插画图库写入失败: %s", e)
        finally:
            _chahua_stats["refresh_running"] = False


async def _chahua_refresh_loop():
    """后台定时循环: 每小时自动更新图库"""
    try:
        await asyncio.sleep(_CHAHUA_REFRESH_FIRST_DELAY)
    except asyncio.CancelledError:
        return
    while True:
        try:
            await _chahua_refresh_once()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            log.warning("插画图库自动更新异常: %s", e)
        try:
            await asyncio.sleep(_CHAHUA_REFRESH_INTERVAL)
        except asyncio.CancelledError:
            return

# ---------- from 台风图.py (合并自 台风__1_.py, 替换旧台风) ----------
import asyncio
import hashlib
import io
import json
import os
import re
import time
import traceback
from datetime import datetime
from urllib.parse import quote

import aiohttp
from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_unload

_tf_log = get_logger(PLUGIN, '台风')

NMC = 'https://typhoon.nmc.cn/weatherservice/typhoon/jsons'
NMC_PUB = 'https://www.nmc.cn/publish/typhoon'
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
_IMG_DIR = os.path.join(DATA_DIR, 'official_img')
os.makedirs(_IMG_DIR, exist_ok=True)

_LEVEL_CN = {
    'TD': '热带低压', 'TS': '热带风暴', 'STS': '强热带风暴',
    'TY': '台风', 'STY': '强台风', 'SuperTY': '超强台风',
}
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
    'Referer': 'https://typhoon.nmc.cn/web.html',
    'Accept': '*/*',
}
_PUB_HEADERS = {**_HEADERS, 'Referer': f'{NMC_PUB}/probability.html'}
_PUB_PAGES = ('probability.html',) + tuple(f'probability-img{i}.html' for i in range(2, 9))
_SESSION = None
_SESSION_LOCK = asyncio.Lock()
_MEM, _LOCKS = {}, {}
_LIST_TTL, _VIEW_TTL, _MAP_TTL, _IMG_TTL = 60, 90, 180, 180  # 图：磁盘/图床 3 分钟
_PAGE_SIZE = 12
_FONT_CANDIDATES = (
    'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/msyh.ttf', 'C:/Windows/Fonts/simhei.ttf',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/System/Library/Fonts/PingFang.ttc',
)
_FONT_CACHE = {}
_INK, _MUTED, _LINE, _BG = (30, 41, 59), (100, 116, 139), (226, 232, 240), (248, 250, 252)
_LIVE, _WHITE = (185, 28, 28), (255, 255, 255)


def _cache_get(key):
    item = _MEM.get(key)
    if not item:
        return None
    exp, val = item
    if time.time() > exp:
        _MEM.pop(key, None)
        return None
    return val


def _cache_set(key, val, ttl):
    _MEM[key] = (time.time() + ttl, val)
    return val


async def _get_session():
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        return _SESSION
    async with _SESSION_LOCK:
        if _SESSION is not None and not _SESSION.closed:
            return _SESSION
        _SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False, limit=24, ttl_dns_cache=300, enable_cleanup_closed=True),
            timeout=aiohttp.ClientTimeout(total=12, connect=5),
            headers=_HEADERS,
        )
        return _SESSION


@on_unload
async def _tf_close_http():
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = None


def _btn(text, data, *, style=1, enter=True):
    item = {'text': str(text)[:16], 'data': str(data), 'type': 2, 'style': style}
    if enter:
        item['enter'] = True
    return item


def _nav_btns():
    return [
        [_btn('最强台风', '最强台风'), _btn('活跃台风', '活跃台风')],
        [_btn('台风列表', '台风列表'), _btn('台风帮助', '台风帮助', style=4)],
    ]


def _list_btns(year, page, pages):
    rows = []
    if pages > 1:
        row = []
        if page > 1:
            row.append(_btn('上一页', f'台风列表 {year} {page - 1}'))
        if page < pages:
            row.append(_btn('下一页', f'台风列表 {year} {page + 1}'))
        if row:
            rows.append(row)
    rows.extend(_nav_btns())
    return rows


def _chip(show, cmd):
    return (
        f'<qqbot-cmd-input text="{quote(str(cmd), safe="")}" '
        f'show="{quote(str(show), safe="")}" reference="false" />'
    )


def _year_bar(selected=None):
    now = datetime.now().year
    chips = [_chip(str(y), f'台风列表 {y}') for y in range(now, now - 6, -1)]
    rows = ['　　'.join(chips[i:i + 3]) for i in range(0, len(chips), 3)]
    return '📅 点选年份查看往年列表\n' + '\n'.join(rows)


def _ms(start):
    return int((time.time() - start) * 1000)


def _cmd_head(match):
    s = re.sub(r'^\s*/?', '', (match.group(0) or '')).strip()
    s = re.sub(r'\s*\d{4}\s*$', '', s)
    return s.split()[0] if s else ''


def _hint_query():
    return (
        '❗ 请补上名称、编号或年份。\n\n'
        '💡 示例：\n'
        '`台风查询 沙德尔`　按名称\n'
        '`台风查询 2411`　按编号\n'
        '`台风列表 2023`　查看该年名单'
    )


def _hint_year():
    return '❗ 请补上四位年份，例如：`台风列表 2023`'


def _hint_miss():
    return '❗ 查询不到，请正确使用。例如：`台风列表 2025`　`台风查询 沙德尔`'


def _hint_noarg(cmd=None):
    return _hint_miss()


def _parse_year(text):
    s = str(text or '').strip()
    if not re.fullmatch(r'(19|20)\d{2}', s):
        return None
    year = int(s)
    now = datetime.now().year
    if year < 1945 or year > now:
        return None
    return year


def _parse_page_token(text):
    s = str(text or '').strip()
    if re.fullmatch(r'(19|20)\d{2}', s):
        return None
    m = re.fullmatch(r'(?:p|第)?([1-9]\d?)页?', s, re.I)
    return int(m.group(1)) if m else None


def _parse_year_page(text, *, default_year=None):
    s = str(text or '').strip()
    page = 1
    if not s:
        return default_year, 1
    parts = s.split()
    tok = _parse_page_token(parts[-1])
    if tok is not None and (len(parts) > 1 or default_year is not None):
        page = tok
        parts = parts[:-1]
        s = ' '.join(parts).strip()
    if not s:
        return default_year, page
    m = re.fullmatch(r'((?:19|20)\d{2})(?:第([1-9]\d?)页)?', s)
    if m:
        year = _parse_year(m.group(1))
        if year is None:
            return None, None
        if m.group(2):
            page = int(m.group(2))
        return year, page
    year = _parse_year(s)
    if year is None:
        return None, None
    return year, page


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_time(v):
    s = str(v or '')
    return f'{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}' if len(s) >= 12 and s.isdigit() else s


def _avg_radius(wind_radius, code):
    for row in wind_radius or []:
        if not row or str(row[0]).upper() != code:
            continue
        vals = []
        for v in row[1:5]:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
        if vals:
            return round(sum(vals) / len(vals))
    return None


def parse_jsonp(text):
    text = (text or '').strip()
    m = re.search(r'^[^(]*\(\s*(\{.*\})\s*\)\s*;?\s*$', text, re.S)
    if m:
        return json.loads(m.group(1))
    i, j = text.find('{'), text.rfind('}')
    if i >= 0 and j > i:
        return json.loads(text[i:j + 1])
    raise ValueError('无法解析 JSONP')


def _clean_name(v):
    s = str(v or '').strip()
    return '' if (not s or s.lower() in ('null', 'none', 'nameless', '未知')) else s


def _wind_txt(wind):
    try:
        ms = float(wind)
        return f'{wind}m/s({ms * 3.6:.0f}km/h)'
    except (TypeError, ValueError):
        return f'{wind or "-"}m/s'


def _ok(result):
    if result in (False, None):
        return False
    if isinstance(result, tuple):
        return bool(result) and result[0] is True
    if isinstance(result, dict):
        return result.get('code') in (None, 0, '0')
    return bool(result)


def _lock(key):
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _disk_name(*parts):
    return hashlib.md5('|'.join(str(p) for p in parts).encode('utf-8')).hexdigest()


def _disk_load(name):
    path = os.path.join(_IMG_DIR, name)
    try:
        if not os.path.isfile(path) or time.time() - os.path.getmtime(path) > _IMG_TTL:
            return None
        with open(path, 'rb') as f:
            data = f.read()
        return data if data and len(data) > 800 else None
    except Exception:
        return None


def _disk_save(name, data):
    path = os.path.join(_IMG_DIR, name)
    try:
        with open(path, 'wb') as f:
            f.write(data)
        files = [os.path.join(_IMG_DIR, n) for n in os.listdir(_IMG_DIR) if n.endswith(('.jpg', '.bin'))]
        if len(files) > 48:
            files.sort(key=os.path.getmtime)
            for p in files[: len(files) - 36]:
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception as e:
        _tf_log.warning('图片缓存写入失败: %s', e)


def _info_pairs(view):
    pts = view.get('points') or []
    p = pts[-1] if pts else {}
    pairs = [
        ('编号', str(view.get('num') or '-')),
        ('状态', '活跃' if view.get('status') == 'start' else '停编'),
        ('强度', str(p.get('strong') or '-')),
        ('气压', f'{p.get("pressure", "-")} hPa'),
        ('风速', _wind_txt(p.get('wind'))),
        ('当前位置', f'{p.get("lat")}°N  {p.get("lng")}°E'),
    ]
    if p.get('move'):
        mv = str(p.get('move'))
        if p.get('movespeed') not in (None, ''):
            mv += f'  {p.get("movespeed")} km/h'
        pairs.append(('移向', mv))
    rparts = [f'{lab}{p.get(k)}km' for lab, k in (('7级', 'radius7'), ('10级', 'radius10'), ('12级', 'radius12')) if p.get(k)]
    if rparts:
        pairs.append(('风圈', ' / '.join(rparts)))
    if p.get('time'):
        pairs.append(('时间', f'{p.get("time")}（北京时）'))
    return pairs


def _forecast_line(view, sep='  ·  '):
    pts = view.get('points') or []
    fc = (pts[-1].get('forecasts') if pts else None) or []
    if not fc:
        return ''
    bits = sep.join(f'+{f.get("hour")}h {_LEVEL_CN.get(f.get("level"), f.get("level") or "")}' for f in fc[:5])
    return f'预报  {bits}'


def _defense_tips(view):
    pts = view.get('points') or []
    if view.get('status') == 'stop' or not pts:
        return []
    p = pts[-1]
    wind = _to_float(p.get('wind')) or 0
    level = str(p.get('level') or '')
    strong = p.get('strong') or _LEVEL_CN.get(level, '') or ''
    if wind >= 41 or level in ('STY', 'SuperTY', 'SUPERTY') or '强台风' in strong or '超强' in strong:
        return ['请留在坚固建筑物内，远离门窗玻璃', '停止户外及水上作业，服从转移安排', '备足饮用水与照明，避免涉水出行']
    if wind >= 24.5 or level in ('TY', 'STY', 'SuperTY') or '台风' in strong:
        return ['尽量减少外出，关闭门窗并收妥阳台物品', '远离工地、广告牌及高大树木', '低洼地区注意内涝，切勿涉水通行']
    return ['外出注意大风和强降雨', '关闭门窗，移走阳台易坠物', '积水勿趟，远离树木和广告牌']


async def _tf_http(url, *, binary=False, timeout=12, headers=None):
    try:
        session = await _get_session()
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout, connect=5), headers=headers or _HEADERS
        ) as resp:
            if resp.status != 200:
                return None
            return await (resp.read() if binary else resp.text())
    except Exception as e:
        _tf_log.warning('请求失败 %s: %s', url, e)
        return None


async def nmc_list(year=None):
    key = 'default' if year is None else str(year)
    ck = f'list:{key}'
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    text = await _tf_http(f'{NMC}/list_{key}?t={int(time.time() * 1000)}&callback=typhoon_jsons_list_{key}', timeout=10)
    if not text:
        return None
    try:
        data = parse_jsonp(text)
    except Exception as e:
        _tf_log.warning('解析列表失败: %s', e)
        return None
    rows = []
    for item in data.get('typhoonList') or []:
        if not isinstance(item, (list, tuple)) or len(item) < 8:
            continue
        rows.append({
            'id': item[0], 'en': _clean_name(item[1]), 'cn': _clean_name(item[2]),
            'num': str(item[3] or ''), 'status': item[7] or '',
        })
    return _cache_set(ck, {'year': year, 'list': rows}, _LIST_TTL)


async def nmc_view(tid):
    tid = str(tid)
    ck = f'view:{tid}'
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    text = await _tf_http(f'{NMC}/view_{tid}?t={int(time.time() * 1000)}&callback=typhoon_jsons_view_{tid}')
    if not text:
        return None
    try:
        data = parse_jsonp(text)
    except Exception as e:
        _tf_log.warning('解析详情失败: %s', e)
        return None
    ty = data.get('typhoon')
    if not isinstance(ty, (list, tuple)) or len(ty) < 9:
        return None
    points, stopped = [], str(ty[7] if len(ty) > 7 else '') == 'stop'
    for p in ty[8] or []:
        if not isinstance(p, (list, tuple)) or len(p) < 8:
            continue
        level, wr = str(p[3] or '').strip(), p[10] if len(p) > 10 else []
        forecasts, fcmap = [], p[11] if len(p) > 11 else None
        if isinstance(fcmap, dict) and not stopped:
            for f in fcmap.get('BABJ') or []:
                if not isinstance(f, (list, tuple)) or len(f) < 6:
                    continue
                forecasts.append({
                    'hour': f[0], 'lng': f[2], 'lat': f[3], 'pressure': f[4], 'wind': f[5],
                    'level': str(f[7] if len(f) > 7 else '').strip(),
                })
        points.append({
            'time': _fmt_time(p[1]), 'level': level,
            'strong': _LEVEL_CN.get(level, level or '-'),
            'lng': p[4], 'lat': p[5], 'pressure': p[6], 'wind': p[7],
            'move': p[8] if len(p) > 8 else '', 'movespeed': p[9] if len(p) > 9 else '',
            'radius7': _avg_radius(wr, '30KTS'), 'radius10': _avg_radius(wr, '50KTS'),
            'radius12': _avg_radius(wr, '64KTS'), 'forecasts': forecasts,
        })
    return _cache_set(ck, {
        'id': ty[0], 'en': _clean_name(ty[1]), 'cn': _clean_name(ty[2]),
        'num': str(ty[3] or ''), 'status': ty[7] if len(ty) > 7 else '', 'points': points,
    }, _VIEW_TTL)


def _match(item, keyword):
    kw = keyword.strip()
    if not kw:
        return False
    sid, num = str(item.get('id') or ''), str(item.get('num') or '')
    cn, en = str(item.get('cn') or ''), str(item.get('en') or '')
    if kw == sid or kw == num:
        return True
    if kw.isdigit() and num.isdigit():
        if kw == num or (len(kw) >= 4 and (num.endswith(kw[-4:]) or kw.endswith(num))):
            return True
        if len(kw) <= 2 and len(num) >= 2 and num.endswith(kw.zfill(2)):
            return True
    if len(kw) >= 2 and (kw in cn or (en and kw.lower() in en.lower())):
        return True
    return False


async def resolve_id(keyword):
    kw = keyword.strip()
    if not kw:
        return None, '请输入台风名称或编号，例如：台风查询 沙德尔'
    if re.fullmatch(r'\d{6,10}', kw):
        view = await nmc_view(kw)
        if view:
            return view['id'], None
    bundle = await nmc_list()
    if bundle is None:
        return None, '暂时无法获取台风数据，请稍后重试'
    for it in bundle['list']:
        if it['status'] == 'start' and _match(it, kw):
            return it['id'], None
    for it in bundle['list']:
        if _match(it, kw):
            return it['id'], None
    m = re.search(r'(19|20)\d{2}', kw)
    year = int(m.group(0)) if m else None
    if year:
        yb = await nmc_list(year)
        if yb:
            for it in yb['list']:
                if _match(it, kw):
                    return it['id'], None
    if kw.isdigit() and year is None:
        now_y = datetime.now().year
        for yb in await asyncio.gather(nmc_list(now_y), nmc_list(now_y - 1), return_exceptions=True):
            if isinstance(yb, Exception) or not yb:
                continue
            for it in yb['list']:
                if _match(it, kw):
                    return it['id'], None
    return None, '未查询到该台风'


def _parse_pub_page(html):
    html = html or ''
    m = re.search(r'<title>([^<]+)</title>', html, re.I)
    mt = re.search(r'路径预报[_．.\s]*([\u4e00-\u9fffA-Za-z0-9·]{2,20})', m.group(1) if m else '')
    imgs = re.findall(r'data-img="(https?://image\.nmc\.cn/product/[^"]+)"', html)
    if not imgs:
        imgs = re.findall(r'(https?://image\.nmc\.cn/product/[^"\'?\s]+TCBU[^"\'?\s]+\.(?:JPG|jpg|PNG|png))', html)
    latest = (imgs[0].split('?')[0].replace('/medium/', '/') if imgs else '')
    return {
        'cn': mt.group(1).strip() if mt else '',
        'img': latest,
        'codes': sorted(set(re.findall(r'0W\d{6,10}', html))),
    }


def _official_match(info, cn, num):
    icn = info.get('cn') or ''
    if cn and icn and (cn in icn or icn in cn):
        return True
    if not num:
        return False
    n4 = num[-4:] if len(num) >= 4 else num
    for c in info.get('codes') or []:
        if 'null' not in c.lower() and (num in c or (len(n4) >= 2 and n4 in c)):
            return True
    return False


async def nmc_official_maps():
    cached = _cache_get('official_maps')
    if cached is not None:
        return cached

    async def one(page):
        html = await _tf_http(f'{NMC_PUB}/{page}', timeout=8, headers=_PUB_HEADERS)
        if not html:
            return None
        info = _parse_pub_page(html)
        if not info.get('img'):
            return None
        cn, img = info.get('cn') or '', info.get('img') or ''
        if (cn in ('ll号台风', '号台风') or 'null' in img.lower()) and not any(
            'null' not in c.lower() for c in (info.get('codes') or [])
        ):
            return None
        return info

    parts = await asyncio.gather(*[one(p) for p in _PUB_PAGES], return_exceptions=True)
    out, seen = [], set()
    for info in parts:
        if isinstance(info, Exception) or not info or info['img'] in seen:
            continue
        seen.add(info['img'])
        out.append(info)
    return _cache_set('official_maps', out, _MAP_TTL)


async def fetch_official_track_png(view):
    if view.get('status') == 'stop':
        return None, ''
    cn, num = view.get('cn') or '', str(view.get('num') or '')
    maps = await nmc_official_maps()
    hit = next((i for i in maps if _official_match(i, cn, num)), None)
    if not hit:
        return None, ''
    src = hit.get('img') or ''
    name = _disk_name('rawo', src) + '.bin'
    async with _lock(src):
        data = _disk_load(name)
        if not data:
            data = await _tf_http(src, binary=True, timeout=18, headers=_PUB_HEADERS)
            if not data or len(data) < 2000:
                mid = src.replace('/TCBU/', '/TCBU/medium/')
                if mid != src and '/medium/medium/' not in mid:
                    data = await _tf_http(mid, binary=True, timeout=12, headers=_PUB_HEADERS)
            if data and len(data) >= 2000:
                _disk_save(name, data)
    return (data, src) if data and len(data) >= 2000 else (None, '')


def _name(event):
    n = str(getattr(event, 'username', '') or '').strip()
    if not n or n.isdigit() or (len(n) >= 18 and n.isalnum()):
        return '你'
    return n


def _avatar(event):
    for key in ('avatar', 'avatar_url', 'head_img', 'avatarUrl'):
        v = str(getattr(event, key, '') or '').strip()
        if v.startswith(('http://', 'https://')):
            return v
    appid = str(getattr(event, 'appid', '') or '').strip()
    oid = str(getattr(event, 'raw_user_id', None) or getattr(event, 'user_id', '') or '').strip()
    if appid and oid:
        return f'https://q.qlogo.cn/qqapp/{appid}/{oid}/100'
    return ''


def _font(size):
    size = int(size)
    hit = _FONT_CACHE.get(size)
    if hit is not None:
        return hit
    font = None
    try:
        from PIL import ImageFont
        for path in _FONT_CANDIDATES:
            if os.path.isfile(path):
                font = ImageFont.truetype(path, size=size)
                break
        if font is None:
            font = ImageFont.load_default()
    except Exception:
        font = None
    _FONT_CACHE[size] = font
    return font


def _tw(draw, text, font):
    text = text or ''
    if not font:
        return max(len(text) * 8, 1), 12
    if hasattr(draw, 'textbbox'):
        b = draw.textbbox((0, 0), text, font=font)
        return b[2] - b[0], b[3] - b[1]
    return draw.textsize(text, font=font)


def _metrics(W):
    W = max(int(W), 640)
    pad = max(10, min(16, W * 10 // 880))
    ft = max(22, min(34, W * 26 // 880))
    fs = max(20, min(32, W * 24 // 880))
    fm = max(16, min(24, W * 18 // 880))
    return pad, ft, fs, fm


def _jpeg(im):
    rgb = im.convert('RGB')
    buf = io.BytesIO()
    rgb.save(buf, format='JPEG', quality=92, subsampling=0, optimize=True)
    blob = buf.getvalue()
    if len(blob) > 4_000_000:
        buf = io.BytesIO()
        rgb.save(buf, format='JPEG', quality=84, subsampling=0, optimize=True)
        blob = buf.getvalue()
    return blob, rgb.size


def _wrap(draw, text, font, max_w):
    text = str(text or '')
    if not text:
        return ['']
    lines, cur = [], ''
    for ch in text:
        if ch == '\n':
            lines.append(cur)
            cur = ''
            continue
        nxt = cur + ch
        if cur and _tw(draw, nxt, font)[0] > max_w:
            lines.append(cur)
            cur = ch
        else:
            cur = nxt
    if cur or not lines:
        lines.append(cur)
    return lines


def _draw_title(draw, W, title, right, pad, ft, fm):
    _, th = _tw(draw, title or ' ', _font(ft))
    gap = 6
    hh = gap * 2 + th
    draw.rectangle((0, 0, W, hh), fill=_WHITE)
    draw.text((pad, gap), title, font=_font(ft), fill=_INK)
    if right:
        rw, rh = _tw(draw, right, _font(fm))
        draw.text((W - pad - rw, gap + max((th - rh) // 2, 0)), right, font=_font(fm), fill=_MUTED)
    draw.line((0, hh - 1, W, hh - 1), fill=_LINE)
    return hh


def compose_detail(map_blob, view, note=''):
    from PIL import Image, ImageDraw
    mp = None
    if map_blob:
        try:
            mp = Image.open(io.BytesIO(map_blob)).convert('RGB')
        except Exception:
            mp = None
    W = max(mp.size[0] if mp else 880, 800)
    pad, ft, fs, fm = _metrics(W)
    probe = ImageDraw.Draw(Image.new('RGB', (W, 8)))
    cn, en = view.get('cn') or '', view.get('en') or ''
    heading = cn or en or '台风'
    if en and cn and en != cn:
        heading = f'{cn}  {en}'
    if view.get('num'):
        heading = f'{heading}    {view.get("num")}'
    st = '活跃' if view.get('status') == 'start' else '停编'
    right = f'{st}' + (f'  {note}' if note else '')

    shorts, longs = [], []
    for k, v in _info_pairs(view):
        (longs if k in ('当前位置', '风圈', '时间') else shorts).append((k, str(v)))
    fc = _forecast_line(view, sep='  ')
    if fc:
        longs.append(('预报', fc.replace('预报  ', '', 1).strip()))
    tips = _defense_tips(view)

    col_w = (W - pad * 3) // 2
    short_lw = max((_tw(probe, k, _font(fs))[0] for k, _ in shorts), default=36) + 2
    inner = max(col_w - short_lw - 8, 40)
    kept, rest = [], []
    for k, v in shorts:
        (rest if _tw(probe, v, _font(fs))[0] > inner else kept).append((k, v))
    shorts, longs = kept, rest + longs
    long_blocks = []
    for k, v in longs:
        lw = _tw(probe, k, _font(fs))[0] + 6
        long_blocks.append((k, lw, _wrap(probe, v, _font(fs), W - pad * 2 - lw)))
    tip_blocks = [_wrap(probe, f'{i}. {t}', _font(fs), W - pad * 2) for i, t in enumerate(tips[:3], 1)]

    lh = _tw(probe, '字', _font(fs))[1] + 1
    mh = mp.size[1] if mp else 0
    canvas = Image.new('RGB', (W, 80 + mh + 1200), _BG)
    draw = ImageDraw.Draw(canvas)
    y = _draw_title(draw, W, heading, right, pad, ft, fm)
    if mp:
        canvas.paste(mp, ((W - mp.size[0]) // 2, y))
        y += mh
    y += 6
    for i in range(0, len(shorts), 2):
        for col, (k, v) in enumerate(shorts[i:i + 2]):
            x0 = pad + col * (col_w + pad)
            draw.text((x0, y), k, font=_font(fs), fill=_MUTED)
            draw.text((x0 + short_lw + 6, y), v, font=_font(fs), fill=_INK)
        y += lh
    for k, lw, wrapped in long_blocks:
        draw.text((pad, y), k, font=_font(fs), fill=_MUTED)
        vx = pad + lw
        for j, line in enumerate(wrapped):
            draw.text((vx, y + j * lh), line, font=_font(fs), fill=_INK)
        y += max(len(wrapped), 1) * lh
    if tip_blocks:
        y += 2
        draw.line((pad, y, W - pad, y), fill=_LINE)
        y += 4
        lab = '防护'
        draw.text((pad, y), lab, font=_font(fs), fill=_MUTED)
        vx = pad + _tw(probe, lab, _font(fs))[0] + 10
        for i, block in enumerate(tip_blocks):
            for j, line in enumerate(block):
                draw.text((vx, y), line, font=_font(fs), fill=_INK)
                y += lh
    return _jpeg(canvas.crop((0, 0, W, min(canvas.size[1], y + 8))))


def compose_card(spec, note=''):
    from PIL import Image, ImageDraw
    W = 880
    pad, ft, fs, fm = _metrics(W)
    probe = ImageDraw.Draw(Image.new('RGB', (W, 8)))
    kind = spec.get('kind') or 'lines'
    title = spec.get('title') or '台风'
    rows = spec.get('rows') or []
    lines = spec.get('lines') or []
    canvas = Image.new('RGB', (W, 3600), _BG)
    draw = ImageDraw.Draw(canvas)
    y = _draw_title(draw, W, title, note, pad, ft, fm) + 6
    lh = _tw(probe, '字', _font(fs))[1] + 4
    if kind == 'year':
        nx = pad
        namx = pad + _tw(probe, '00000', _font(fs))[0] + 12
        for num, name, st in rows:
            draw.text((nx, y), str(num), font=_font(fs), fill=_MUTED)
            draw.text((namx, y), name, font=_font(fs), fill=_INK)
            sw, _ = _tw(draw, st, _font(fs))
            draw.text((W - pad - sw, y), st, font=_font(fs), fill=_LIVE if st == '活跃' else _MUTED)
            y += lh
    elif kind == 'active':
        for name, meta, pos in rows:
            draw.text((pad, y), name, font=_font(ft), fill=_INK)
            mw, _ = _tw(draw, meta, _font(fm))
            draw.text((W - pad - mw, y + 2), meta, font=_font(fm), fill=_MUTED)
            y += _tw(probe, '字', _font(ft))[1] + 2
            if pos:
                draw.text((pad, y), pos, font=_font(fm), fill=_MUTED)
                y += _tw(probe, '字', _font(fm))[1] + 6
            else:
                y += 4
    elif kind == 'help':
        cmd_w = max((_tw(probe, a, _font(fs))[0] for a, _ in rows), default=80) + 4
        for cmd, desc in rows:
            draw.text((pad, y), cmd, font=_font(fs), fill=_INK)
            draw.text((pad + cmd_w + 12, y), desc, font=_font(fs), fill=_MUTED)
            y += lh
        y += 4
        for ln in lines:
            fill = _MUTED if ln in ('示例',) or ln.endswith('：') else _INK
            for wln in _wrap(probe, ln, _font(fs), W - pad * 2):
                draw.text((pad, y), wln, font=_font(fs), fill=fill)
                y += lh
    else:
        for ln in lines:
            if not str(ln).strip():
                y += 4
                continue
            for wln in _wrap(probe, ln, _font(fs), W - pad * 2):
                draw.text((pad, y), wln, font=_font(fs), fill=_INK)
                y += lh
    return _jpeg(canvas.crop((0, 0, W, min(3600, y + 8))))


def _head(event):
    name = f'@{_name(event)}'
    av = _avatar(event)
    return f'![头像 #24px #24px]({av}) {name}' if av else name


async def upload_track_image(event, png, filename='typhoon_nmc.jpg'):
    try:
        from core.application import get_app
        app = get_app()
        hosting = app.module_manager.get('image_hosting') if app and app.module_manager else None
        if not hosting:
            return None
        bot = app.get_bot(event.appid)
        return await hosting.upload_any(
            png, filename, token_manager=getattr(bot, 'token_manager', None) if bot else None
        )
    except Exception as e:
        _tf_log.warning('图床上传失败: %s', e)
        return None


async def _sender_of(event):
    sender = getattr(event, 'sender', None)
    if sender:
        return sender
    try:
        from core.application import get_app
        app = get_app()
        bot = app.get_bot(event.appid) if app else None
        return getattr(bot, 'sender', None) if bot else None
    except Exception:
        return None


async def _send_native(event, blob, filename, buttons=None):
    sender = await _sender_of(event)
    if not sender:
        return False
    try:
        fi = await sender.upload_media(event, blob, 1, file_name=filename)
        if not fi:
            return False
        kw = {'media': {'file_info': fi}, 'skip_suffix': True}
        if buttons:
            kw['buttons'] = buttons
        return _ok(await event.reply(' ', **kw))
    except Exception as e:
        _tf_log.warning('媒体上传失败: %s', e)
        return False


async def _send_merged(event, blob, size, cache_key, buttons=None, extra=''):
    w, h = size
    url = _cache_get(f'host:{cache_key}') if cache_key else None
    if not url:
        url = await upload_track_image(event, blob, filename='typhoon_nmc.jpg')
        if url and cache_key:
            _cache_set(f'host:{cache_key}', url, _IMG_TTL)
    if not url:
        return False
    md = f'{_head(event)}\n![路径 #{w}px #{h}px]({url})'
    if extra:
        md += f'\n\n{extra}'
    for btns in ((buttons or None), None):
        for force in (False, True):
            try:
                kw = {'msg_type': 2, 'skip_suffix': True, 'force_verify_image_resource': force}
                if btns:
                    kw['buttons'] = btns
                r = await event.reply(md, **kw)
                if _ok(r):
                    _tf_log.info('路径图合并消息 %sx%s force=%s buttons=%s', w, h, force, bool(btns))
                    return True
                _tf_log.warning('合并 Markdown 未成功 force=%s: %s', force, r)
            except Exception as e:
                _tf_log.warning('合并 Markdown 失败 force=%s: %s', force, e)
    return False


async def _send_pic(event, blob, size, buttons=None, cache_key=None, src='', extra=''):
    w, h = size
    if await _send_merged(event, blob, (w, h), cache_key, buttons, extra):
        return True
    if await _send_native(event, blob, 'typhoon_nmc.jpg', buttons):
        _tf_log.info('路径图原图通道 %sx%s %sB src=%s', w, h, len(blob), src)
        if extra:
            await safe_reply(event, extra, buttons)
        return True
    try:
        sent = _ok(await event.reply_image(blob, ''))
    except Exception as e:
        _tf_log.warning('reply_image 失败: %s', e)
        sent = False
    if sent:
        follow = extra or ('快捷操作' if buttons else '')
        if follow or buttons:
            await safe_reply(event, follow or '快捷操作', buttons)
    return sent


def _pos_txt(view):
    pts = (view or {}).get('points') or []
    if not pts:
        return ''
    p = pts[-1]
    lat, lng = p.get('lat'), p.get('lng')
    if lat in (None, '') or lng in (None, ''):
        return ''
    return f'{lat}°N {lng}°E'


def spec_lines(title, text):
    s = str(text or '').replace('**', '').replace('`', '')
    s = re.sub(r'\n{3,}', '\n\n', s).strip()
    return {'kind': 'lines', 'title': title, 'lines': s.split('\n')}


def spec_active(active, views=None):
    views = views or [None] * len(active)
    rows = []
    for it, view in zip(active, views):
        name = it['cn'] or it['en'] or str(it['id'])
        bits = [str(it.get('num') or '')]
        if it.get('en') and it.get('en') != it.get('cn'):
            bits.append(it['en'])
        pos = _pos_txt(view) if view and not isinstance(view, Exception) else ''
        rows.append((name, ' · '.join(x for x in bits if x), f'当前位置  {pos}' if pos else ''))
    return {'kind': 'active', 'title': f'活跃台风（{len(active)}）', 'rows': rows}


def spec_year(bundle):
    year = bundle.get('year') or datetime.now().year
    rows = []
    for it in (bundle.get('list') or [])[:30]:
        name = it['cn'] or it['en'] or '未命名'
        if it.get('en') and it.get('en') != it.get('cn'):
            name = f'{name}  {it["en"]}'
        st = '活跃' if it['status'] == 'start' else '停编'
        rows.append((it.get('num') or '', name, st))
    n = len(bundle.get('list') or [])
    return {'kind': 'year', 'title': f'{year}年台风（{n}）', 'rows': rows}


def spec_help():
    return {
        'kind': 'help',
        'title': '台风查询',
        'rows': [
            ('当前台风', '查看当前最强台风'),
            ('活跃台风', '查看全部活跃台风'),
            ('本年台风', '查看本年台风名单'),
            ('台风查询', '按名称或编号查询'),
        ],
        'lines': [
            '示例',
            '台风查询 沙德尔　　按名称',
            '台风查询 2411　　按编号',
            '台风列表 2023　　往年名单',
        ],
    }


def fmt_list_active(active, views=None):
    if not active:
        return '📡 当前暂无活跃台风'
    md = f'**📡 活跃台风（{len(active)}）**\n点选名称查看路径\n\n'
    views = views or [None] * len(active)
    for it, view in zip(active, views):
        link = _chip('🌀 ' + (it['cn'] or it['en'] or str(it['id'])), f'台风查询 {it["id"]}')
        bits = [f'`{it["num"]}`']
        if it.get('en') and it.get('en') != it.get('cn'):
            bits.append(it['en'])
        pos = _pos_txt(view) if view and not isinstance(view, Exception) else ''
        md += f'- {link}\n  {" · ".join(bits)}'
        if pos:
            md += f'\n  📍 {pos}'
        md += '\n'
    return md


def _page_bar(year, page, pages):
    if pages <= 1:
        return ''
    chips = [_chip(f'·{i}·' if i == page else str(i), f'台风列表 {year} {i}') for i in range(1, pages + 1)]
    return f'📄 第 {page}/{pages} 页　' + '　'.join(chips)


def fmt_year(bundle, page=1):
    year = bundle.get('year') or datetime.now().year
    rows = bundle.get('list') or []
    total = len(rows)
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(1, int(page or 1)), pages)
    chunk = rows[(page - 1) * _PAGE_SIZE: page * _PAGE_SIZE]
    md = f'**📋 {year}年台风（{total}）**'
    if pages > 1:
        md += f'　第{page}/{pages}页'
    md += '\n点选名称查看路径\n————————————\n\n'
    for it in chunk:
        link = _chip('🌀 ' + (it['cn'] or it['en'] or '未命名'), f'台风查询 {it["id"]}')
        live = it['status'] == 'start'
        st = '🔴 活跃' if live else '⚫ 停编'
        bits = [f'`{it["num"]}`', st]
        if it.get('en') and it.get('en') != it.get('cn'):
            bits.insert(1, it['en'])
        md += f'- {link}\n  {" · ".join(bits)}\n'
    bar = _page_bar(year, page, pages)
    if bar:
        md += '\n' + bar
    md += '\n\n' + _year_bar(year)
    return md, page, pages


_MD_EMOJI = {
    '编号': '🔢', '强度': '💪', '气压': '📉', '风速': '💨',
    '当前位置': '📍', '最后位置': '📍', '移向': '🧭', '风圈': '⭕',
    '时间': '🕐', '生成': '🌱', '停编时间': '🕐', '过程最强': '⚡',
}


def _peak_point(view):
    best, best_w = None, -1
    for p in view.get('points') or []:
        w = _to_float(p.get('wind')) or 0
        if w >= best_w:
            best, best_w = p, w
    return best or {}


def fmt_detail(view):
    cn, en = view.get('cn') or '', view.get('en') or ''
    live = view.get('status') == 'start'
    st = '🔴 活跃' if live else '⚫ 停编'
    title = f'🌀 **{cn or "未命名"}**' + (f'（{en}）' if en and en != cn else '')
    num = view.get('num') or ''
    lines = [title, f'`{num}`　{st}' if num else st, '————————————']
    pts = view.get('points') or []
    if not pts:
        return '\n'.join(lines + ['暂无路径点'])
    if not live:
        lines.append('📝 该台风已停编，以下为监测资料。')
    last, peak = pts[-1], _peak_point(view)
    skip = {'状态', '时间'} if not live else {'状态'}
    for k, v in _info_pairs(view):
        if k in skip:
            continue
        if k == '当前位置' and not live:
            k = '最后位置'
        lines.append(f'{_MD_EMOJI.get(k, "•")} {k}：{v}')
    if not live:
        first_t, last_t = pts[0].get('time'), last.get('time')
        if first_t:
            lines.append(f'{_MD_EMOJI["生成"]} 生成：{first_t}（北京时）')
        if last_t:
            lines.append(f'{_MD_EMOJI["停编时间"]} 停编时间：{last_t}（北京时）')
        pw, lw = _to_float(peak.get('wind')) or 0, _to_float(last.get('wind')) or 0
        if pw > lw + 0.5:
            bits = [peak.get('strong') or '-', _wind_txt(peak.get('wind'))]
            if peak.get('time'):
                bits.append(str(peak.get('time')))
            lines.append(f'{_MD_EMOJI["过程最强"]} 过程最强：{" · ".join(bits)}')
    fc = _forecast_line(view, sep=' · ')
    if fc and live:
        lines.append('🔮 ' + fc.replace('预报  ', '预报：', 1))
    tips = _defense_tips(view)
    if tips:
        lines += ['', '💡 **防护建议**'] + [f'{i}. {t}' for i, t in enumerate(tips[:3], 1)]
    return '\n'.join(lines)


async def _prepared_image(view):
    try:
        raw, src = await fetch_official_track_png(view)
    except Exception as e:
        _tf_log.warning('官网路径图失败: %s', e)
        return None, None, ''
    if not raw or not str(src).startswith('http'):
        return None, None, ''
    try:
        from PIL import Image
        size = Image.open(io.BytesIO(raw)).size
    except Exception as e:
        _tf_log.warning('官网图无法读取: %s', e)
        return None, None, ''
    return raw, size, src


async def reply_detail(event, view, *, t0=None, buttons=None):
    buttons = buttons if buttons is not None else _nav_btns()
    live = view.get('status') == 'start'
    if not live:
        text = fmt_detail(view)
        if t0 is not None:
            text += f'\n\n耗时：{_ms(t0)}ms'
        await safe_reply(event, text, buttons)
        return True
    blob = src = None
    try:
        blob, _, src = await _prepared_image(view)
    except Exception as e:
        _tf_log.warning('官网图准备失败: %s', e)
    note = f'{_ms(t0)}ms' if t0 is not None else ''
    card = size = None
    try:
        card, size = await asyncio.to_thread(compose_detail, blob, view, note)
    except Exception as e:
        _tf_log.warning('详情卡片渲染失败: %s', e)
    if card and size:
        pts = view.get('points') or []
        last_t = (pts[-1].get('time') if pts else '') or ''
        host_key = _disk_name('card', view.get('id'), last_t, len(card))
        if await _send_pic(event, card, size, buttons, cache_key=host_key, src=src):
            return True
        _tf_log.warning('详情卡片发送失败，改发文字')
    await safe_reply(event, fmt_detail(view), buttons)
    return True


async def safe_reply(event, text, buttons=None):
    text = (text or '').strip() or '（无内容）'
    md = f'{_head(event)}\n{text}'
    for btns in ((buttons or None), None):
        try:
            kw = {'msg_type': 2, 'skip_suffix': True}
            if btns:
                kw['buttons'] = btns
            r = await event.reply(md, **kw)
            if _ok(r):
                return
            _tf_log.warning('回复未成功: %s', r)
        except Exception as e:
            _tf_log.warning('回复失败 markdown: %s', e)
    try:
        await event.reply(f'{_head(event)}\n{text}'[:800], skip_suffix=True)
    except Exception as e:
        _tf_log.warning('回复失败: %s', e)


def guard(fn):
    async def wrapper(event, match):
        try:
            return await fn(event, match)
        except Exception as e:
            _tf_log.error('%s\n%s', e, traceback.format_exc())
            await safe_reply(event, f'台风指令出错：{type(e).__name__}: {e}', _nav_btns())
    wrapper.__name__ = fn.__name__
    return wrapper


async def _say(event, text, t0=None, *, buttons=None, extra=''):
    if extra:
        text = f'{(text or "").strip()}\n\n{extra}'
    if t0 is not None:
        text = f'{text}\n\n耗时：{_ms(t0)}ms'
    await safe_reply(event, text, buttons if buttons is not None else _nav_btns())


async def _say_card(event, spec, extra='', t0=None, buttons=None):
    btns = buttons if buttons is not None else _nav_btns()
    note = f'{_ms(t0)}ms' if t0 is not None else ''
    card = size = None
    try:
        card, size = await asyncio.to_thread(compose_card, spec, note)
    except Exception as e:
        _tf_log.warning('文字卡片渲染失败: %s', e)
    if card and size:
        key = _disk_name('tcard', spec.get('title'), len(card))
        if await _send_pic(event, card, size, btns, cache_key=key, extra=extra):
            return
    text = spec.get('md') or spec.get('title') or '台风'
    await _say(event, text, t0=t0, buttons=btns, extra='' if spec.get('md') else extra)


# ---------- 指令 ----------

def _arg(match):
    try:
        return (match.group(1) or '').strip()
    except IndexError:
        return ''


async def _reply_year_list(event, year, page=1, t0=None):
    bundle = await nmc_list(year)
    if bundle is None:
        return await _say(event, '❗ 暂时无法获取台风数据，请稍后重试')
    bundle['year'] = year
    if not bundle.get('list'):
        return await _say(event, _hint_miss(), extra=_year_bar(year))
    md, page, pages = fmt_year(bundle, page)
    await _say(event, md, t0=t0, buttons=_list_btns(year, page, pages))


@handler(r'^\s*/?(?:台风|当前台风|最强台风)(?:\s+(\S.*?))?\s*$', name='台风', desc='查看当前最强台风', ignore_at_check=True, block=True)
@guard
async def cmd_strongest(event, match):
    extra = _arg(match)
    if extra:
        return await _say(event, _hint_miss())
    start = time.time()
    bundle = await nmc_list()
    if bundle is None:
        return await _say(event, '❗ 暂时无法获取台风数据，请稍后重试')
    active = [x for x in bundle['list'] if x['status'] == 'start']
    if not active:
        return await _say(event, '📡 当前暂无活跃台风', extra=_year_bar(), t0=start)
    views = await asyncio.gather(*[nmc_view(it['id']) for it in active], return_exceptions=True)
    best_view, best_wind = None, -1
    for view in views:
        if isinstance(view, Exception) or not view or not view.get('points'):
            continue
        w = _to_float(view['points'][-1].get('wind')) or 0
        if w >= best_wind:
            best_wind, best_view = w, view
    if not best_view:
        return await _say(event, '❗ 暂时无法获取该台风详情')
    await reply_detail(event, best_view, t0=start)


@handler(r'^\s*/?(?:台风活跃|活跃台风)(?:\s*(\S.*?))?\s*$', name='活跃台风', desc='查看活跃台风', ignore_at_check=True, block=True)
@guard
async def cmd_list(event, match):
    extra = _arg(match)
    if extra:
        year, page = _parse_year_page(extra)
        if year is None:
            return await _say(event, _hint_miss())
        return await _reply_year_list(event, year, page, t0=time.time())
    year = None
    start = time.time()
    bundle = await nmc_list(year)
    if bundle is None:
        return await _say(event, '❗ 暂时无法获取台风数据，请稍后重试')
    active = [x for x in bundle['list'] if x['status'] == 'start']
    views = await asyncio.gather(*[nmc_view(it['id']) for it in active], return_exceptions=True) if active else []
    if not active:
        return await _say(event, '📡 当前暂无活跃台风', extra=_year_bar(), t0=start)
    chips = []
    for it in active:
        show = it['cn'] or it['en'] or str(it['id'])
        chips.append(_chip('🌀 ' + show, f'台风查询 {it["id"]}'))
    extra = '点选名称查看路径\n' + '\n'.join(chips)
    spec = spec_active(active, views)
    spec['md'] = fmt_list_active(active, views)
    await _say_card(event, spec, extra=extra, t0=start)


@handler(r'^\s*/?(?:台风列表|台风年份|今年台风|本年台风)(?:\s*(\S.*?))?\s*$', name='台风年份', desc='查看本年台风', ignore_at_check=True, block=True)
@guard
async def cmd_year(event, match):
    extra = _arg(match)
    head = _cmd_head(match)
    implied = head in ('今年台风', '本年台风')
    if not extra and not implied:
        return await _say(event, _hint_year(), extra=_year_bar())
    year, page = _parse_year_page(extra, default_year=datetime.now().year if implied else None)
    if year is None:
        return await _say(event, _hint_miss())
    await _reply_year_list(event, year, page, t0=time.time())


@handler(r'^\s*/?(?:台风查询|查台风)(?:\s*(\S.*?))?\s*$', name='台风详情', desc='按名称或编号查询台风', ignore_at_check=True, block=True)
@guard
async def cmd_detail(event, match):
    keyword = _arg(match)
    if not keyword:
        return await _say(event, _hint_query(), extra=_year_bar())
    start = time.time()
    year, page = _parse_year_page(keyword)
    if year is not None:
        return await _reply_year_list(event, year, page, t0=start)
    if re.fullmatch(r'(19|20)\d{2}', keyword):
        return await _say(event, _hint_miss())
    tid, err = await resolve_id(keyword)
    if tid is None:
        return await _say(event, _hint_miss())
    view = await nmc_view(tid)
    if not view:
        return await _say(event, _hint_miss())
    await reply_detail(event, view, t0=start)


@handler(r'^\s*/?(?:台风帮助|台风怎么用|使用说明)(?:\s+(\S.*?))?\s*$', name='台风帮助', desc='使用说明', ignore_at_check=True, block=True)
@guard
async def cmd_help(event, match):
    if _arg(match):
        await _say(event, _hint_noarg('台风帮助'))
        return
    await _say(event, (
        '**🌀 台风查询**\n'
        '————————————\n'
        f'{_chip("🌀 当前台风", "台风")}　查看当前最强台风\n'
        f'{_chip("📡 活跃台风", "活跃台风")}　查看全部活跃台风\n'
        f'{_chip("📋 本年台风", "本年台风")}　查看本年台风名单\n'
        f'{_chip("🔍 台风查询", "台风查询 ")}　按名称或编号查询\n'
        '————————————\n'
        '💡 示例：`台风查询 沙德尔`　`台风查询 2411`\n'
        '📅 往年名单：`台风列表 2023`\n\n'
        + _year_bar()
    ), buttons=[])
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


# ---------- from 台风.py ----------

import math
import traceback
from urllib.parse import quote

import aiohttp
from core.base.logger import PLUGIN, get_logger
from core.plugin.decorators import handler, on_unload

_tf_log = get_logger(PLUGIN, '台风合')

NMC = 'https://typhoon.nmc.cn/weatherservice/typhoon/jsons'
NMC_PUB = 'https://www.nmc.cn/publish/typhoon'
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, 'taifeng.db')

_LEVEL_CN = {
    'TD': '热带低压', 'TS': '热带风暴', 'STS': '强热带风暴',
    'TY': '台风', 'STY': '强台风', 'SuperTY': '超强台风',
}
_LEVEL_POWER = {
    'TD': 6, 'TS': 8, 'STS': 10, 'TY': 12, 'STY': 14, 'SuperTY': 16, 'SUPERTY': 16,
}
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0',
    'Referer': 'https://typhoon.nmc.cn/web.html',
    'Accept': '*/*',
}
_PUB_HEADERS = {
    **_HEADERS,
    'Referer': 'https://www.nmc.cn/publish/typhoon/probability.html',
}
_PUB_PAGES = ('probability.html',) + tuple(f'probability-img{i}.html' for i in range(2, 9))
_FONT_CANDIDATES = (
    'C:/Windows/Fonts/msyh.ttc',
    'C:/Windows/Fonts/msyh.ttf',
    'C:/Windows/Fonts/simhei.ttf',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/System/Library/Fonts/PingFang.ttc',
)

# 复用会话 / 短缓存，显著降低查询耗时
_SESSION = None
_SESSION_LOCK = asyncio.Lock()
_MEM_CACHE = {}  # key -> (expire_ts, value)
_FONT_CACHE = {}
_LIST_TTL = 60
_VIEW_TTL = 90
_OFFICIAL_TTL = 180
_IMG_TTL = 300


def _cache_get(key):
    item = _MEM_CACHE.get(key)
    if not item or time.time() > item[0]:
        _MEM_CACHE.pop(key, None)
        return None
    return item[1]


def _cache_set(key, val, ttl):
    _MEM_CACHE[key] = (time.time() + ttl, val)
    return val


async def _get_session():
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        return _SESSION
    async with _SESSION_LOCK:
        if _SESSION is None or _SESSION.closed:
            _SESSION = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False, limit=24, ttl_dns_cache=300, enable_cleanup_closed=True),
                timeout=aiohttp.ClientTimeout(total=12, connect=5),
                headers=_HEADERS,
            )
        return _SESSION


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'CREATE TABLE IF NOT EXISTS jilu ('
        'id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, keyword TEXT, time INTEGER, summary TEXT)'
    )
    conn.commit()
    conn.close()


init_db()


@on_unload
async def _close_http():
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        await _SESSION.close()
    _SESSION = None


# ---------- 工具 ----------

def _btns(*rows):
    return [[{'text': t, 'data': d, 'type': 2} for t, d in row] for row in rows]


def _inline(cmd, label=None):
    return f'[{label or cmd}](mqqapi://aio/inlinecmd?command={quote(cmd)}&enter=false&reply=false)'


def _tf_at(event):
    """头像 + @ 前缀 (与娱乐助手风格一致): ![头像 #30px #30px](qq头像url) <@uid>"""
    uid = str(getattr(event, 'raw_user_id', None) or getattr(event, 'user_id', '') or '').strip()
    if not uid:
        return ''
    appid = str(getattr(event, 'appid', '') or '100000000')
    avatar = f"https://q.qlogo.cn/qqapp/{appid}/{uid}/640"
    return f"![头像 #30px #30px]({avatar}) <@{uid}>\n\n"


def _ms(start):
    return int((time.time() - start) * 1000)


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _power_of(level, wind=None):
    p = _LEVEL_POWER.get(str(level or '').strip())
    if p:
        return p
    try:
        ws = float(wind)
    except (TypeError, ValueError):
        return 0
    return next((pw for th, pw in ((51, 16), (41, 14), (32, 12), (24, 10), (17, 8), (0, 6)) if ws >= th), 6)


def _fmt_time(s):
    s = str(s or '').strip()
    if len(s) >= 12 and s.isdigit():
        return f'{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}'
    return s


def _quad_radii(wind_radius, code):
    """风圈四象限半径 km：NE/SE/SW/NW。"""
    for row in wind_radius or []:
        if not row or str(row[0]).upper() != code:
            continue
        try:
            return [float(row[i]) for i in range(1, 5)]
        except (TypeError, ValueError, IndexError):
            pass
    return None


def _avg_radius(wind_radius, code):
    qs = _quad_radii(wind_radius, code)
    return round(sum(qs) / len(qs)) if qs else None


def _radius_txt(p):
    return [f'{lab}{p.get(k)}km' for lab, k in (('7级', 'radius7'), ('10级', 'radius10'), ('12级', 'radius12')) if p.get(k)]


def parse_jsonp(text):
    text = (text or '').strip()
    for pat in (r'^[^(]*\(\s*(\{.*\})\s*\)\s*;?\s*$', r'\(\(\s*(\{.*\})\s*\)\)'):
        m = re.search(pat, text, re.S)
        if m:
            return json.loads(m.group(1))
    i, j = text.find('{'), text.rfind('}')
    if i >= 0 and j > i:
        return json.loads(text[i:j + 1])
    raise ValueError('无法解析 JSONP')


def _clean_name(v):
    s = str(v or '').strip()
    if not s or s.lower() in ('null', 'none', 'nameless', '未知'):
        return ''
    return s


async def http_bytes(url, timeout=10, headers=None, *, text=False):
    try:
        session = await _get_session()
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout, connect=5),
            headers=headers if headers is not None else _HEADERS,
        ) as resp:
            if resp.status != 200:
                return None
            return await (resp.text() if text else resp.read())
    except Exception as e:
        _tf_log.warning('请求失败 %s: %s', url, e)
        return None


async def http_text(url, timeout=12, headers=None):
    return await http_bytes(url, timeout, headers, text=True)


_RAD = (('radius7', '30KTS'), ('radius10', '50KTS'), ('radius12', '64KTS'))
_QH = [('查询', '台风查询 '), ('帮助', '台风帮助')]
_FAIL = '中央气象台接口无响应，请稍后重试'


async def _nmc_jsonp(name, timeout=10):
    t = int(time.time() * 1000)
    text = await http_text(f'{NMC}/{name}?t={t}&callback=typhoon_jsons_{name}', timeout=timeout)
    if not text:
        return None
    try:
        return parse_jsonp(text)
    except Exception as e:
        _tf_log.warning('解析失败 %s: %s', name, e)
        return None


async def nmc_list(year=None):
    key = 'default' if year is None else str(year)
    ck = f'list:{key}'
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    data = await _nmc_jsonp(f'list_{key}', 10)
    if not data:
        return None
    rows = []
    for item in data.get('typhoonList') or []:
        if not isinstance(item, (list, tuple)) or len(item) < 8:
            continue
        rows.append({
            'id': item[0], 'en': _clean_name(item[1]), 'cn': _clean_name(item[2]),
            'num': str(item[3] or ''), 'desc': _clean_name(item[6]), 'status': item[7] or '',
        })
    return _cache_set(ck, {'year': year, 'list': rows}, _LIST_TTL)


async def nmc_view(tid):
    tid, ck = str(tid), f'view:{tid}'
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    data = await _nmc_jsonp(f'view_{tid}', 12)
    ty = (data or {}).get('typhoon')
    if not isinstance(ty, (list, tuple)) or len(ty) < 9:
        return None
    points = []
    for p in ty[8] or []:
        if not isinstance(p, (list, tuple)) or len(p) < 8:
            continue
        level, wind, wr = str(p[3] or '').strip(), p[7], p[10] if len(p) > 10 else []
        forecasts = []
        fcmap = p[11] if len(p) > 11 else None
        if isinstance(fcmap, dict):
            for f in fcmap.get('BABJ') or []:
                if not isinstance(f, (list, tuple)) or len(f) < 6:
                    continue
                flvl = str(f[7] if len(f) > 7 else '').strip()
                forecasts.append({
                    'hour': f[0], 'lng': f[2], 'lat': f[3], 'pressure': f[4],
                    'wind': f[5], 'level': flvl, 'power': _power_of(flvl, f[5]),
                })
        row = {
            'time': _fmt_time(p[1]), 'level': level,
            'strong': _LEVEL_CN.get(level, level or '-'),
            'lng': p[4], 'lat': p[5], 'pressure': p[6], 'wind': wind,
            'power': _power_of(level, wind),
            'move': p[8] if len(p) > 8 else '',
            'movespeed': p[9] if len(p) > 9 else '',
            'forecasts': forecasts,
        }
        for k, code in _RAD:
            row[k] = _avg_radius(wr, code)
            row[k + 'q'] = _quad_radii(wr, code)
        points.append(row)
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
    if kw in cn or (en and kw.lower() in en.lower()):
        return True
    return False


async def resolve_id(keyword):
    kw = keyword.strip()
    if not kw:
        return None, '请输入名称 / 编号 / ID'

    if re.fullmatch(r'\d{6,10}', kw):
        view = await nmc_view(kw)
        if view:
            return view['id'], None

    bundle = await nmc_list()
    if bundle is None:
        return None, _FAIL
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

    # 纯编号未命中时并行补扫当年/去年
    if kw.isdigit() and year is None:
        now_y = datetime.now().year
        ybs = await asyncio.gather(nmc_list(now_y), nmc_list(now_y - 1), return_exceptions=True)
        for yb in ybs:
            if isinstance(yb, Exception) or not yb:
                continue
            for it in yb['list']:
                if _match(it, kw):
                    return it['id'], None

    return None, '未找到该台风。可试：沙德尔 / 2618 / 3304099 / 台风列表 2024'


async def get_name(event, uid):
    try:
        from core.application import get_app
        app = get_app()
        bot = app.get_bot(event.appid) if app else None
        if bot:
            row = await bot.log_service.db_fetch_one(
                'SELECT name FROM users WHERE user_id = ?', (uid,)
            )
            if row and row.get('name'):
                return row['name']
    except Exception:
        pass
    return uid


async def safe_reply(event, text, buttons=None):
    text = (text or '').strip() or '（无内容）'
    head = _tf_at(event)
    if head and not text.startswith(head):
        text = f'{head}\n{text}'
    for kwargs in (
        {'buttons': buttons, 'msg_type': 2, 'skip_suffix': True},
        {'msg_type': 2, 'skip_suffix': True},
        {},
    ):
        try:
            await event.reply(text if kwargs else text[:800], **kwargs)
            return
        except Exception as e:
            _tf_log.warning('回复失败 %s: %s', kwargs, e)


def guard(fn):
    async def wrapper(event, match):
        try:
            return await fn(event, match)
        except Exception as e:
            _tf_log.error('%s\n%s', e, traceback.format_exc())
            await safe_reply(event, f'台风指令出错：{type(e).__name__}: {e}')
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------- 文案 ----------

def fmt_list_active(rows):
    active = [x for x in rows if x['status'] == 'start']
    if not active:
        return '当前暂无活跃台风\n可用：台风列表 2024'
    md = f'**中央气象台 · 活跃台风（{len(active)}）**\n\n'
    for it in active:
        link = _inline(f'台风查询 {it["id"]}', it['cn'] or it['en'] or str(it['id']))
        en = f'（{it["en"]}）' if it.get('en') else ''
        md += f'- {link}{en}`{it["num"]}`\n'
    md += f'\n历史：{_inline("台风列表 2024")} / {_inline("台风年份")}'
    return md


def fmt_year(bundle):
    year = bundle.get('year') or '本年'
    rows = bundle.get('list') or []
    md = f'**中央气象台 · {year}（{len(rows)}）**\n\n'
    for it in rows[:30]:
        st = '活跃' if it['status'] == 'start' else '停编'
        link = _inline(f'台风查询 {it["id"]}', it['cn'] or it['en'] or '未命名')
        en = f' {it["en"]}' if it.get('en') else ''
        md += f'- {link} `{it["num"]}`{en} · {st}\n'
    if len(rows) > 30:
        md += f'\n…其余 {len(rows) - 30} 个请精确查询'
    return md


# ---------- 路径图（纯 2D 俯视平面） ----------

_NMC_COLOR = {
    'TD': (255, 230, 0), 'TS': (70, 130, 255), 'STS': (40, 190, 60),
    'TY': (255, 150, 0), 'STY': (220, 40, 200), 'SuperTY': (230, 30, 30), 'SUPERTY': (230, 30, 30),
}
# 中国近海可扩展范围（按路径自适应收紧）
_VIEW_MAX = (100.0, 145.0, 5.0, 45.0)
_CN_LABELS = (
    (116.4, 39.9, '北京'), (117.2, 39.1, '天津'), (121.5, 31.2, '上海'),
    (113.3, 23.1, '广州'), (114.1, 22.5, '深圳'), (110.3, 20.0, '海口'),
    (108.3, 22.8, '南宁'), (114.3, 30.6, '武汉'), (112.9, 28.2, '长沙'),
    (118.8, 32.0, '南京'), (120.2, 30.3, '杭州'), (119.3, 26.1, '福州'),
    (118.1, 24.5, '厦门'), (117.3, 31.9, '合肥'), (115.9, 28.7, '南昌'),
    (113.6, 34.8, '郑州'), (117.0, 36.7, '济南'), (121.5, 25.0, '台北'),
    (120.4, 36.1, '青岛'), (121.6, 38.9, '大连'), (109.5, 18.2, '三亚'),
    (110.4, 21.2, '湛江'), (108.9, 34.3, '西安'), (104.1, 30.7, '成都'),
    (125.3, 43.9, '长春'), (123.4, 41.8, '沈阳'), (129.0, 35.1, '釜山'),
    (139.7, 35.7, '东京'),
)
# 东亚近海简化陆地（经度,纬度），内嵌不依赖外部文件
_LAND_POLYS = (
    ((125.24, 1.42), (124.44, 0.43), (121.06, 0.38), (119.77, 0.0), (120.89, 1.31), (122.93, 0.88), (124.08, 0.92), (125.07, 1.64), (125.24, 1.42)),
    ((128.69, 1.13), (128.63, 0.26), (128.12, 0.36), (128.03, 0.0), (127.63, 0.0), (127.4, 1.01), (127.93, 2.17), (128.0, 1.63), (128.6, 1.54), (128.69, 1.13)),
    ((99.46, 0.0), (98.6, 1.82), (95.38, 4.97), (95.29, 5.48), (97.48, 5.25), (100.64, 2.1), (101.66, 2.08), (103.84, 0.1), (99.46, 0.0)),
    ((117.87, 1.83), (119.0, 0.9), (117.81, 0.78), (117.48, 0.0), (109.02, 0.0), (109.07, 1.34), (109.66, 2.01), (110.4, 1.66), (111.17, 1.85), (111.37, 2.7), (113.0, 3.1), (116.73, 6.92), (117.13, 6.93), (117.69, 5.99), (119.18, 5.41), (119.11, 5.02), (118.44, 4.97), (118.62, 4.48), (117.88, 4.14), (117.31, 3.24), (118.05, 2.29), (117.87, 1.83)),
    ((126.38, 8.41), (126.54, 7.19), (126.2, 6.27), (125.83, 7.29), (125.36, 6.79), (125.68, 6.05), (125.4, 5.58), (124.22, 6.16), (123.94, 6.89), (124.24, 7.36), (123.61, 7.83), (122.09, 6.9), (121.92, 7.19), (122.31, 8.04), (123.49, 8.69), (123.84, 8.24), (124.6, 8.51), (124.76, 8.96), (125.47, 8.99), (125.41, 9.76), (126.22, 9.29), (126.38, 8.41)),
    ((123.98, 10.28), (123.0, 9.02), (122.38, 9.71), (122.95, 10.88), (123.5, 10.94), (123.34, 10.27), (124.08, 11.23), (123.98, 10.28)),
    ((118.5, 9.32), (117.18, 8.37), (119.51, 11.37), (119.69, 10.55), (118.5, 9.32)),
    ((121.88, 11.89), (123.12, 11.58), (123.1, 11.17), (122.0, 10.44), (121.88, 11.89)),
    ((125.5, 12.16), (125.78, 11.05), (125.01, 11.31), (125.28, 10.36), (124.8, 10.13), (124.3, 11.49), (124.89, 11.42), (124.88, 11.79), (124.27, 12.56), (125.23, 12.54), (125.5, 12.16)),
    ((121.53, 13.07), (121.26, 12.21), (120.32, 13.47), (121.18, 13.43), (121.53, 13.07)),
    ((121.32, 18.5), (121.94, 18.22), (122.24, 18.48), (122.51, 17.09), (122.25, 16.26), (121.66, 15.93), (121.73, 14.33), (122.7, 14.34), (123.95, 13.78), (124.08, 12.54), (122.93, 13.55), (122.67, 13.19), (122.04, 13.78), (120.63, 13.86), (120.99, 14.53), (120.69, 14.76), (120.56, 14.4), (120.07, 14.97), (119.88, 16.36), (120.29, 16.03), (120.71, 18.51), (121.32, 18.5)),
    ((110.34, 18.68), (109.48, 18.2), (108.65, 18.51), (108.63, 19.37), (109.12, 19.82), (110.79, 20.08), (111.01, 19.7), (110.34, 18.68)),
    ((121.18, 22.79), (120.75, 21.97), (120.11, 23.56), (121.5, 25.3), (121.95, 25.0), (121.18, 22.79)),
    ((134.64, 34.15), (134.77, 33.81), (134.2, 33.2), (133.79, 33.52), (133.02, 32.7), (132.36, 32.99), (132.93, 34.06), (133.49, 33.94), (133.91, 34.36), (134.64, 34.15)),
    ((140.98, 37.14), (140.6, 36.34), (140.77, 35.84), (140.25, 35.14), (138.97, 34.67), (137.22, 34.61), (135.79, 33.46), (135.12, 33.85), (135.08, 34.6), (130.99, 33.89), (132.0, 33.15), (131.33, 31.45), (130.69, 31.03), (130.2, 31.42), (130.45, 32.32), (129.82, 32.61), (129.41, 33.3), (130.36, 33.6), (132.62, 35.43), (134.61, 35.73), (135.68, 35.53), (136.72, 37.3), (137.39, 36.83), (139.43, 38.22), (140.05, 39.44), (139.88, 40.56), (140.31, 41.2), (141.37, 41.38), (141.88, 39.18), (140.96, 38.17), (140.98, 37.14)),
    ((143.91, 44.17), (144.61, 43.96), (145.32, 44.38), (145.54, 43.26), (144.06, 42.99), (143.18, 41.99), (141.61, 42.68), (141.07, 41.59), (139.96, 41.57), (139.82, 42.56), (140.31, 43.33), (141.38, 43.39), (141.97, 45.55), (143.91, 44.17)),
    ((144.07, 50.0), (144.65, 48.98), (143.18, 49.31), (142.56, 47.86), (143.54, 46.84), (143.51, 46.14), (142.75, 46.74), (142.09, 45.97), (141.9, 48.86), (142.15, 50.0), (144.07, 50.0)),
    ((140.5, 50.0), (140.06, 48.45), (138.22, 46.31), (134.87, 43.4), (133.54, 42.81), (132.91, 42.8), (132.28, 43.28), (129.97, 41.94), (129.7, 40.88), (127.53, 39.76), (127.39, 39.21), (128.35, 38.61), (129.46, 36.78), (129.47, 35.63), (129.09, 35.08), (126.49, 34.39), (126.56, 35.68), (126.12, 36.73), (126.86, 36.89), (126.18, 37.75), (125.69, 37.94), (125.28, 37.67), (124.71, 38.11), (125.22, 38.67), (125.32, 39.55), (124.26, 39.93), (121.05, 38.9), (121.59, 39.36), (121.38, 39.75), (122.17, 40.42), (121.64, 40.95), (119.02, 39.25), (118.04, 39.2), (117.53, 38.74), (118.06, 38.06), (118.88, 37.9), (118.91, 37.45), (119.7, 37.16), (120.82, 37.87), (122.36, 37.46), (122.52, 36.93), (121.1, 36.65), (119.15, 34.91), (120.23, 34.36), (121.91, 31.69), (121.89, 30.95), (121.27, 30.68), (122.09, 29.83), (121.68, 28.23), (121.13, 28.14), (118.66, 24.55), (115.89, 22.78), (114.76, 22.67), (114.15, 22.22), (113.81, 22.55), (113.24, 22.05), (110.79, 21.4), (110.44, 20.34), (109.89, 20.28), (109.63, 21.01), (109.86, 21.39), (108.52, 21.72), (106.71, 20.7), (105.88, 19.75), (105.66, 19.06), (107.36, 16.7), (108.88, 15.28), (109.33, 13.43), (109.2, 11.67), (105.16, 8.6), (104.8, 9.24), (105.08, 9.92), (103.5, 10.63), (102.58, 12.19), (101.69, 12.65), (100.83, 12.63), (100.98, 13.41), (100.1, 13.41), (99.22, 9.24), (99.87, 9.21), (100.46, 7.43), (102.96, 5.53), (103.38, 4.85), (103.5, 2.79), (104.23, 1.29), (103.52, 1.23), (101.39, 2.76), (100.2, 5.31), (100.09, 6.46), (98.5, 8.38), (98.34, 7.79), (98.15, 8.35), (98.77, 11.44), (97.6, 16.1), (97.16, 16.93), (95.37, 15.71), (95.0, 15.77), (95.0, 50.0), (140.5, 50.0)),
)
# 省级边界（ECharts 高分辨率省界）
_PROV_LINES = (
    ((120.444,22.441),(120.274,22.561),(120.201,22.722),(120.134,23.001),(120.022,23.061),(120.108,23.342),(120.122,23.505),(120.097,23.57),(120.104,23.701),(120.176,23.808),(120.256,23.853),(120.278,23.929),(120.452,24.184),(120.547,24.371),(120.644,24.49),(120.689,24.602),(120.882,24.746),(120.909,24.853),(121.025,25.041),(121.21,25.128),(121.372,25.16),(121.414,25.239),(121.486,25.293),(121.603,25.305),(121.746,25.162),(121.93,25.131),(121.942,25.039),(122.013,25.002),(121.846,24.837),(121.842,24.734),(121.893,24.618),(121.886,24.53),(121.827,24.424),(121.81,24.34),(121.639,24.085),(121.66,24.008),(121.48,23.323),(121.415,23.196),(121.43,23.124),(121.371,23.085),(121.325,22.946),(121.198,22.753),(121.034,22.651),(120.996,22.566),(120.915,22.303),(120.911,22.048),(120.867,21.985),(120.867,21.89),(120.702,21.928),(120.652,22.033),(120.641,22.242),(120.57,22.362),(120.444,22.441)),
    ((116.906,39.688),(116.883,39.719),(116.917,39.731),(116.902,39.764),(116.954,39.788),(116.918,39.848),(116.9,39.832),(116.787,39.888),(116.783,39.948),(116.758,39.964),(116.778,40.033),(116.931,40.056),(117.022,40.03),(117.086,40.075),(117.212,40.082),(117.224,40.066),(117.183,40.06),(117.198,39.993),(117.137,39.921),(117.159,39.91),(117.153,39.876),(117.261,39.844),(117.157,39.818),(117.206,39.765),(117.154,39.736),(117.179,39.644),(117.128,39.617),(117.017,39.654),(116.978,39.637),(116.946,39.671),(116.951,39.707),(116.906,39.688)),
    ((113.732,36.364),(113.709,36.424),(113.547,36.488),(113.564,36.53),(113.546,36.541),(113.584,36.543),(113.589,36.563),(113.54,36.595),(113.545,36.624),(113.479,36.642),(113.508,36.705),(113.466,36.708),(113.491,36.738),(113.537,36.732),(113.682,36.791),(113.677,36.856),(113.703,36.886),(113.744,36.851),(113.792,36.875),(113.762,36.957),(113.796,36.995),(113.772,37.018),(113.789,37.061),(113.759,37.076),(113.768,37.146),(113.832,37.168),(113.887,37.239),(113.904,37.316),(113.96,37.35),(113.975,37.403),(114.015,37.425),(114.037,37.495),(114.119,37.591),(114.141,37.677),(114.068,37.722),(113.994,37.707),(114.046,37.772),(114.016,37.812),(113.978,37.817),(113.957,37.912),(113.901,37.985),(113.862,38.001),(113.877,38.056),(113.811,38.113),(113.834,38.167),(113.731,38.169),(113.714,38.214),(113.583,38.229),(113.545,38.271),(113.558,38.344),(113.526,38.384),(113.584,38.467),(113.56,38.493),(113.562,38.559),(113.606,38.593),(113.613,38.646),(113.71,38.656),(113.714,38.71),(113.764,38.702),(113.804,38.764),(113.84,38.759),(113.856,38.829),(113.776,38.886),(113.777,38.987),(114.044,39.138),(114.109,39.053),(114.347,39.076),(114.389,39.178),(114.466,39.189),(114.475,39.221),(114.416,39.243),(114.438,39.261),(114.431,39.307),(114.479,39.347),(114.472,39.409),(114.569,39.574),(114.516,39.565),(114.496,39.608),(114.438,39.611),(114.415,39.641),(114.396,39.868),(114.289,39.858),(114.277,39.875),(114.226,39.852),(114.2,39.879),(114.229,39.898),(114.217,39.918),(114.175,39.898),(114.048,39.917),(114.025,39.99),(113.911,40.013),(113.971,40.044),(113.98,40.112),(114.021,40.103),(114.044,40.058),(114.092,40.075),(114.079,40.187),(114.137,40.176),(114.234,40.196),(114.256,40.236),(114.451,40.26),(114.531,40.344),(114.447,40.373),(114.391,40.352),(114.314,40.37),(114.287,40.426),(114.299,40.446),(114.268,40.475),(114.298,40.538),(114.273,40.555),(114.283,40.591),(114.212,40.624),(114.104,40.769),(114.104,40.798),(114.081,40.791),(114.046,40.831),(114.074,40.857),(114.043,40.905),(114.056,40.93),(113.991,40.94),(113.964,40.993),(113.819,41.098),(113.878,41.116),(113.915,41.171),(113.985,41.181),(114.017,41.232),(113.992,41.271),(113.967,41.241),(113.957,41.287),(113.902,41.312),(113.949,41.393),(113.872,41.414),(113.879,41.432),(113.933,41.487),(114.041,41.534),(114.231,41.514),(114.228,41.619),(114.26,41.624),(114.216,41.685),(114.237,41.709),(114.207,41.739),(114.201,41.79),(114.354,41.954),(114.422,41.942),(114.511,41.974),(114.467,42.039),(114.503,42.067),(114.512,42.111),(114.561,42.133),(114.644,42.11),(114.752,42.116),(114.821,42.149),(114.861,42.104),(114.861,42.056),(114.891,42.031),(114.876,42.021),(114.902,42.016),(114.934,41.944),(114.916,41.917),(114.939,41.847),(114.869,41.814),(114.903,41.689),(114.864,41.592),(114.946,41.614),(115.061,41.603),(115.1,41.624),(115.195,41.603),(115.205,41.572),(115.24,41.577),(115.281,41.626),(115.312,41.593),(115.378,41.603),(115.346,41.636),(115.363,41.669),(115.319,41.692),(115.337,41.71),(115.648,41.825),(115.685,41.866),(115.723,41.867),(115.733,41.893),(115.767,41.89),(115.829,41.938),(115.917,41.945),(116.018,41.777),(116.1,41.777),(116.13,41.81),(116.108,41.852),(116.2,41.865),(116.225,41.933),(116.328,42.006),(116.409,41.994),(116.394,41.943),(116.454,41.946),(116.497,41.98),(116.554,41.929),(116.641,41.931),(116.728,41.951),(116.767,41.991),(116.802,41.979),(116.868,42.003),(116.885,42.111),(116.79,42.201),(116.915,42.197),(116.886,42.354),(116.915,42.403),(117.001,42.428),(117.018,42.457),(117.08,42.461),(117.096,42.484),(117.392,42.463),(117.418,42.495),(117.414,42.519),(117.388,42.518),(117.396,42.537),(117.474,42.603),(117.688,42.583),(117.793,42.619),(117.803,42.58),(118.02,42.396),(118.01,42.347),(118.061,42.299),(117.97,42.242),(118.106,42.173),(118.092,42.11),(118.155,42.082),(118.117,42.038),(118.205,42.035),(118.213,42.082),(118.24,42.094),(118.298,42.055),(118.238,42.023),(118.314,41.988),(118.307,41.94),(118.266,41.921),(118.341,41.873),(118.336,41.846),(118.271,41.763),(118.231,41.812),(118.166,41.813),(118.131,41.743),(118.16,41.677),(118.215,41.643),(118.222,41.59),(118.312,41.567),(118.316,41.513),(118.271,41.476),(118.35,41.429),(118.352,41.338),(118.39,41.31),(118.528,41.355),(118.678,41.351),(118.742,41.324),(118.844,41.375),(118.846,41.343),(118.892,41.301),(118.95,41.318),(119.201,41.283),(119.24,41.315),(119.243,41.269),(119.21,41.227),(119.165,41.219),(119.185,41.184),(119.082,41.132),(119.074,41.085),(119.029,41.064),(118.965,41.08),(118.938,41.053),(118.952,41.02),(119.021,40.998),(118.974,40.958),(118.902,40.962),(118.85,40.802),(119.055,40.665),(119.177,40.69),(119.18,40.664),(119.146,40.635),(119.158,40.604),(119.231,40.604),(119.221,40.569),(119.277,40.535),(119.571,40.541),(119.552,40.51),(119.605,40.455),(119.598,40.337),(119.652,40.272),(119.625,40.226),(119.672,40.24),(119.715,40.197),(119.746,40.208),(119.763,40.141),(119.737,40.105),(119.772,40.083),(119.771,40.049),(119.854,40.034),(119.84,40.007),(119.873,39.956),(119.828,39.965),(119.837,39.986),(119.788,39.951),(119.547,39.894),(119.521,39.839),(119.537,39.81),(119.468,39.812),(119.36,39.726),(119.267,39.487),(119.305,39.461),(119.314,39.412),(119.204,39.379),(119.097,39.242),(118.938,39.134),(118.897,39.125),(118.948,39.144),(118.959,39.178),(118.797,39.136),(118.638,39.158),(118.584,39.142),(118.584,39.102),(118.527,39.102),(118.571,39.0),(118.604,38.972),(118.54,38.91),(118.501,38.905),(118.379,38.973),(118.373,39.017),(118.226,39.035),(118.126,39.184),(118.038,39.221),(118.065,39.231),(118.065,39.257),(118.027,39.292),(117.848,39.329),(117.853,39.37),(117.838,39.352),(117.805,39.36),(117.866,39.379),(117.848,39.408),(117.872,39.412),(117.871,39.455),(117.9,39.475),(117.931,39.579),(117.768,39.601),(117.717,39.53),(117.686,39.566),(117.707,39.576),(117.622,39.593),(117.669,39.667),(117.646,39.702),(117.58,39.719),(117.597,39.746),(117.54,39.761),(117.568,39.8),(117.504,39.92),(117.548,39.978),(117.538,39.998),(117.592,39.997),(117.633,39.969),(117.696,39.988),(117.782,39.967),(117.798,40.011),(117.745,40.019),(117.776,40.06),(117.708,40.095),(117.65,40.092),(117.653,40.126),(117.577,40.179),(117.564,40.229),(117.441,40.254),(117.353,40.229),(117.332,40.29),(117.296,40.278),(117.275,40.333),(117.225,40.372),(117.265,40.442),(117.209,40.499),(117.264,40.514),(117.25,40.549),(117.312,40.578),(117.422,40.57),(117.422,40.636),(117.449,40.628),(117.463,40.653),(117.501,40.637),(117.515,40.662),(117.41,40.688),(117.318,40.658),(117.208,40.695),(116.97,40.707),(116.877,40.821),(116.805,40.842),(116.714,40.911),(116.723,40.928),(116.678,40.972),(116.692,41.041),(116.647,41.06),(116.616,41.054),(116.603,40.977),(116.563,40.994),(116.456,40.981),(116.475,40.896),(116.399,40.906),(116.371,40.944),(116.345,40.934),(116.335,40.905),(116.466,40.772),(116.317,40.772),(116.31,40.752),(116.248,40.792),(116.165,40.664),(116.111,40.647),(116.122,40.63),(115.984,40.579),(115.968,40.606),(115.908,40.618),(115.754,40.539),(115.736,40.504),(115.782,40.492),(115.771,40.443),(115.859,40.363),(115.919,40.354),(115.97,40.265),(115.898,40.237),(115.873,40.188),(115.849,40.185),(115.853,40.148),(115.777,40.178),(115.741,40.133),(115.6,40.12),(115.591,40.097),(115.455,40.03),(115.427,39.951),(115.521,39.902),(115.515,39.838),(115.569,39.814),(115.426,39.775),(115.493,39.739),(115.501,39.691),(115.479,39.652),(115.522,39.641),(115.515,39.593),(115.546,39.619),(115.58,39.59),(115.668,39.616),(115.753,39.513),(115.829,39.508),(115.819,39.531),(115.888,39.551),(115.911,39.602),(115.958,39.562),(115.979,39.596),(115.996,39.577),(116.131,39.568),(116.204,39.589),(116.247,39.558),(116.259,39.501),(116.337,39.457),(116.435,39.443),(116.457,39.459),(116.444,39.482),(116.413,39.482),(116.402,39.528),(116.444,39.511),(116.438,39.526),(116.478,39.535),(116.472,39.555),(116.509,39.552),(116.525,39.597),(116.566,39.62),(116.706,39.589),(116.728,39.596),(116.701,39.621),(116.755,39.618),(116.78,39.594),(116.812,39.616),(116.787,39.553),(116.823,39.533),(116.822,39.487),(116.785,39.467),(116.876,39.435),(116.839,39.411),(116.843,39.376),(116.818,39.374),(116.83,39.339),(116.871,39.358),(116.89,39.339),(116.863,39.298),(116.894,39.228),(116.856,39.216),(116.864,39.154),(116.912,39.15),(116.927,39.12),(116.872,39.055),(116.757,39.051),(116.755,39.004),(116.709,38.933),(116.724,38.853),(116.752,38.832),(116.745,38.753),(116.868,38.746),(116.878,38.682),(117.046,38.706),(117.039,38.688),(117.068,38.681),(117.053,38.642),(117.099,38.587),(117.23,38.644),(117.26,38.608),(117.237,38.585),(117.262,38.587),(117.24,38.579),(117.254,38.557),(117.351,38.562),(117.369,38.583),(117.369,38.565),(117.479,38.618),(117.527,38.602),(117.64,38.627),(117.648,38.509),(117.72,38.465),(117.781,38.374),(117.938,38.389),(117.958,38.362),(117.808,38.228),(117.802,38.174),(117.769,38.163),(117.771,38.135),(117.728,38.092),(117.557,38.058),(117.512,37.942),(117.436,37.853),(117.339,37.863),(117.265,37.839),(117.09,37.85),(117.027,37.833),(116.804,37.851),(116.747,37.805),(116.748,37.76),(116.724,37.768),(116.725,37.745),(116.68,37.729),(116.611,37.626),(116.457,37.515),(116.435,37.474),(116.369,37.527),(116.38,37.563),(116.32,37.581),(116.335,37.575),(116.3,37.569),(116.276,37.522),(116.299,37.51),(116.279,37.469),(116.225,37.48),(116.244,37.456),(116.228,37.425),(116.269,37.431),(116.286,37.404),(116.236,37.362),(116.167,37.386),(115.977,37.338),(115.971,37.241),(115.905,37.208),(115.884,37.1),(115.777,36.992),(115.797,36.968),(115.762,36.938),(115.766,36.909),(115.701,36.868),(115.687,36.809),(115.479,36.76),(115.445,36.69),(115.355,36.628),(115.331,36.549),(115.273,36.498),(115.317,36.454),(115.298,36.414),(115.347,36.391),(115.361,36.312),(115.423,36.324),(115.42,36.289),(115.469,36.269),(115.484,36.149),(115.455,36.172),(115.413,36.139),(115.405,36.16),(115.366,36.1),(115.319,36.088),(115.242,36.191),(115.125,36.211),(115.105,36.173),(115.061,36.176),(115.047,36.113),(114.999,36.07),(114.921,36.049),(114.913,36.141),(114.771,36.125),(114.735,36.156),(114.589,36.119),(114.568,36.152),(114.356,36.23),(114.346,36.256),(114.24,36.252),(114.212,36.273),(114.179,36.243),(114.141,36.28),(114.068,36.273),(114.038,36.305),(114.059,36.328),(114.027,36.325),(114.025,36.355),(113.978,36.358),(114.003,36.335),(113.994,36.314),(113.954,36.358),(113.958,36.337),(113.912,36.315),(113.883,36.354),(113.818,36.332),(113.732,36.364)),
    ((119.854,39.989),(119.854,40.034),(119.771,40.049),(119.772,40.083),(119.737,40.105),(119.763,40.141),(119.746,40.208),(119.715,40.197),(119.672,40.24),(119.625,40.226),(119.652,40.272),(119.598,40.337),(119.605,40.455),(119.552,40.51),(119.571,40.541),(119.277,40.535),(119.221,40.569),(119.231,40.604),(119.158,40.604),(119.146,40.635),(119.18,40.664),(119.177,40.69),(119.055,40.665),(118.988,40.698),(118.95,40.748),(118.896,40.754),(118.911,40.777),(118.847,40.81),(118.902,40.962),(118.974,40.958),(119.021,40.998),(118.952,41.02),(118.938,41.053),(118.965,41.08),(119.029,41.064),(119.072,41.083),(119.082,41.132),(119.183,41.181),(119.165,41.219),(119.21,41.227),(119.243,41.269),(119.241,41.319),(119.3,41.329),(119.311,41.35),(119.327,41.33),(119.331,41.386),(119.306,41.403),(119.375,41.421),(119.379,41.46),(119.404,41.476),(119.405,41.511),(119.362,41.566),(119.415,41.562),(119.42,41.583),(119.313,41.641),(119.3,41.711),(119.319,41.731),(119.301,41.743),(119.318,41.765),(119.291,41.784),(119.335,41.87),(119.325,41.97),(119.375,42.021),(119.386,42.089),(119.315,42.12),(119.278,42.186),(119.238,42.198),(119.281,42.264),(119.433,42.317),(119.488,42.352),(119.503,42.389),(119.572,42.36),(119.54,42.295),(119.609,42.277),(119.617,42.253),(119.744,42.212),(119.851,42.214),(119.844,42.101),(119.951,41.975),(119.955,41.921),(119.99,41.899),(120.023,41.817),(120.05,41.828),(120.036,41.709),(120.097,41.697),(120.139,41.729),(120.126,41.769),(120.188,41.849),(120.301,41.889),(120.261,41.904),(120.272,41.926),(120.318,41.935),(120.334,41.98),(120.422,41.986),(120.411,41.997),(120.457,42.016),(120.451,42.058),(120.499,42.092),(120.47,42.099),(120.482,42.116),(120.585,42.168),(120.625,42.155),(120.746,42.224),(120.821,42.229),(120.83,42.253),(120.889,42.243),(120.888,42.272),(121.069,42.253),(121.22,42.372),(121.285,42.389),(121.312,42.44),(121.386,42.452),(121.416,42.486),(121.569,42.487),(121.607,42.517),(121.657,42.443),(121.702,42.44),(121.748,42.489),(121.818,42.505),(121.832,42.534),(121.871,42.528),(121.906,42.571),(121.896,42.594),(121.918,42.589),(121.903,42.638),(121.966,42.701),(122.064,42.724),(122.196,42.68),(122.205,42.733),(122.338,42.671),(122.396,42.685),(122.458,42.774),(122.373,42.776),(122.351,42.826),(122.42,42.843),(122.564,42.826),(122.581,42.79),(122.626,42.773),(122.731,42.787),(122.855,42.707),(122.888,42.771),(122.946,42.754),(122.989,42.779),(123.059,42.77),(123.223,42.826),(123.171,42.853),(123.185,42.926),(123.26,42.994),(123.473,43.043),(123.536,43.008),(123.587,43.013),(123.581,43.037),(123.612,43.05),(123.597,43.062),(123.628,43.081),(123.648,43.174),(123.668,43.182),(123.646,43.209),(123.678,43.225),(123.665,43.266),(123.705,43.274),(123.7,43.312),(123.721,43.316),(123.703,43.327),(123.722,43.357),(123.696,43.355),(123.703,43.404),(123.75,43.439),(123.747,43.472),(123.792,43.491),(123.873,43.452),(123.851,43.416),(123.893,43.391),(123.896,43.362),(123.947,43.353),(124.033,43.281),(124.102,43.295),(124.115,43.248),(124.217,43.257),(124.229,43.235),(124.284,43.23),(124.274,43.18),(124.293,43.155),(124.427,43.077),(124.334,42.998),(124.442,42.96),(124.367,42.893),(124.44,42.879),(124.455,42.824),(124.66,42.974),(124.693,43.057),(124.75,43.07),(124.8,43.123),(124.896,43.131),(124.89,43.076),(124.841,43.029),(124.876,42.969),(124.85,42.883),(124.873,42.792),(124.901,42.789),(124.929,42.82),(124.975,42.804),(124.997,42.746),(124.969,42.723),(124.99,42.696),(124.966,42.678),(125.016,42.666),(125.03,42.617),(125.098,42.623),(125.067,42.536),(125.092,42.515),(125.067,42.503),(125.196,42.411),(125.204,42.367),(125.168,42.356),(125.176,42.309),(125.264,42.313),(125.3,42.29),(125.276,42.231),(125.313,42.221),(125.283,42.172),(125.32,42.205),(125.307,42.146),(125.357,42.146),(125.369,42.184),(125.49,42.137),(125.449,42.1),(125.412,42.102),(125.424,42.079),(125.37,42.003),(125.293,41.964),(125.353,41.929),(125.308,41.925),(125.297,41.888),(125.295,41.824),(125.347,41.763),(125.317,41.678),(125.412,41.692),(125.451,41.675),(125.472,41.64),(125.45,41.599),(125.508,41.534),(125.494,41.51),(125.543,41.469),(125.549,41.401),(125.638,41.345),(125.62,41.318),(125.647,41.266),(125.676,41.278),(125.694,41.245),(125.75,41.246),(125.738,41.18),(125.792,41.167),(125.714,41.105),(125.74,41.087),(125.685,41.022),(125.683,40.98),(125.59,40.932),(125.578,40.902),(125.653,40.917),(125.693,40.893),(125.708,40.867),(125.637,40.809),(125.688,40.771),(125.618,40.764),(125.586,40.789),(125.545,40.729),(125.486,40.728),(125.454,40.678),(125.419,40.674),(125.421,40.635),(125.376,40.659),(125.291,40.659),(125.264,40.621),(125.19,40.615),(125.048,40.551),(125.002,40.514),(125.045,40.467),(124.992,40.478),(124.934,40.457),(124.903,40.484),(124.835,40.415),(124.745,40.375),(124.725,40.323),(124.616,40.288),(124.352,40.084),(124.337,40.051),(124.372,40.021),(124.354,39.978),(124.299,39.97),(124.287,39.933),(124.23,39.92),(124.214,39.864),(124.167,39.828),(124.151,39.746),(124.1,39.778),(124.104,39.824),(123.999,39.801),(123.821,39.832),(123.692,39.808),(123.659,39.832),(123.613,39.775),(123.577,39.781),(123.549,39.756),(123.535,39.789),(123.448,39.732),(123.394,39.725),(123.384,39.767),(123.282,39.759),(123.275,39.738),(123.25,39.751),(123.284,39.695),(123.219,39.698),(123.217,39.667),(123.167,39.675),(123.143,39.646),(123.104,39.678),(123.039,39.664),(123.047,39.645),(123.017,39.658),(122.974,39.596),(122.954,39.615),(122.856,39.607),(122.822,39.565),(122.801,39.58),(122.689,39.517),(122.651,39.52),(122.533,39.421),(122.421,39.414),(122.367,39.391),(122.364,39.366),(122.34,39.379),(122.248,39.271),(122.118,39.215),(122.121,39.175),(122.171,39.151),(122.051,39.108),(122.07,39.062),(121.997,39.069),(121.968,39.03),(121.913,39.061),(121.923,39.015),(121.854,39.036),(121.905,39.0),(121.912,38.964),(121.834,38.951),(121.793,39.021),(121.764,39.029),(121.738,39.0),(121.674,39.011),(121.663,38.967),(121.619,38.948),(121.72,38.921),(121.698,38.865),(121.568,38.876),(121.496,38.814),(121.341,38.819),(121.261,38.787),(121.194,38.721),(121.134,38.726),(121.111,38.78),(121.129,38.8),(121.111,38.862),(121.13,38.881),(121.09,38.898),(121.093,38.929),(121.128,38.96),(121.222,38.941),(121.326,38.973),(121.344,38.988),(121.318,39.016),(121.374,39.062),(121.418,39.029),(121.472,39.026),(121.626,39.12),(121.644,39.111),(121.605,39.074),(121.669,39.092),(121.684,39.122),(121.588,39.194),(121.592,39.229),(121.642,39.243),(121.597,39.249),(121.591,39.272),(121.687,39.283),(121.668,39.312),(121.717,39.318),(121.725,39.365),(121.509,39.293),(121.466,39.302),(121.422,39.367),(121.325,39.372),(121.315,39.392),(121.262,39.375),(121.248,39.425),(121.305,39.488),(121.285,39.508),(121.258,39.485),(121.227,39.517),(121.228,39.556),(121.298,39.606),(121.451,39.626),(121.446,39.655),(121.492,39.666),(121.503,39.704),(121.46,39.742),(121.489,39.766),(121.475,39.812),(121.538,39.873),(121.62,39.88),(121.7,39.938),(121.776,39.938),(121.85,39.999),(121.809,40.005),(121.824,40.037),(121.885,40.054),(121.959,40.135),(121.996,40.129),(122.007,40.17),(121.96,40.193),(121.937,40.238),(122.027,40.245),(122.04,40.322),(122.111,40.316),(122.139,40.34),(122.111,40.369),(122.179,40.366),(122.172,40.389),(122.2,40.386),(122.191,40.43),(122.229,40.425),(122.242,40.466),(122.28,40.479),(122.245,40.485),(122.246,40.521),(122.146,40.596),(122.148,40.672),(122.066,40.649),(121.952,40.682),(121.94,40.796),(121.854,40.821),(121.813,40.898),(121.693,40.832),(121.606,40.845),(121.554,40.818),(121.556,40.851),(121.5,40.88),(121.336,40.901),(121.217,40.852),(121.137,40.874),(121.078,40.818),(121.086,40.796),(121.017,40.779),(120.98,40.821),(120.998,40.782),(120.981,40.747),(121.028,40.745),(121.034,40.71),(120.838,40.679),(120.816,40.588),(120.729,40.54),(120.672,40.47),(120.619,40.461),(120.603,40.361),(120.532,40.321),(120.524,40.257),(120.475,40.184),(120.372,40.175),(120.135,40.075),(119.956,40.047),(119.919,39.99),(119.854,39.989)),
    ((118.887,31.52),(118.869,31.612),(118.804,31.62),(118.783,31.656),(118.799,31.669),(118.752,31.678),(118.726,31.628),(118.658,31.642),(118.644,31.672),(118.698,31.71),(118.686,31.726),(118.628,31.76),(118.556,31.729),(118.522,31.743),(118.546,31.763),(118.482,31.779),(118.505,31.842),(118.364,31.933),(118.402,32.02),(118.386,32.062),(118.501,32.121),(118.511,32.194),(118.645,32.211),(118.677,32.254),(118.658,32.304),(118.708,32.335),(118.679,32.394),(118.691,32.473),(118.594,32.479),(118.617,32.517),(118.565,32.562),(118.569,32.587),(118.599,32.602),(118.634,32.579),(118.654,32.599),(118.69,32.589),(118.688,32.605),(118.701,32.589),(118.72,32.615),(118.735,32.59),(118.764,32.604),(118.785,32.583),(118.823,32.604),(118.844,32.567),(118.908,32.593),(118.889,32.556),(118.938,32.558),(118.98,32.504),(119.042,32.516),(119.086,32.453),(119.148,32.493),(119.178,32.595),(119.219,32.574),(119.231,32.607),(119.186,32.826),(119.104,32.827),(119.046,32.912),(119.016,32.908),(119.021,32.956),(118.933,32.938),(118.854,32.959),(118.838,32.912),(118.81,32.913),(118.812,32.855),(118.742,32.854),(118.757,32.737),(118.715,32.72),(118.64,32.745),(118.577,32.72),(118.468,32.724),(118.453,32.744),(118.424,32.719),(118.372,32.723),(118.365,32.771),(118.332,32.762),(118.301,32.784),(118.303,32.847),(118.251,32.849),(118.234,32.925),(118.312,32.962),(118.253,32.982),(118.233,33.061),(118.213,33.062),(118.229,33.07),(118.203,33.089),(118.224,33.091),(118.202,33.106),(118.22,33.114),(118.212,33.199),(118.179,33.219),(118.151,33.171),(118.039,33.136),(117.989,33.18),(117.985,33.223),(117.938,33.23),(117.994,33.332),(117.971,33.349),(118.03,33.373),(118.018,33.407),(118.051,33.492),(118.108,33.476),(118.112,33.617),(118.169,33.664),(118.154,33.721),(118.187,33.745),(118.068,33.767),(118.022,33.739),(117.966,33.764),(117.95,33.734),(117.901,33.736),(117.902,33.721),(117.826,33.738),(117.745,33.712),(117.724,33.741),(117.759,33.886),(117.704,33.889),(117.673,33.936),(117.671,33.995),(117.643,34.02),(117.613,34.031),(117.616,34.003),(117.572,33.984),(117.516,34.062),(117.416,34.026),(117.354,34.09),(117.31,34.068),(117.193,34.069),(117.131,34.103),(117.124,34.129),(117.048,34.152),(117.025,34.168),(117.05,34.243),(116.97,34.284),(116.97,34.39),(116.91,34.409),(116.829,34.39),(116.761,34.463),(116.573,34.49),(116.596,34.512),(116.495,34.569),(116.43,34.653),(116.368,34.646),(116.394,34.707),(116.362,34.725),(116.367,34.746),(116.403,34.757),(116.408,34.852),(116.444,34.896),(116.632,34.941),(116.771,34.917),(116.798,34.939),(116.79,34.976),(116.805,34.929),(116.81,34.97),(116.822,34.93),(116.977,34.871),(116.968,34.841),(117.051,34.771),(117.089,34.706),(117.066,34.648),(117.138,34.634),(117.176,34.471),(117.226,34.473),(117.201,34.442),(117.254,34.451),(117.269,34.533),(117.363,34.59),(117.488,34.467),(117.587,34.462),(117.659,34.502),(117.685,34.548),(117.803,34.52),(117.794,34.652),(117.903,34.646),(117.91,34.671),(117.955,34.68),(118.009,34.648),(118.096,34.654),(118.115,34.615),(118.079,34.57),(118.186,34.545),(118.134,34.484),(118.179,34.454),(118.171,34.382),(118.208,34.378),(118.221,34.406),(118.278,34.405),(118.292,34.426),(118.405,34.429),(118.44,34.509),(118.433,34.62),(118.476,34.624),(118.463,34.668),(118.497,34.673),(118.523,34.712),(118.601,34.716),(118.607,34.695),(118.684,34.679),(118.784,34.723),(118.717,34.765),(118.739,34.768),(118.73,34.789),(118.774,34.796),(118.768,34.845),(118.803,34.846),(118.859,34.941),(118.866,35.03),(119.117,35.056),(119.141,35.098),(119.287,35.115),(119.306,35.034),(119.271,35.069),(119.234,35.045),(119.212,34.982),(119.203,34.891),(119.239,34.8),(119.379,34.765),(119.454,34.781),(119.503,34.755),(119.376,34.76),(119.526,34.733),(119.466,34.674),(119.562,34.632),(119.583,34.6),(119.812,34.486),(119.963,34.459),(120.312,34.308),(120.368,34.092),(120.571,33.703),(120.591,33.587),(120.717,33.42),(120.741,33.338),(120.833,33.282),(120.82,33.238),(120.848,33.221),(120.827,33.186),(120.853,33.076),(120.906,33.03),(120.927,32.881),(120.975,32.875),(120.967,32.754),(120.925,32.752),(120.901,32.724),(120.917,32.643),(120.971,32.653),(120.98,32.637),(120.927,32.621),(121.021,32.606),(121.153,32.529),(121.418,32.443),(121.474,32.139),(121.541,32.153),(121.526,32.137),(121.545,32.124),(121.76,32.06),(121.856,31.956),(121.971,31.72),(121.976,31.617),(121.594,31.705),(121.432,31.77),(121.386,31.834),(121.316,31.872),(121.201,31.836),(121.119,31.76),(121.373,31.554),(121.324,31.5),(121.248,31.478),(121.235,31.493),(121.147,31.444),(121.16,31.406),(121.107,31.355),(121.162,31.283),(121.105,31.274),(121.089,31.293),(121.062,31.268),(121.069,31.149),(120.879,31.134),(120.857,31.104),(120.905,31.081),(120.892,31.004),(120.804,31.006),(120.746,30.963),(120.698,30.972),(120.704,30.871),(120.684,30.883),(120.655,30.848),(120.59,30.854),(120.504,30.759),(120.456,30.817),(120.436,30.921),(120.365,30.881),(120.372,30.949),(120.236,30.926),(120.133,30.943),(120.001,31.028),(119.92,31.171),(119.828,31.175),(119.811,31.149),(119.78,31.18),(119.704,31.152),(119.673,31.169),(119.582,31.109),(119.533,31.159),(119.461,31.157),(119.429,31.183),(119.392,31.175),(119.399,31.199),(119.366,31.196),(119.381,31.268),(119.357,31.304),(119.338,31.26),(119.239,31.255),(119.182,31.301),(119.105,31.235),(118.796,31.229),(118.762,31.278),(118.7,31.302),(118.72,31.295),(118.756,31.387),(118.771,31.363),(118.853,31.395),(118.887,31.52)),
    ((119.624,31.131),(119.673,31.169),(119.704,31.152),(119.78,31.18),(119.811,31.149),(119.828,31.175),(119.92,31.171),(120.001,31.028),(120.133,30.943),(120.236,30.926),(120.372,30.949),(120.365,30.881),(120.436,30.921),(120.456,30.817),(120.504,30.759),(120.59,30.854),(120.655,30.848),(120.684,30.883),(120.704,30.871),(120.698,30.972),(120.746,30.963),(120.804,31.006),(120.866,30.99),(120.949,31.03),(120.99,31.015),(120.991,30.896),(121.022,30.876),(120.991,30.823),(121.015,30.836),(121.046,30.816),(121.064,30.848),(121.113,30.854),(121.138,30.83),(121.123,30.779),(121.218,30.786),(121.271,30.733),(121.275,30.678),(121.24,30.649),(121.059,30.564),(121.12,30.49),(121.272,30.375),(121.351,30.381),(121.477,30.28),(121.627,30.078),(121.722,29.993),(121.784,29.994),(121.92,29.921),(121.972,29.956),(122.008,29.893),(122.144,29.896),(122.007,29.767),(121.926,29.742),(121.834,29.653),(121.873,29.633),(121.924,29.652),(122.0,29.594),(121.967,29.507),(121.992,29.446),(121.933,29.354),(121.959,29.338),(121.945,29.285),(122.004,29.263),(121.968,29.25),(121.974,29.195),(121.938,29.187),(121.989,29.152),(121.967,29.054),(121.901,29.071),(121.895,29.106),(121.853,29.087),(121.786,29.107),(121.769,29.167),(121.716,29.125),(121.609,29.17),(121.662,29.116),(121.656,29.062),(121.713,29.029),(121.714,28.977),(121.777,28.88),(121.76,28.856),(121.66,28.87),(121.688,28.864),(121.705,28.816),(121.686,28.709),(121.541,28.656),(121.595,28.577),(121.635,28.563),(121.665,28.445),(121.692,28.419),(121.634,28.354),(121.675,28.348),(121.649,28.279),(121.582,28.24),(121.564,28.288),(121.489,28.302),(121.373,28.132),(121.289,28.148),(121.308,28.089),(121.262,28.035),(121.15,28.025),(121.117,28.133),(121.071,28.111),(120.992,27.95),(121.064,27.896),(121.162,27.909),(121.163,27.88),(121.199,27.863),(121.192,27.823),(121.135,27.787),(121.153,27.815),(121.023,27.834),(120.942,27.897),(120.798,27.78),(120.636,27.578),(120.703,27.474),(120.671,27.363),(120.573,27.314),(120.554,27.258),(120.576,27.239),(120.522,27.143),(120.462,27.143),(120.408,27.192),(120.398,27.246),(120.431,27.26),(120.353,27.346),(120.351,27.39),(120.319,27.409),(120.275,27.389),(120.251,27.44),(120.135,27.42),(120.134,27.394),(120.099,27.393),(120.053,27.339),(119.995,27.38),(119.957,27.364),(119.945,27.314),(119.771,27.307),(119.785,27.328),(119.686,27.438),(119.711,27.464),(119.709,27.515),(119.66,27.54),(119.676,27.575),(119.632,27.583),(119.645,27.663),(119.618,27.675),(119.54,27.676),(119.502,27.65),(119.467,27.527),(119.438,27.509),(119.417,27.54),(119.371,27.532),(119.269,27.422),(119.148,27.426),(119.122,27.438),(119.123,27.481),(119.065,27.467),(118.991,27.504),(118.956,27.45),(118.902,27.464),(118.857,27.519),(118.911,27.571),(118.914,27.619),(118.875,27.681),(118.898,27.72),(118.841,27.78),(118.819,27.917),(118.731,27.97),(118.72,28.05),(118.745,28.09),(118.804,28.119),(118.804,28.165),(118.762,28.171),(118.812,28.229),(118.755,28.254),(118.714,28.313),(118.623,28.257),(118.548,28.287),(118.497,28.275),(118.492,28.238),(118.445,28.251),(118.434,28.289),(118.487,28.329),(118.432,28.399),(118.482,28.471),(118.415,28.498),(118.446,28.514),(118.41,28.569),(118.434,28.677),(118.392,28.701),(118.384,28.788),(118.364,28.813),(118.301,28.826),(118.271,28.919),(118.195,28.904),(118.229,28.944),(118.181,28.981),(118.134,28.984),(118.128,29.017),(118.101,28.991),(118.108,29.013),(118.098,28.999),(118.067,29.049),(118.076,29.075),(118.038,29.098),(118.054,29.117),(118.029,29.17),(118.043,29.211),(118.082,29.233),(118.073,29.29),(118.179,29.299),(118.167,29.315),(118.208,29.348),(118.192,29.395),(118.217,29.42),(118.316,29.423),(118.312,29.496),(118.348,29.475),(118.384,29.511),(118.496,29.519),(118.501,29.576),(118.574,29.639),(118.647,29.644),(118.745,29.739),(118.755,29.846),(118.842,29.892),(118.84,29.938),(118.896,29.938),(118.903,30.029),(118.869,30.104),(118.897,30.148),(118.846,30.156),(118.931,30.204),(118.882,30.249),(118.88,30.315),(118.955,30.36),(119.058,30.305),(119.091,30.324),(119.227,30.289),(119.248,30.342),(119.327,30.372),(119.35,30.35),(119.4,30.368),(119.349,30.41),(119.328,30.533),(119.275,30.511),(119.24,30.533),(119.266,30.575),(119.239,30.609),(119.312,30.622),(119.389,30.687),(119.409,30.646),(119.445,30.651),(119.483,30.705),(119.48,30.773),(119.525,30.776),(119.577,30.832),(119.557,30.899),(119.584,30.974),(119.635,31.02),(119.63,31.086),(119.649,31.105),(119.624,31.131)),
    ((116.375,34.641),(116.43,34.653),(116.495,34.569),(116.596,34.512),(116.573,34.49),(116.761,34.463),(116.829,34.39),(116.91,34.409),(116.97,34.39),(116.971,34.281),(117.05,34.243),(117.025,34.168),(117.124,34.129),(117.152,34.084),(117.226,34.064),(117.354,34.09),(117.416,34.026),(117.511,34.062),(117.576,33.983),(117.616,34.003),(117.613,34.031),(117.643,34.02),(117.671,33.995),(117.673,33.936),(117.704,33.889),(117.759,33.886),(117.724,33.741),(117.745,33.712),(117.826,33.738),(117.902,33.721),(117.901,33.736),(117.95,33.734),(117.966,33.764),(118.022,33.739),(118.068,33.767),(118.187,33.745),(118.154,33.721),(118.169,33.664),(118.112,33.617),(118.108,33.476),(118.051,33.492),(118.018,33.407),(118.03,33.373),(117.971,33.349),(117.994,33.332),(117.938,33.23),(117.985,33.223),(117.989,33.18),(118.039,33.136),(118.151,33.171),(118.179,33.219),(118.212,33.199),(118.22,33.114),(118.202,33.106),(118.224,33.091),(118.203,33.089),(118.229,33.07),(118.213,33.062),(118.233,33.061),(118.253,32.982),(118.312,32.962),(118.234,32.925),(118.251,32.849),(118.303,32.847),(118.301,32.784),(118.332,32.762),(118.365,32.771),(118.376,32.72),(118.424,32.719),(118.453,32.744),(118.468,32.724),(118.577,32.72),(118.64,32.745),(118.715,32.72),(118.757,32.737),(118.742,32.854),(118.812,32.855),(118.81,32.913),(118.838,32.912),(118.854,32.959),(118.933,32.938),(119.021,32.956),(119.016,32.908),(119.046,32.912),(119.104,32.827),(119.186,32.826),(119.231,32.607),(119.219,32.574),(119.178,32.595),(119.148,32.493),(119.086,32.453),(119.042,32.516),(118.98,32.504),(118.938,32.558),(118.889,32.556),(118.908,32.593),(118.844,32.567),(118.823,32.604),(118.785,32.583),(118.764,32.604),(118.735,32.59),(118.72,32.615),(118.701,32.589),(118.688,32.605),(118.69,32.589),(118.654,32.599),(118.634,32.579),(118.599,32.602),(118.569,32.587),(118.565,32.562),(118.617,32.517),(118.594,32.479),(118.691,32.473),(118.679,32.394),(118.708,32.335),(118.658,32.304),(118.677,32.254),(118.645,32.211),(118.511,32.194),(118.501,32.121),(118.396,32.077),(118.402,32.02),(118.364,31.933),(118.473,31.88),(118.468,31.857),(118.505,31.842),(118.482,31.779),(118.546,31.763),(118.531,31.736),(118.639,31.76),(118.686,31.726),(118.698,31.71),(118.647,31.681),(118.646,31.647),(118.726,31.628),(118.774,31.683),(118.799,31.669),(118.783,31.656),(118.804,31.62),(118.869,31.612),(118.887,31.52),(118.846,31.506),(118.884,31.501),(118.87,31.422),(118.812,31.372),(118.771,31.363),(118.755,31.386),(118.72,31.295),(118.7,31.301),(118.762,31.278),(118.788,31.234),(119.105,31.235),(119.182,31.301),(119.239,31.255),(119.338,31.26),(119.357,31.304),(119.381,31.263),(119.363,31.203),(119.406,31.195),(119.392,31.175),(119.532,31.16),(119.577,31.11),(119.624,31.131),(119.649,31.105),(119.63,31.086),(119.635,31.02),(119.584,30.974),(119.557,30.899),(119.577,30.832),(119.525,30.776),(119.48,30.773),(119.483,30.705),(119.445,30.651),(119.409,30.646),(119.389,30.687),(119.312,30.622),(119.239,30.609),(119.266,30.575),(119.24,30.533),(119.275,30.511),(119.328,30.533),(119.349,30.41),(119.4,30.368),(119.35,30.35),(119.327,30.372),(119.248,30.342),(119.227,30.289),(119.091,30.324),(119.058,30.305),(118.955,30.36),(118.88,30.315),(118.882,30.249),(118.931,30.204),(118.846,30.156),(118.897,30.148),(118.869,30.104),(118.903,30.029),(118.896,29.938),(118.84,29.938),(118.842,29.892),(118.755,29.846),(118.745,29.739),(118.647,29.644),(118.574,29.639),(118.501,29.576),(118.496,29.519),(118.384,29.511),(118.348,29.475),(118.309,29.495),(118.315,29.422),(118.247,29.432),(118.199,29.394),(118.137,29.419),(118.144,29.5),(118.015,29.578),(117.934,29.55),(117.812,29.574),(117.708,29.55),(117.657,29.614),(117.543,29.589),(117.531,29.654),(117.453,29.692),(117.456,29.75),(117.41,29.796),(117.422,29.85),(117.359,29.813),(117.337,29.851),(117.293,29.823),(117.259,29.832),(117.254,29.909),(117.213,29.928),(117.135,29.904),(117.131,29.864),(117.074,29.833),(117.136,29.781),(117.109,29.753),(117.113,29.713),(116.996,29.684),(116.839,29.57),(116.784,29.569),(116.787,29.592),(116.761,29.6),(116.722,29.565),(116.652,29.638),(116.681,29.682),(116.718,29.692),(116.675,29.71),(116.753,29.798),(116.812,29.812),(116.883,29.894),(116.9,29.95),(116.869,29.981),(116.834,29.958),(116.83,30.007),(116.801,29.997),(116.765,30.05),(116.667,30.077),(116.587,30.047),(116.542,29.902),(116.468,29.896),(116.262,29.782),(116.209,29.828),(116.136,29.82),(116.133,29.891),(116.074,29.964),(116.092,30.037),(116.066,30.205),(115.995,30.256),(115.981,30.296),(115.905,30.311),(115.917,30.335),(115.886,30.383),(115.946,30.426),(115.896,30.453),(115.922,30.518),(115.877,30.583),(115.819,30.599),(115.813,30.64),(115.763,30.687),(115.788,30.758),(115.845,30.756),(115.871,30.777),(115.848,30.836),(115.866,30.864),(116.011,30.95),(116.072,30.957),(116.06,31.014),(115.941,31.044),(115.874,31.147),(115.771,31.113),(115.692,31.204),(115.646,31.21),(115.582,31.146),(115.543,31.188),(115.527,31.254),(115.457,31.283),(115.442,31.347),(115.372,31.35),(115.394,31.391),(115.374,31.406),(115.391,31.45),(115.372,31.496),(115.418,31.527),(115.44,31.589),(115.488,31.612),(115.478,31.645),(115.497,31.675),(115.679,31.779),(115.736,31.764),(115.769,31.788),(115.815,31.763),(115.911,31.793),(115.894,31.839),(115.935,31.999),(115.92,32.028),(115.944,32.075),(115.927,32.105),(115.942,32.166),(115.913,32.229),(115.884,32.456),(115.863,32.461),(115.884,32.489),(115.845,32.505),(115.93,32.567),(115.892,32.577),(115.842,32.501),(115.785,32.467),(115.771,32.507),(115.744,32.477),(115.699,32.495),(115.665,32.409),(115.656,32.431),(115.627,32.405),(115.605,32.427),(115.564,32.403),(115.571,32.421),(115.524,32.441),(115.478,32.521),(115.409,32.55),(115.411,32.576),(115.311,32.553),(115.302,32.589),(115.2,32.593),(115.195,32.643),(115.22,32.659),(115.179,32.685),(115.183,32.788),(115.213,32.789),(115.19,32.811),(115.199,32.854),(115.156,32.865),(115.141,32.898),(115.027,32.908),(115.033,32.931),(114.946,32.935),(114.891,32.975),(114.893,33.021),(114.938,33.026),(114.897,33.087),(114.903,33.13),(114.955,33.151),(114.994,33.101),(115.137,33.084),(115.195,33.121),(115.302,33.143),(115.296,33.198),(115.327,33.212),(115.336,33.299),(115.362,33.301),(115.345,33.37),(115.313,33.375),(115.33,33.4),(115.316,33.449),(115.348,33.451),(115.348,33.505),(115.367,33.524),(115.397,33.503),(115.423,33.558),(115.641,33.586),(115.602,33.659),(115.602,33.721),(115.563,33.772),(115.581,33.789),(115.614,33.776),(115.634,33.868),(115.546,33.881),(115.593,34.01),(115.65,34.036),(115.654,34.061),(115.778,34.074),(115.846,34.031),(115.851,34.005),(115.878,34.004),(115.888,34.033),(115.905,34.011),(115.968,34.003),(116.001,33.966),(115.987,33.901),(116.06,33.864),(116.056,33.806),(116.162,33.709),(116.264,33.73),(116.409,33.806),(116.433,33.796),(116.438,33.847),(116.559,33.882),(116.564,33.908),(116.642,33.891),(116.649,33.972),(116.528,34.116),(116.566,34.17),(116.544,34.24),(116.583,34.275),(116.504,34.297),(116.457,34.27),(116.451,34.289),(116.365,34.27),(116.364,34.317),(116.214,34.383),(116.162,34.46),(116.204,34.509),(116.196,34.575),(116.241,34.553),(116.281,34.607),(116.318,34.602),(116.375,34.641)),
    ((119.569,25.414),(119.548,25.366),(119.488,25.363),(119.508,25.394),(119.49,25.447),(119.463,25.448),(119.441,25.411),(119.431,25.435),(119.452,25.497),(119.404,25.492),(119.362,25.522),(119.355,25.428),(119.317,25.409),(119.269,25.426),(119.275,25.477),(119.256,25.489),(119.163,25.441),(119.146,25.386),(119.22,25.368),(119.243,25.316),(119.251,25.336),(119.297,25.331),(119.338,25.284),(119.386,25.275),(119.361,25.241),(119.295,25.237),(119.315,25.191),(119.27,25.16),(119.232,25.189),(119.183,25.179),(119.139,25.226),(119.119,25.212),(119.113,25.184),(119.166,25.146),(119.128,25.013),(119.09,25.055),(119.116,25.055),(119.108,25.078),(119.144,25.104),(119.076,25.101),(119.029,25.14),(119.032,25.173),(119.071,25.187),(119.071,25.233),(118.99,25.202),(118.989,25.255),(119.024,25.266),(118.995,25.277),(118.884,25.241),(118.985,25.195),(118.986,25.167),(118.952,25.149),(118.976,25.117),(118.868,25.083),(118.929,25.026),(119.023,25.05),(118.984,24.991),(119.033,24.958),(118.983,24.935),(118.942,24.954),(118.918,24.93),(118.993,24.882),(118.865,24.888),(118.84,24.855),(118.807,24.87),(118.742,24.822),(118.699,24.868),(118.648,24.844),(118.67,24.797),(118.732,24.816),(118.736,24.784),(118.787,24.777),(118.785,24.759),(118.725,24.678),(118.658,24.674),(118.66,24.624),(118.688,24.632),(118.681,24.583),(118.58,24.508),(118.559,24.513),(118.558,24.573),(118.479,24.615),(118.374,24.576),(118.348,24.531),(118.243,24.513),(118.151,24.584),(118.113,24.563),(118.05,24.419),(118.089,24.409),(118.077,24.359),(118.12,24.351),(118.156,24.259),(118.02,24.197),(117.929,24.09),(117.911,24.013),(117.86,24.003),(117.77,23.889),(117.673,23.879),(117.661,23.791),(117.608,23.706),(117.502,23.705),(117.5,23.647),(117.455,23.629),(117.464,23.586),(117.383,23.554),(117.193,23.562),(117.191,23.634),(117.125,23.646),(117.056,23.694),(117.019,23.848),(116.963,23.862),(116.98,23.884),(116.955,23.921),(116.98,23.941),(116.982,24.0),(116.94,24.032),(116.953,24.056),(116.927,24.102),(116.999,24.181),(116.935,24.221),(116.938,24.282),(116.915,24.288),(116.904,24.371),(116.84,24.443),(116.86,24.464),(116.757,24.551),(116.813,24.646),(116.802,24.679),(116.632,24.641),(116.598,24.655),(116.526,24.605),(116.489,24.718),(116.442,24.718),(116.376,24.805),(116.418,24.841),(116.396,24.878),(116.362,24.87),(116.335,24.822),(116.246,24.794),(116.251,24.828),(116.223,24.83),(116.192,24.878),(116.091,24.839),(116.02,24.905),(115.909,24.924),(115.871,24.96),(115.884,24.978),(115.926,24.961),(115.874,25.021),(115.93,25.049),(115.881,25.093),(115.888,25.135),(115.854,25.155),(115.856,25.21),(115.93,25.235),(115.95,25.293),(116.003,25.306),(115.993,25.375),(116.023,25.437),(116.006,25.491),(116.063,25.562),(116.041,25.604),(116.068,25.637),(116.061,25.695),(116.108,25.703),(116.13,25.76),(116.176,25.75),(116.182,25.779),(116.132,25.825),(116.131,25.859),(116.37,25.964),(116.363,26.004),(116.491,26.122),(116.472,26.176),(116.393,26.171),(116.397,26.271),(116.5,26.362),(116.516,26.409),(116.554,26.4),(116.557,26.365),(116.609,26.385),(116.639,26.479),(116.598,26.484),(116.598,26.513),(116.54,26.56),(116.569,26.643),(116.513,26.709),(116.56,26.768),(116.549,26.841),(116.695,26.987),(116.758,26.984),(116.783,27.01),(116.904,27.034),(116.937,27.02),(116.968,27.062),(117.054,27.101),(117.047,27.149),(117.167,27.268),(117.171,27.293),(117.101,27.34),(117.1,27.38),(117.134,27.426),(117.103,27.541),(117.081,27.567),(117.056,27.543),(117.021,27.558),(117.004,27.627),(117.021,27.653),(117.058,27.671),(117.1,27.627),(117.118,27.694),(117.205,27.685),(117.207,27.716),(117.264,27.729),(117.278,27.769),(117.306,27.776),(117.28,27.871),(117.326,27.896),(117.353,27.858),(117.523,27.983),(117.566,27.938),(117.587,27.942),(117.604,27.868),(117.741,27.801),(117.788,27.854),(117.787,27.896),(117.856,27.946),(117.968,27.964),(117.999,27.991),(118.096,27.967),(118.095,28.004),(118.129,28.017),(118.138,28.058),(118.2,28.051),(118.354,28.088),(118.376,28.187),(118.315,28.224),(118.433,28.294),(118.445,28.251),(118.492,28.238),(118.504,28.28),(118.585,28.285),(118.621,28.257),(118.72,28.312),(118.755,28.254),(118.812,28.229),(118.762,28.171),(118.804,28.165),(118.804,28.119),(118.745,28.09),(118.72,28.05),(118.731,27.97),(118.819,27.917),(118.841,27.78),(118.898,27.72),(118.875,27.681),(118.914,27.619),(118.911,27.571),(118.857,27.519),(118.905,27.463),(118.956,27.45),(118.991,27.504),(119.065,27.467),(119.123,27.481),(119.122,27.438),(119.148,27.426),(119.269,27.422),(119.371,27.532),(119.417,27.54),(119.438,27.509),(119.467,27.527),(119.502,27.65),(119.54,27.676),(119.618,27.675),(119.645,27.663),(119.632,27.583),(119.676,27.575),(119.66,27.54),(119.709,27.515),(119.711,27.464),(119.686,27.438),(119.785,27.328),(119.771,27.307),(119.945,27.314),(119.957,27.364),(119.995,27.38),(120.053,27.339),(120.099,27.393),(120.134,27.394),(120.135,27.42),(120.249,27.44),(120.275,27.389),(120.319,27.409),(120.347,27.396),(120.353,27.346),(120.431,27.26),(120.398,27.246),(120.401,27.212),(120.462,27.143),(120.394,27.082),(120.284,27.093),(120.3,27.04),(120.258,27.035),(120.281,26.991),(120.251,26.974),(120.233,26.908),(120.121,26.921),(120.118,26.884),(120.038,26.86),(120.046,26.826),(120.103,26.827),(120.103,26.796),(120.139,26.798),(120.107,26.755),(120.166,26.732),(120.11,26.691),(120.139,26.639),(119.957,26.596),(119.895,26.515),(119.838,26.516),(119.847,26.59),(119.904,26.625),(119.949,26.613),(119.971,26.692),(120.063,26.772),(119.967,26.788),(119.926,26.773),(119.938,26.742),(119.899,26.693),(119.909,26.662),(119.877,26.645),(119.834,26.691),(119.712,26.688),(119.654,26.745),(119.658,26.701),(119.636,26.701),(119.62,26.649),(119.577,26.628),(119.614,26.592),(119.676,26.619),(119.789,26.584),(119.877,26.36),(119.963,26.372),(119.91,26.311),(119.808,26.309),(119.803,26.27),(119.771,26.286),(119.674,26.262),(119.668,26.205),(119.604,26.168),(119.669,26.026),(119.722,26.019),(119.721,25.98),(119.693,25.9),(119.633,25.884),(119.639,25.754),(119.604,25.686),(119.491,25.674),(119.478,25.633),(119.541,25.629),(119.536,25.586),(119.6,25.593),(119.627,25.474),(119.676,25.476),(119.724,25.514),(119.685,25.599),(119.785,25.668),(119.786,25.617),(119.841,25.602),(119.846,25.57),(119.891,25.566),(119.865,25.533),(119.815,25.53),(119.815,25.496),(119.864,25.47),(119.767,25.436),(119.774,25.396),(119.693,25.43),(119.676,25.469),(119.649,25.462),(119.623,25.435),(119.671,25.436),(119.666,25.373),(119.631,25.358),(119.659,25.354),(119.6,25.335),(119.573,25.451),(119.556,25.43),(119.569,25.414)),
    ((113.954,25.619),(113.915,25.729),(114.028,25.894),(114.009,26.017),(114.044,26.077),(114.107,26.07),(114.109,26.101),(114.237,26.152),(114.219,26.167),(114.232,26.182),(114.182,26.215),(114.089,26.169),(114.037,26.189),(113.963,26.151),(113.945,26.164),(113.979,26.238),(114.03,26.267),(114.048,26.338),(114.031,26.377),(114.089,26.412),(114.11,26.483),(114.072,26.488),(114.109,26.572),(114.021,26.588),(113.997,26.616),(113.907,26.616),(113.861,26.665),(113.835,26.805),(113.894,26.861),(113.928,26.949),(113.821,27.038),(113.809,27.099),(113.772,27.103),(113.872,27.285),(113.855,27.306),(113.877,27.383),(113.7,27.333),(113.617,27.346),(113.604,27.387),(113.633,27.406),(113.603,27.421),(113.592,27.468),(113.629,27.515),(113.579,27.538),(113.612,27.634),(113.768,27.809),(113.76,27.855),(113.729,27.877),(113.754,27.936),(113.819,27.982),(113.851,27.974),(113.864,28.005),(113.9,27.988),(113.966,28.018),(113.964,28.04),(114.027,28.032),(114.048,28.058),(113.994,28.164),(114.105,28.182),(114.146,28.248),(114.183,28.251),(114.198,28.291),(114.256,28.324),(114.256,28.395),(114.172,28.436),(114.219,28.485),(114.087,28.557),(114.133,28.607),(114.123,28.69),(114.149,28.725),(114.153,28.835),(114.062,28.847),(114.063,28.9),(114.029,28.892),(114.01,28.956),(113.967,28.944),(113.946,29.019),(113.954,29.096),(114.038,29.155),(114.062,29.204),(114.253,29.235),(114.26,29.345),(114.308,29.366),(114.35,29.324),(114.445,29.35),(114.458,29.326),(114.503,29.324),(114.674,29.396),(114.733,29.395),(114.761,29.363),(114.785,29.387),(114.895,29.397),(114.948,29.465),(114.919,29.483),(114.903,29.471),(114.919,29.455),(114.886,29.437),(114.861,29.477),(114.901,29.53),(114.9,29.507),(114.926,29.522),(114.926,29.499),(114.958,29.501),(114.97,29.52),(114.948,29.543),(114.999,29.572),(115.035,29.547),(115.088,29.561),(115.09,29.521),(115.154,29.511),(115.173,29.593),(115.121,29.597),(115.144,29.649),(115.109,29.664),(115.111,29.684),(115.177,29.655),(115.267,29.66),(115.286,29.619),(115.36,29.646),(115.471,29.74),(115.477,29.808),(115.512,29.841),(115.684,29.849),(115.838,29.749),(115.934,29.722),(116.168,29.828),(116.218,29.824),(116.271,29.785),(116.468,29.896),(116.548,29.906),(116.587,30.047),(116.652,30.077),(116.765,30.05),(116.801,29.997),(116.83,30.007),(116.833,29.959),(116.869,29.981),(116.9,29.95),(116.883,29.894),(116.812,29.812),(116.753,29.798),(116.654,29.695),(116.682,29.677),(116.652,29.638),(116.722,29.565),(116.761,29.6),(116.787,29.592),(116.784,29.569),(116.839,29.57),(116.996,29.684),(117.113,29.713),(117.109,29.753),(117.136,29.781),(117.074,29.833),(117.131,29.864),(117.135,29.904),(117.213,29.928),(117.254,29.909),(117.259,29.832),(117.293,29.823),(117.337,29.851),(117.359,29.813),(117.422,29.85),(117.41,29.796),(117.456,29.75),(117.453,29.692),(117.531,29.654),(117.543,29.589),(117.657,29.614),(117.708,29.55),(117.812,29.574),(117.877,29.547),(117.939,29.55),(118.009,29.579),(118.138,29.507),(118.131,29.426),(118.191,29.405),(118.208,29.348),(118.153,29.287),(118.072,29.289),(118.082,29.233),(118.043,29.211),(118.029,29.17),(118.054,29.117),(118.038,29.098),(118.076,29.075),(118.067,29.049),(118.098,28.999),(118.108,29.013),(118.101,28.991),(118.128,29.017),(118.134,28.984),(118.181,28.981),(118.229,28.944),(118.195,28.904),(118.271,28.919),(118.301,28.826),(118.384,28.789),(118.392,28.701),(118.434,28.677),(118.41,28.569),(118.446,28.514),(118.415,28.498),(118.482,28.471),(118.432,28.399),(118.483,28.318),(118.315,28.224),(118.376,28.187),(118.354,28.088),(118.2,28.051),(118.138,28.058),(118.129,28.017),(118.095,28.004),(118.096,27.967),(117.999,27.991),(117.968,27.964),(117.856,27.946),(117.787,27.896),(117.788,27.854),(117.741,27.801),(117.604,27.868),(117.587,27.942),(117.566,27.938),(117.523,27.983),(117.353,27.858),(117.326,27.896),(117.28,27.871),(117.306,27.776),(117.278,27.769),(117.264,27.729),(117.207,27.716),(117.205,27.685),(117.118,27.694),(117.1,27.627),(117.058,27.671),(117.021,27.653),(117.004,27.627),(117.021,27.558),(117.056,27.543),(117.081,27.567),(117.103,27.541),(117.134,27.426),(117.1,27.38),(117.101,27.34),(117.171,27.293),(117.167,27.268),(117.047,27.149),(117.054,27.101),(116.968,27.062),(116.937,27.02),(116.904,27.034),(116.783,27.01),(116.758,26.984),(116.695,26.987),(116.549,26.841),(116.56,26.768),(116.513,26.709),(116.569,26.643),(116.54,26.56),(116.638,26.467),(116.602,26.373),(116.557,26.365),(116.554,26.4),(116.516,26.409),(116.5,26.362),(116.397,26.271),(116.393,26.171),(116.472,26.176),(116.491,26.122),(116.363,26.004),(116.37,25.964),(116.131,25.859),(116.132,25.825),(116.182,25.779),(116.176,25.75),(116.13,25.76),(116.108,25.703),(116.061,25.695),(116.068,25.637),(116.041,25.604),(116.063,25.562),(116.006,25.491),(116.023,25.437),(115.993,25.375),(116.003,25.306),(115.95,25.293),(115.93,25.235),(115.856,25.21),(115.854,25.155),(115.888,25.135),(115.881,25.093),(115.93,25.049),(115.876,25.007),(115.926,24.961),(115.871,24.969),(115.9,24.875),(115.862,24.864),(115.866,24.891),(115.825,24.91),(115.807,24.86),(115.783,24.863),(115.793,24.837),(115.757,24.748),(115.771,24.709),(115.809,24.701),(115.799,24.676),(115.762,24.669),(115.849,24.566),(115.688,24.546),(115.672,24.605),(115.574,24.617),(115.527,24.716),(115.407,24.794),(115.361,24.735),(115.279,24.755),(115.121,24.665),(115.055,24.71),(115.025,24.67),(114.94,24.65),(114.914,24.665),(114.869,24.562),(114.85,24.603),(114.827,24.589),(114.753,24.617),(114.731,24.611),(114.738,24.565),(114.706,24.526),(114.666,24.584),(114.593,24.538),(114.532,24.56),(114.432,24.487),(114.402,24.499),(114.393,24.564),(114.356,24.591),(114.305,24.575),(114.259,24.642),(114.175,24.646),(114.191,24.657),(114.17,24.69),(114.273,24.701),(114.336,24.75),(114.344,24.812),(114.403,24.878),(114.396,24.951),(114.424,24.973),(114.511,25.003),(114.562,25.078),(114.641,25.074),(114.735,25.122),(114.736,25.155),(114.681,25.195),(114.749,25.232),(114.743,25.275),(114.715,25.315),(114.636,25.326),(114.601,25.387),(114.536,25.417),(114.482,25.371),(114.448,25.387),(114.429,25.342),(114.382,25.316),(114.307,25.335),(114.302,25.29),(114.14,25.312),(114.033,25.252),(114.015,25.281),(114.057,25.312),(114.03,25.329),(114.051,25.364),(114.024,25.374),(114.046,25.386),(113.988,25.404),(114.004,25.443),(113.941,25.448),(113.959,25.484),(113.945,25.498),(113.988,25.531),(113.994,25.563),(113.954,25.619)),
    ((115.488,35.881),(115.364,35.78),(115.335,35.799),(115.363,35.973),(115.448,36.013),(115.442,36.057),(115.484,36.126),(115.478,36.242),(115.463,36.276),(115.42,36.289),(115.423,36.324),(115.361,36.312),(115.347,36.391),(115.298,36.414),(115.317,36.454),(115.273,36.498),(115.331,36.549),(115.355,36.628),(115.445,36.69),(115.479,36.76),(115.687,36.809),(115.701,36.868),(115.766,36.909),(115.762,36.938),(115.797,36.968),(115.777,36.992),(115.884,37.1),(115.905,37.208),(115.971,37.241),(115.977,37.338),(116.167,37.386),(116.236,37.362),(116.286,37.404),(116.269,37.431),(116.228,37.425),(116.244,37.456),(116.225,37.48),(116.279,37.469),(116.299,37.51),(116.276,37.522),(116.3,37.569),(116.335,37.575),(116.32,37.581),(116.38,37.563),(116.369,37.527),(116.435,37.474),(116.457,37.515),(116.611,37.626),(116.68,37.729),(116.725,37.745),(116.724,37.768),(116.748,37.76),(116.747,37.805),(116.804,37.851),(117.027,37.833),(117.09,37.85),(117.265,37.839),(117.339,37.863),(117.436,37.853),(117.512,37.942),(117.557,38.058),(117.728,38.092),(117.771,38.135),(117.769,38.163),(117.802,38.174),(117.808,38.228),(117.896,38.302),(117.897,38.28),(118.033,38.203),(118.135,38.308),(118.075,38.163),(118.421,38.107),(118.483,38.124),(118.553,38.057),(118.598,38.079),(118.614,38.135),(118.727,38.154),(118.854,38.155),(118.959,38.11),(119.005,37.993),(119.117,37.912),(119.129,37.814),(119.213,37.814),(119.219,37.76),(119.275,37.731),(119.248,37.699),(119.081,37.697),(118.998,37.633),(118.94,37.527),(118.987,37.34),(119.046,37.3),(119.067,37.242),(119.129,37.255),(119.158,37.289),(119.171,37.277),(119.143,37.28),(119.13,37.233),(119.181,37.227),(119.199,37.257),(119.197,37.194),(119.213,37.226),(119.291,37.21),(119.3,37.144),(119.328,37.116),(119.49,37.135),(119.481,37.157),(119.504,37.129),(119.561,37.124),(119.567,37.101),(119.745,37.136),(119.827,37.224),(119.887,37.253),(119.861,37.263),(119.895,37.268),(119.884,37.312),(119.839,37.344),(119.845,37.377),(119.93,37.388),(119.95,37.421),(120.145,37.482),(120.247,37.557),(120.209,37.589),(120.211,37.617),(120.267,37.629),(120.274,37.651),(120.219,37.69),(120.369,37.698),(120.451,37.757),(120.586,37.763),(120.752,37.839),(120.937,37.823),(120.946,37.783),(121.038,37.719),(121.149,37.72),(121.15,37.62),(121.217,37.584),(121.306,37.583),(121.362,37.602),(121.346,37.635),(121.389,37.628),(121.439,37.599),(121.387,37.592),(121.402,37.557),(121.46,37.524),(121.478,37.477),(121.565,37.44),(121.639,37.495),(121.683,37.473),(121.925,37.474),(121.997,37.494),(122.018,37.531),(122.074,37.539),(122.067,37.568),(122.108,37.551),(122.123,37.568),(122.172,37.542),(122.13,37.509),(122.179,37.433),(122.235,37.47),(122.315,37.417),(122.488,37.436),(122.496,37.414),(122.554,37.407),(122.642,37.429),(122.677,37.414),(122.683,37.431),(122.716,37.4),(122.69,37.374),(122.651,37.389),(122.612,37.367),(122.594,37.338),(122.615,37.336),(122.575,37.302),(122.568,37.26),(122.593,37.262),(122.629,37.195),(122.575,37.181),(122.582,37.148),(122.511,37.151),(122.485,37.129),(122.462,37.04),(122.586,37.044),(122.55,37.016),(122.546,36.92),(122.517,36.891),(122.435,36.915),(122.457,36.869),(122.385,36.866),(122.349,36.829),(122.182,36.842),(122.178,36.894),(122.118,36.896),(122.139,36.944),(122.053,36.905),(122.041,36.871),(122.015,36.961),(121.983,36.959),(121.769,36.875),(121.729,36.827),(121.633,36.801),(121.652,36.725),(121.604,36.739),(121.599,36.767),(121.557,36.765),(121.572,36.733),(121.533,36.731),(121.547,36.748),(121.481,36.775),(121.565,36.831),(121.457,36.754),(121.395,36.738),(121.401,36.702),(121.286,36.7),(121.037,36.575),(120.956,36.576),(120.906,36.624),(120.848,36.619),(120.983,36.546),(120.955,36.508),(120.969,36.473),(120.909,36.451),(120.937,36.416),(120.872,36.367),(120.833,36.467),(120.759,36.463),(120.761,36.435),(120.696,36.393),(120.745,36.329),(120.663,36.332),(120.654,36.284),(120.688,36.278),(120.713,36.127),(120.551,36.112),(120.549,36.093),(120.479,36.092),(120.433,36.053),(120.342,36.043),(120.323,36.061),(120.288,36.043),(120.363,36.196),(120.32,36.232),(120.294,36.22),(120.312,36.187),(120.284,36.18),(120.262,36.199),(120.229,36.188),(120.218,36.212),(120.179,36.202),(120.141,36.174),(120.137,36.135),(120.109,36.128),(120.117,36.104),(120.242,36.061),(120.197,35.997),(120.258,36.025),(120.252,35.98),(120.31,36.015),(120.306,35.973),(120.252,35.96),(120.201,35.892),(120.168,35.892),(120.21,35.948),(120.071,35.882),(120.014,35.715),(119.981,35.716),(119.959,35.761),(119.927,35.761),(119.922,35.737),(119.954,35.718),(119.911,35.675),(119.924,35.636),(119.869,35.609),(119.832,35.619),(119.825,35.646),(119.793,35.616),(119.791,35.576),(119.755,35.587),(119.749,35.619),(119.663,35.59),(119.58,35.407),(119.588,35.364),(119.544,35.349),(119.538,35.296),(119.494,35.319),(119.418,35.244),(119.394,35.145),(119.419,35.123),(119.397,35.092),(119.307,35.077),(119.287,35.115),(119.25,35.125),(119.141,35.098),(119.117,35.056),(118.866,35.03),(118.859,34.941),(118.803,34.846),(118.768,34.845),(118.774,34.796),(118.73,34.789),(118.739,34.768),(118.717,34.765),(118.784,34.723),(118.684,34.679),(118.607,34.695),(118.601,34.716),(118.523,34.712),(118.497,34.673),(118.463,34.668),(118.476,34.624),(118.433,34.62),(118.44,34.509),(118.405,34.429),(118.292,34.426),(118.278,34.405),(118.221,34.406),(118.218,34.38),(118.171,34.382),(118.179,34.454),(118.134,34.484),(118.186,34.545),(118.079,34.57),(118.115,34.615),(118.101,34.652),(118.009,34.648),(117.958,34.68),(117.91,34.671),(117.903,34.646),(117.794,34.652),(117.802,34.52),(117.685,34.548),(117.659,34.502),(117.61,34.491),(117.591,34.462),(117.488,34.467),(117.363,34.59),(117.269,34.533),(117.254,34.451),(117.201,34.442),(117.226,34.473),(117.176,34.471),(117.138,34.634),(117.066,34.648),(117.089,34.706),(117.051,34.771),(116.968,34.841),(116.977,34.871),(116.822,34.93),(116.81,34.97),(116.805,34.929),(116.79,34.976),(116.798,34.939),(116.771,34.917),(116.632,34.941),(116.444,34.896),(116.408,34.852),(116.403,34.757),(116.366,34.743),(116.394,34.707),(116.375,34.641),(116.318,34.602),(116.281,34.607),(116.241,34.553),(116.202,34.579),(116.147,34.554),(116.102,34.606),(116.038,34.594),(116.004,34.624),(115.974,34.59),(115.83,34.563),(115.704,34.601),(115.685,34.556),(115.522,34.579),(115.445,34.675),(115.43,34.803),(115.314,34.86),(115.244,34.851),(115.253,34.907),(115.189,34.915),(115.22,34.961),(115.157,34.959),(115.129,35.005),(115.029,34.973),(114.951,34.99),(114.924,34.969),(114.878,35.025),(114.868,35.0),(114.826,35.012),(114.855,35.037),(114.819,35.053),(114.884,35.1),(114.846,35.166),(114.932,35.197),(114.931,35.249),(115.015,35.318),(115.024,35.373),(115.074,35.375),(115.092,35.417),(115.123,35.4),(115.138,35.422),(115.238,35.424),(115.358,35.499),(115.346,35.554),(115.384,35.569),(115.498,35.72),(115.694,35.755),(115.735,35.833),(115.876,35.859),(115.89,35.898),(115.871,35.915),(115.908,35.928),(115.912,35.961),(116.048,35.97),(116.101,36.111),(115.92,36.021),(115.813,36.013),(115.771,35.974),(115.7,35.967),(115.646,35.921),(115.59,35.924),(115.514,35.891),(115.502,35.914),(115.488,35.881)),
    ((115.484,36.149),(115.442,36.057),(115.448,36.013),(115.357,35.955),(115.365,35.898),(115.34,35.871),(115.335,35.799),(115.362,35.781),(115.504,35.89),(115.59,35.924),(115.646,35.921),(115.7,35.967),(115.775,35.975),(115.801,36.009),(115.92,36.021),(115.994,36.048),(116.058,36.105),(116.101,36.105),(116.048,35.97),(115.912,35.961),(115.908,35.928),(115.871,35.915),(115.89,35.898),(115.876,35.859),(115.735,35.833),(115.694,35.755),(115.486,35.711),(115.384,35.569),(115.346,35.554),(115.357,35.497),(115.238,35.424),(115.138,35.422),(115.123,35.4),(115.092,35.417),(115.074,35.375),(115.024,35.373),(115.015,35.318),(114.931,35.249),(114.932,35.197),(114.842,35.16),(114.884,35.104),(114.83,35.071),(114.82,35.051),(114.855,35.035),(114.825,35.013),(114.868,35.0),(114.878,35.025),(114.924,34.969),(114.951,34.99),(115.029,34.973),(115.129,35.005),(115.157,34.959),(115.22,34.961),(115.189,34.915),(115.253,34.907),(115.244,34.851),(115.314,34.86),(115.428,34.806),(115.462,34.638),(115.549,34.569),(115.685,34.556),(115.704,34.601),(115.83,34.563),(115.974,34.59),(116.004,34.624),(116.038,34.594),(116.102,34.606),(116.147,34.554),(116.196,34.575),(116.204,34.509),(116.162,34.46),(116.214,34.383),(116.364,34.317),(116.373,34.267),(116.447,34.289),(116.467,34.271),(116.504,34.297),(116.583,34.275),(116.544,34.24),(116.566,34.17),(116.528,34.116),(116.652,33.963),(116.642,33.891),(116.564,33.908),(116.559,33.882),(116.438,33.847),(116.433,33.796),(116.409,33.806),(116.264,33.73),(116.162,33.709),(116.056,33.806),(116.06,33.864),(115.987,33.901),(116.001,33.966),(115.968,34.003),(115.905,34.011),(115.888,34.033),(115.878,34.004),(115.851,34.005),(115.846,34.031),(115.778,34.074),(115.654,34.061),(115.65,34.036),(115.593,34.01),(115.546,33.881),(115.634,33.868),(115.614,33.776),(115.581,33.789),(115.563,33.772),(115.602,33.721),(115.602,33.659),(115.641,33.586),(115.423,33.558),(115.397,33.503),(115.367,33.524),(115.348,33.505),(115.348,33.451),(115.316,33.449),(115.33,33.4),(115.313,33.375),(115.345,33.37),(115.362,33.301),(115.336,33.299),(115.327,33.212),(115.296,33.198),(115.302,33.143),(115.195,33.121),(115.137,33.084),(114.994,33.101),(114.955,33.151),(114.903,33.13),(114.897,33.087),(114.938,33.026),(114.893,33.021),(114.891,32.975),(114.946,32.935),(115.033,32.931),(115.027,32.908),(115.141,32.898),(115.156,32.865),(115.199,32.854),(115.19,32.811),(115.213,32.789),(115.183,32.788),(115.179,32.685),(115.22,32.659),(115.195,32.643),(115.2,32.593),(115.302,32.589),(115.311,32.553),(115.411,32.576),(115.409,32.55),(115.478,32.521),(115.524,32.441),(115.571,32.421),(115.564,32.403),(115.605,32.427),(115.627,32.405),(115.656,32.431),(115.665,32.409),(115.699,32.495),(115.744,32.477),(115.771,32.507),(115.785,32.467),(115.842,32.501),(115.892,32.577),(115.93,32.567),(115.845,32.505),(115.884,32.489),(115.863,32.461),(115.884,32.456),(115.913,32.229),(115.942,32.166),(115.927,32.105),(115.944,32.075),(115.92,32.028),(115.935,31.999),(115.894,31.839),(115.911,31.793),(115.815,31.763),(115.769,31.788),(115.736,31.764),(115.679,31.779),(115.497,31.675),(115.478,31.645),(115.488,31.612),(115.44,31.589),(115.418,31.527),(115.372,31.496),(115.391,31.45),(115.374,31.406),(115.308,31.383),(115.251,31.393),(115.261,31.413),(115.211,31.446),(115.236,31.556),(115.193,31.564),(115.165,31.605),(115.126,31.6),(115.097,31.509),(115.024,31.529),(114.998,31.472),(114.969,31.497),(114.938,31.471),(114.87,31.479),(114.83,31.459),(114.783,31.485),(114.779,31.521),(114.697,31.526),(114.7,31.547),(114.643,31.583),(114.573,31.555),(114.549,31.624),(114.592,31.702),(114.583,31.766),(114.552,31.769),(114.533,31.741),(114.513,31.769),(114.509,31.741),(114.447,31.729),(114.295,31.752),(114.237,31.843),(114.181,31.854),(114.135,31.844),(114.09,31.782),(113.979,31.754),(113.936,31.88),(113.854,31.844),(113.833,31.919),(113.806,31.932),(113.817,31.967),(113.759,31.988),(113.792,32.036),(113.729,32.084),(113.723,32.125),(113.751,32.117),(113.784,32.187),(113.739,32.256),(113.771,32.276),(113.753,32.337),(113.772,32.362),(113.749,32.364),(113.753,32.389),(113.719,32.418),(113.665,32.423),(113.624,32.361),(113.594,32.366),(113.606,32.351),(113.552,32.33),(113.56,32.306),(113.53,32.331),(113.424,32.27),(113.319,32.319),(113.332,32.343),(113.212,32.433),(113.159,32.411),(113.15,32.377),(113.028,32.426),(112.995,32.411),(112.988,32.373),(112.912,32.392),(112.88,32.375),(112.871,32.398),(112.764,32.343),(112.546,32.404),(112.531,32.378),(112.478,32.381),(112.451,32.344),(112.361,32.366),(112.328,32.322),(112.228,32.387),(112.173,32.386),(112.175,32.409),(112.145,32.408),(112.165,32.386),(112.147,32.383),(112.081,32.423),(112.064,32.475),(112.008,32.451),(111.951,32.517),(111.882,32.507),(111.722,32.604),(111.646,32.606),(111.642,32.635),(111.573,32.595),(111.473,32.719),(111.426,32.733),(111.461,32.728),(111.467,32.772),(111.424,32.752),(111.381,32.829),(111.294,32.86),(111.28,32.905),(111.247,32.889),(111.246,32.944),(111.274,32.973),(111.238,33.041),(111.148,33.043),(111.193,33.072),(111.18,33.116),(111.095,33.181),(111.038,33.161),(111.057,33.193),(110.983,33.271),(111.001,33.324),(111.03,33.34),(110.997,33.437),(111.027,33.47),(111.004,33.579),(110.839,33.667),(110.782,33.797),(110.742,33.799),(110.668,33.854),(110.613,33.852),(110.588,33.888),(110.678,33.95),(110.625,34.034),(110.589,34.023),(110.582,34.043),(110.592,34.103),(110.643,34.161),(110.438,34.246),(110.429,34.288),(110.501,34.321),(110.503,34.346),(110.477,34.409),(110.41,34.422),(110.368,34.495),(110.365,34.533),(110.405,34.56),(110.367,34.567),(110.38,34.601),(110.426,34.589),(110.467,34.619),(110.541,34.582),(110.611,34.607),(110.707,34.604),(110.755,34.654),(110.825,34.626),(110.884,34.643),(110.92,34.73),(110.977,34.707),(111.123,34.76),(111.161,34.815),(111.229,34.79),(111.259,34.821),(111.29,34.807),(111.344,34.832),(111.396,34.815),(111.439,34.839),(111.571,34.844),(111.622,34.917),(111.682,34.95),(111.666,34.986),(111.801,35.028),(111.816,35.068),(111.952,35.083),(112.054,35.045),(112.067,35.152),(112.041,35.194),(112.082,35.225),(112.06,35.28),(112.243,35.235),(112.302,35.254),(112.29,35.218),(112.402,35.242),(112.568,35.212),(112.638,35.227),(112.618,35.247),(112.636,35.266),(112.708,35.218),(112.713,35.188),(112.727,35.21),(112.773,35.208),(112.816,35.258),(112.909,35.246),(112.937,35.285),(112.988,35.29),(112.997,35.362),(113.138,35.336),(113.19,35.449),(113.304,35.427),(113.307,35.461),(113.324,35.454),(113.295,35.468),(113.312,35.481),(113.349,35.469),(113.416,35.517),(113.507,35.517),(113.507,35.566),(113.559,35.622),(113.549,35.658),(113.625,35.633),(113.623,35.675),(113.593,35.693),(113.6,35.775),(113.582,35.792),(113.605,35.801),(113.583,35.822),(113.657,35.837),(113.638,35.87),(113.655,35.918),(113.639,35.989),(113.687,35.987),(113.699,36.022),(113.661,36.035),(113.688,36.062),(113.656,36.127),(113.714,36.134),(113.651,36.174),(113.699,36.184),(113.699,36.215),(113.673,36.213),(113.718,36.266),(113.732,36.364),(113.818,36.332),(113.883,36.354),(113.912,36.315),(113.958,36.337),(113.954,36.358),(113.994,36.314),(114.003,36.335),(113.978,36.358),(114.025,36.355),(114.027,36.325),(114.059,36.328),(114.038,36.305),(114.068,36.273),(114.141,36.28),(114.179,36.243),(114.212,36.273),(114.24,36.252),(114.346,36.256),(114.356,36.23),(114.568,36.152),(114.589,36.119),(114.735,36.156),(114.771,36.125),(114.913,36.141),(114.921,36.049),(114.999,36.07),(115.047,36.113),(115.061,36.176),(115.105,36.173),(115.125,36.211),(115.242,36.191),(115.319,36.088),(115.366,36.1),(115.405,36.16),(115.413,36.139),(115.455,36.172),(115.484,36.149)),
    ((110.985,33.256),(111.047,33.203),(111.017,33.174),(111.041,33.157),(111.095,33.181),(111.18,33.116),(111.193,33.072),(111.148,33.043),(111.238,33.041),(111.274,32.973),(111.246,32.944),(111.247,32.889),(111.28,32.905),(111.294,32.86),(111.381,32.829),(111.424,32.752),(111.467,32.772),(111.461,32.728),(111.426,32.733),(111.473,32.719),(111.573,32.595),(111.642,32.635),(111.646,32.606),(111.722,32.604),(111.882,32.507),(111.951,32.517),(112.008,32.451),(112.064,32.475),(112.081,32.423),(112.147,32.383),(112.165,32.386),(112.145,32.408),(112.175,32.409),(112.173,32.386),(112.228,32.387),(112.328,32.322),(112.361,32.366),(112.451,32.344),(112.478,32.381),(112.531,32.378),(112.546,32.404),(112.764,32.343),(112.871,32.398),(112.88,32.375),(112.912,32.392),(112.988,32.373),(112.995,32.411),(113.028,32.426),(113.15,32.377),(113.159,32.411),(113.212,32.433),(113.332,32.343),(113.319,32.319),(113.424,32.27),(113.53,32.331),(113.56,32.306),(113.552,32.33),(113.606,32.351),(113.594,32.366),(113.624,32.361),(113.665,32.423),(113.719,32.418),(113.753,32.389),(113.749,32.364),(113.772,32.362),(113.753,32.334),(113.774,32.305),(113.739,32.256),(113.784,32.187),(113.751,32.117),(113.723,32.125),(113.729,32.084),(113.792,32.036),(113.759,31.988),(113.817,31.967),(113.806,31.932),(113.833,31.919),(113.854,31.844),(113.936,31.88),(113.979,31.754),(114.09,31.782),(114.135,31.844),(114.181,31.854),(114.237,31.843),(114.295,31.752),(114.447,31.729),(114.509,31.741),(114.513,31.769),(114.533,31.741),(114.552,31.769),(114.585,31.765),(114.592,31.701),(114.55,31.645),(114.562,31.562),(114.643,31.583),(114.7,31.547),(114.697,31.526),(114.779,31.521),(114.783,31.485),(114.83,31.459),(114.87,31.479),(114.938,31.471),(114.969,31.497),(114.998,31.472),(115.024,31.529),(115.097,31.509),(115.126,31.6),(115.174,31.603),(115.193,31.564),(115.236,31.556),(115.211,31.446),(115.261,31.413),(115.251,31.393),(115.392,31.394),(115.372,31.35),(115.442,31.347),(115.457,31.283),(115.527,31.254),(115.569,31.153),(115.592,31.146),(115.655,31.212),(115.701,31.201),(115.771,31.113),(115.874,31.147),(115.941,31.044),(116.06,31.014),(116.072,30.957),(116.011,30.95),(115.866,30.864),(115.848,30.836),(115.871,30.777),(115.845,30.756),(115.788,30.758),(115.763,30.687),(115.813,30.64),(115.819,30.599),(115.877,30.583),(115.922,30.518),(115.896,30.453),(115.946,30.426),(115.886,30.383),(115.917,30.335),(115.905,30.311),(115.981,30.296),(115.995,30.256),(116.066,30.205),(116.092,30.037),(116.074,29.964),(116.133,29.891),(116.136,29.82),(115.934,29.722),(115.838,29.749),(115.684,29.849),(115.512,29.841),(115.477,29.808),(115.471,29.74),(115.36,29.646),(115.286,29.619),(115.267,29.66),(115.177,29.655),(115.111,29.684),(115.109,29.664),(115.144,29.649),(115.121,29.597),(115.173,29.593),(115.154,29.511),(115.09,29.521),(115.088,29.561),(115.035,29.547),(114.999,29.572),(114.948,29.543),(114.97,29.52),(114.958,29.501),(114.926,29.499),(114.926,29.522),(114.9,29.507),(114.901,29.53),(114.861,29.477),(114.886,29.437),(114.919,29.455),(114.903,29.471),(114.919,29.483),(114.948,29.465),(114.928,29.417),(114.885,29.394),(114.785,29.387),(114.761,29.363),(114.733,29.395),(114.674,29.396),(114.503,29.324),(114.458,29.326),(114.445,29.35),(114.354,29.324),(114.312,29.366),(114.274,29.354),(114.253,29.235),(114.063,29.205),(114.038,29.155),(113.956,29.099),(113.942,29.048),(113.896,29.03),(113.883,29.066),(113.832,29.07),(113.826,29.104),(113.773,29.095),(113.745,29.059),(113.73,29.106),(113.689,29.079),(113.662,29.168),(113.693,29.227),(113.607,29.254),(113.668,29.382),(113.729,29.394),(113.756,29.447),(113.689,29.51),(113.632,29.519),(113.741,29.589),(113.704,29.635),(113.672,29.639),(113.665,29.685),(113.609,29.667),(113.539,29.685),(113.572,29.85),(113.378,29.704),(113.156,29.458),(113.101,29.46),(113.091,29.432),(113.059,29.522),(112.95,29.474),(112.912,29.616),(113.006,29.694),(113.031,29.768),(112.938,29.682),(112.924,29.767),(112.94,29.78),(112.91,29.804),(112.794,29.736),(112.789,29.681),(112.718,29.652),(112.684,29.59),(112.627,29.617),(112.572,29.624),(112.536,29.601),(112.5,29.63),(112.44,29.634),(112.396,29.563),(112.32,29.541),(112.283,29.495),(112.304,29.586),(112.235,29.616),(112.245,29.66),(112.199,29.618),(112.173,29.661),(112.066,29.682),(112.064,29.771),(112.063,29.743),(111.956,29.796),(111.965,29.836),(111.862,29.857),(111.808,29.904),(111.736,29.921),(111.683,29.885),(111.554,29.895),(111.522,29.929),(111.397,29.913),(111.384,29.95),(111.344,29.945),(111.243,30.041),(110.931,30.063),(110.924,30.113),(110.814,30.127),(110.747,30.113),(110.757,30.055),(110.713,30.033),(110.653,30.078),(110.602,30.055),(110.497,30.055),(110.496,30.016),(110.558,29.988),(110.516,29.96),(110.499,29.91),(110.538,29.897),(110.552,29.848),(110.615,29.831),(110.644,29.776),(110.563,29.713),(110.516,29.691),(110.466,29.714),(110.447,29.665),(110.371,29.634),(110.344,29.668),(110.298,29.665),(110.246,29.732),(110.114,29.79),(109.792,29.764),(109.753,29.74),(109.773,29.725),(109.763,29.691),(109.715,29.674),(109.718,29.614),(109.664,29.601),(109.652,29.627),(109.611,29.635),(109.56,29.607),(109.517,29.627),(109.49,29.554),(109.462,29.555),(109.465,29.514),(109.432,29.529),(109.44,29.492),(109.416,29.497),(109.405,29.47),(109.419,29.449),(109.371,29.42),(109.392,29.373),(109.345,29.37),(109.353,29.284),(109.259,29.223),(109.277,29.125),(109.243,29.116),(109.172,29.179),(109.14,29.17),(109.11,29.216),(109.143,29.271),(109.107,29.289),(109.113,29.361),(109.081,29.392),(109.054,29.403),(109.034,29.36),(108.919,29.328),(108.944,29.411),(108.867,29.473),(108.909,29.595),(108.869,29.599),(108.887,29.634),(108.83,29.652),(108.835,29.67),(108.82,29.633),(108.782,29.637),(108.787,29.692),(108.753,29.689),(108.784,29.656),(108.761,29.646),(108.711,29.68),(108.715,29.699),(108.688,29.69),(108.662,29.854),(108.602,29.866),(108.579,29.847),(108.523,29.765),(108.55,29.746),(108.505,29.731),(108.507,29.709),(108.438,29.741),(108.445,29.776),(108.422,29.774),(108.404,29.836),(108.367,29.82),(108.387,29.861),(108.429,29.881),(108.519,29.868),(108.543,29.998),(108.532,30.056),(108.514,30.058),(108.568,30.157),(108.553,30.164),(108.583,30.254),(108.461,30.36),(108.402,30.376),(108.431,30.416),(108.416,30.48),(108.509,30.504),(108.569,30.47),(108.649,30.538),(108.641,30.575),(108.667,30.589),(108.691,30.587),(108.729,30.504),(108.79,30.514),(108.812,30.492),(108.968,30.625),(109.043,30.656),(109.119,30.64),(109.084,30.599),(109.145,30.521),(109.304,30.632),(109.361,30.556),(109.338,30.521),(109.355,30.487),(109.459,30.616),(109.529,30.664),(109.534,30.64),(109.574,30.647),(109.591,30.694),(109.652,30.724),(109.657,30.761),(109.719,30.779),(109.73,30.815),(109.895,30.9),(110.008,30.885),(110.021,30.83),(110.083,30.8),(110.174,30.98),(110.137,30.987),(110.121,31.09),(110.186,31.126),(110.2,31.159),(110.16,31.257),(110.144,31.39),(110.115,31.413),(110.055,31.411),(109.988,31.476),(109.946,31.47),(109.982,31.514),(109.896,31.52),(109.839,31.556),(109.729,31.549),(109.766,31.604),(109.738,31.629),(109.733,31.7),(109.586,31.728),(109.606,31.744),(109.593,31.789),(109.639,31.812),(109.586,31.901),(109.631,31.944),(109.588,32.025),(109.623,32.104),(109.59,32.15),(109.604,32.206),(109.551,32.226),(109.496,32.301),(109.527,32.434),(109.579,32.511),(109.638,32.542),(109.625,32.598),(109.728,32.608),(109.817,32.578),(109.911,32.593),(110.028,32.549),(110.085,32.581),(110.091,32.617),(110.166,32.595),(110.205,32.628),(110.155,32.69),(110.16,32.768),(110.131,32.772),(110.142,32.81),(109.991,32.887),(109.864,32.914),(109.79,32.883),(109.765,32.91),(109.792,33.07),(109.688,33.117),(109.577,33.11),(109.439,33.152),(109.515,33.238),(109.602,33.233),(109.62,33.275),(109.719,33.234),(109.853,33.248),(110.031,33.192),(110.165,33.21),(110.232,33.159),(110.338,33.161),(110.372,33.188),(110.472,33.172),(110.534,33.253),(110.564,33.255),(110.602,33.155),(110.658,33.154),(110.712,33.097),(110.746,33.147),(110.82,33.153),(110.831,33.203),(110.914,33.207),(110.985,33.256)),
    ((109.905,26.672),(109.827,26.606),(109.893,26.527),(109.856,26.466),(109.929,26.478),(109.953,26.435),(109.987,26.433),(109.979,26.389),(110.019,26.348),(109.984,26.279),(109.993,26.239),(109.907,26.146),(109.869,26.031),(109.815,26.041),(109.792,26.018),(109.783,25.989),(109.827,25.917),(109.813,25.88),(109.686,25.881),(109.683,25.934),(109.725,26.003),(109.652,26.015),(109.636,26.048),(109.519,25.997),(109.454,26.056),(109.45,26.103),(109.505,26.098),(109.515,26.125),(109.477,26.148),(109.44,26.239),(109.468,26.314),(109.349,26.265),(109.278,26.309),(109.32,26.419),(109.382,26.455),(109.363,26.473),(109.409,26.537),(109.357,26.658),(109.318,26.654),(109.284,26.699),(109.362,26.695),(109.382,26.728),(109.413,26.721),(109.462,26.763),(109.525,26.747),(109.515,26.717),(109.613,26.687),(109.661,26.71),(109.644,26.738),(109.662,26.774),(109.755,26.755),(109.804,26.786),(109.845,26.72),(109.932,26.719),(109.942,26.679),(109.905,26.672)),
    ((113.955,25.615),(113.994,25.563),(113.988,25.531),(113.945,25.498),(113.944,25.439),(113.888,25.438),(113.879,25.382),(113.815,25.329),(113.759,25.33),(113.748,25.366),(113.583,25.307),(113.582,25.343),(113.544,25.368),(113.446,25.36),(113.419,25.399),(113.375,25.401),(113.36,25.438),(113.314,25.443),(113.294,25.518),(113.284,25.495),(113.229,25.512),(113.181,25.472),(113.152,25.493),(113.119,25.448),(113.131,25.414),(113.093,25.418),(113.024,25.347),(112.969,25.351),(112.927,25.298),(112.895,25.34),(112.854,25.338),(112.869,25.249),(112.993,25.248),(113.035,25.202),(112.97,25.151),(113.019,25.084),(112.979,25.034),(113.013,24.948),(112.984,24.922),(112.784,24.896),(112.783,24.943),(112.745,24.958),(112.716,25.026),(112.714,25.083),(112.66,25.133),(112.508,25.138),(112.459,25.152),(112.445,25.187),(112.406,25.141),(112.365,25.192),(112.303,25.157),(112.198,25.188),(112.156,25.027),(112.122,24.985),(112.129,24.951),(112.176,24.926),(112.154,24.838),(112.06,24.8),(112.024,24.74),(111.962,24.771),(111.876,24.757),(111.78,24.787),(111.69,24.779),(111.64,24.726),(111.64,24.683),(111.594,24.693),(111.571,24.646),(111.534,24.637),(111.432,24.687),(111.479,24.798),(111.45,24.857),(111.471,24.93),(111.432,24.968),(111.469,25.02),(111.419,25.042),(111.438,25.099),(111.396,25.129),(111.322,25.105),(111.275,25.151),(111.201,25.075),(111.104,25.038),(111.098,24.941),(110.979,24.915),(110.992,24.96),(110.97,24.973),(110.952,25.044),(110.999,25.162),(111.113,25.218),(111.104,25.285),(111.293,25.437),(111.344,25.605),(111.311,25.646),(111.309,25.719),(111.44,25.771),(111.432,25.845),(111.492,25.869),(111.345,25.907),(111.291,25.854),(111.261,25.861),(111.19,25.953),(111.215,26.021),(111.268,26.058),(111.245,26.078),(111.27,26.109),(111.27,26.215),(111.294,26.225),(111.282,26.271),(111.229,26.262),(111.203,26.279),(111.207,26.308),(111.041,26.322),(110.975,26.386),(110.946,26.375),(110.926,26.32),(110.928,26.255),(110.915,26.276),(110.762,26.25),(110.732,26.27),(110.742,26.314),(110.712,26.292),(110.613,26.334),(110.554,26.284),(110.508,26.177),(110.472,26.179),(110.378,26.095),(110.326,25.977),(110.254,25.963),(110.246,26.022),(110.197,26.068),(110.156,26.019),(110.068,26.044),(110.1,26.17),(110.04,26.163),(109.967,26.196),(110.013,26.373),(109.979,26.389),(109.987,26.433),(109.953,26.435),(109.933,26.477),(109.856,26.466),(109.893,26.527),(109.827,26.606),(109.858,26.644),(109.942,26.679),(109.932,26.719),(109.845,26.72),(109.804,26.786),(109.755,26.755),(109.662,26.774),(109.644,26.738),(109.661,26.71),(109.613,26.687),(109.514,26.718),(109.529,26.741),(109.548,26.719),(109.568,26.727),(109.599,26.759),(109.577,26.771),(109.561,26.737),(109.522,26.75),(109.497,26.82),(109.515,26.875),(109.487,26.896),(109.437,26.862),(109.437,26.894),(109.556,26.947),(109.528,26.978),(109.543,27.01),(109.521,27.072),(109.456,27.066),(109.482,27.073),(109.46,27.085),(109.474,27.136),(109.441,27.119),(109.4,27.16),(109.273,27.128),(109.249,27.154),(109.164,27.066),(109.102,27.069),(109.135,27.118),(109.087,27.118),(108.941,27.047),(108.952,27.02),(108.922,27.01),(108.922,27.03),(108.876,27.0),(108.844,27.062),(108.791,27.085),(108.886,27.109),(108.928,27.161),(108.908,27.208),(108.986,27.271),(109.042,27.277),(109.047,27.335),(109.104,27.337),(109.144,27.425),(109.109,27.419),(109.142,27.448),(109.156,27.417),(109.208,27.451),(109.246,27.419),(109.301,27.425),(109.312,27.486),(109.463,27.566),(109.471,27.681),(109.415,27.726),(109.366,27.723),(109.378,27.74),(109.333,27.781),(109.348,27.839),(109.303,27.957),(109.38,28.033),(109.339,28.063),(109.299,28.037),(109.357,28.234),(109.4,28.272),(109.355,28.266),(109.364,28.285),(109.339,28.294),(109.305,28.275),(109.273,28.31),(109.289,28.374),(109.266,28.393),(109.261,28.465),(109.274,28.539),(109.321,28.586),(109.301,28.627),(109.203,28.599),(109.181,28.621),(109.271,28.672),(109.254,28.692),(109.301,28.74),(109.24,28.782),(109.236,28.882),(109.32,29.043),(109.227,29.113),(109.277,29.125),(109.259,29.223),(109.353,29.284),(109.345,29.37),(109.392,29.373),(109.371,29.42),(109.419,29.449),(109.405,29.47),(109.416,29.497),(109.44,29.492),(109.432,29.529),(109.465,29.514),(109.462,29.555),(109.49,29.554),(109.497,29.604),(109.525,29.609),(109.515,29.625),(109.56,29.607),(109.611,29.635),(109.652,29.627),(109.662,29.601),(109.709,29.609),(109.715,29.674),(109.763,29.691),(109.773,29.725),(109.753,29.74),(109.792,29.764),(110.114,29.79),(110.246,29.732),(110.298,29.665),(110.344,29.668),(110.374,29.635),(110.448,29.666),(110.466,29.714),(110.525,29.694),(110.643,29.772),(110.615,29.831),(110.552,29.848),(110.538,29.897),(110.499,29.91),(110.516,29.96),(110.558,29.988),(110.496,30.016),(110.497,30.055),(110.602,30.055),(110.653,30.078),(110.713,30.033),(110.757,30.055),(110.747,30.113),(110.82,30.127),(110.924,30.113),(110.931,30.063),(111.243,30.041),(111.344,29.945),(111.384,29.95),(111.397,29.913),(111.522,29.929),(111.554,29.895),(111.683,29.885),(111.758,29.921),(111.862,29.857),(111.964,29.837),(111.956,29.796),(112.063,29.743),(112.064,29.771),(112.066,29.682),(112.173,29.661),(112.199,29.618),(112.245,29.66),(112.235,29.616),(112.304,29.586),(112.283,29.495),(112.32,29.541),(112.396,29.563),(112.44,29.634),(112.5,29.63),(112.536,29.601),(112.572,29.624),(112.627,29.617),(112.684,29.59),(112.718,29.652),(112.789,29.681),(112.794,29.736),(112.881,29.787),(112.928,29.764),(112.938,29.682),(113.029,29.771),(113.006,29.694),(112.912,29.607),(112.95,29.474),(113.059,29.522),(113.091,29.432),(113.101,29.46),(113.156,29.458),(113.378,29.704),(113.572,29.85),(113.539,29.685),(113.609,29.667),(113.664,29.685),(113.672,29.639),(113.741,29.592),(113.632,29.523),(113.689,29.51),(113.756,29.448),(113.729,29.394),(113.668,29.382),(113.661,29.334),(113.607,29.267),(113.653,29.226),(113.693,29.227),(113.662,29.168),(113.687,29.083),(113.732,29.105),(113.745,29.059),(113.816,29.105),(113.832,29.07),(113.883,29.066),(113.882,29.034),(113.943,29.048),(113.967,28.944),(114.01,28.956),(114.029,28.892),(114.063,28.9),(114.062,28.847),(114.153,28.835),(114.149,28.725),(114.123,28.69),(114.133,28.607),(114.087,28.557),(114.219,28.485),(114.173,28.434),(114.26,28.377),(114.255,28.321),(114.198,28.291),(114.183,28.251),(114.146,28.248),(114.107,28.184),(113.994,28.164),(114.048,28.058),(114.027,28.032),(113.964,28.04),(113.966,28.018),(113.9,27.988),(113.864,28.005),(113.851,27.974),(113.819,27.982),(113.754,27.936),(113.729,27.877),(113.76,27.855),(113.768,27.809),(113.612,27.634),(113.579,27.546),(113.588,27.522),(113.629,27.515),(113.592,27.468),(113.603,27.421),(113.633,27.406),(113.604,27.387),(113.617,27.346),(113.7,27.333),(113.877,27.383),(113.855,27.306),(113.872,27.285),(113.772,27.103),(113.809,27.099),(113.821,27.038),(113.928,26.949),(113.894,26.861),(113.835,26.805),(113.861,26.665),(113.907,26.616),(113.997,26.616),(114.021,26.588),(114.109,26.572),(114.072,26.488),(114.11,26.483),(114.089,26.412),(114.031,26.377),(114.048,26.338),(114.03,26.267),(113.979,26.238),(113.945,26.164),(113.963,26.151),(114.037,26.189),(114.089,26.169),(114.182,26.215),(114.232,26.182),(114.219,26.167),(114.237,26.152),(114.109,26.101),(114.107,26.07),(114.044,26.077),(114.009,26.017),(114.028,25.894),(113.915,25.729),(113.955,25.615)),
    ((113.56,22.213),(113.532,22.176),(113.566,22.073),(113.479,22.054),(113.442,22.01),(113.334,21.967),(113.32,21.91),(113.283,21.878),(113.235,21.889),(113.16,21.97),(113.095,22.059),(113.088,22.127),(113.031,22.067),(113.055,22.004),(113.038,21.936),(112.945,21.843),(112.894,21.845),(112.842,21.921),(112.801,21.926),(112.647,21.759),(112.536,21.755),(112.439,21.804),(112.41,21.729),(112.264,21.694),(112.197,21.737),(112.189,21.793),(112.137,21.794),(111.956,21.711),(111.955,21.668),(112.026,21.632),(111.914,21.597),(111.866,21.558),(111.813,21.559),(111.833,21.58),(111.811,21.605),(111.746,21.613),(111.688,21.585),(111.678,21.53),(111.651,21.513),(111.61,21.53),(111.552,21.503),(111.536,21.52),(111.396,21.502),(111.281,21.417),(111.259,21.413),(111.251,21.451),(111.28,21.445),(111.288,21.485),(111.062,21.45),(110.908,21.37),(110.769,21.365),(110.633,21.218),(110.497,21.218),(110.425,21.194),(110.401,21.132),(110.298,21.095),(110.242,21.017),(110.212,21.055),(110.178,20.907),(110.21,20.86),(110.328,20.849),(110.395,20.817),(110.393,20.684),(110.472,20.673),(110.551,20.473),(110.546,20.428),(110.437,20.298),(110.385,20.293),(110.34,20.255),(110.221,20.252),(110.146,20.218),(110.083,20.259),(110.026,20.258),(109.938,20.213),(109.911,20.225),(109.916,20.317),(109.86,20.386),(109.897,20.462),(109.825,20.503),(109.794,20.616),(109.745,20.622),(109.73,20.72),(109.655,20.904),(109.668,21.122),(109.765,21.227),(109.758,21.348),(109.869,21.366),(109.906,21.436),(109.786,21.457),(109.744,21.602),(109.767,21.668),(109.802,21.628),(109.903,21.653),(109.906,21.694),(109.942,21.736),(109.945,21.848),(109.987,21.88),(110.07,21.858),(110.128,21.903),(110.143,21.882),(110.197,21.9),(110.255,21.881),(110.291,21.919),(110.336,21.889),(110.39,21.892),(110.373,21.935),(110.392,21.95),(110.354,21.977),(110.365,22.126),(110.327,22.153),(110.349,22.196),(110.382,22.165),(110.432,22.207),(110.504,22.144),(110.56,22.196),(110.63,22.149),(110.68,22.173),(110.655,22.24),(110.724,22.296),(110.759,22.275),(110.79,22.287),(110.743,22.361),(110.712,22.37),(110.714,22.439),(110.684,22.474),(110.748,22.474),(110.761,22.583),(110.801,22.558),(110.834,22.585),(110.888,22.584),(110.896,22.614),(110.95,22.611),(110.96,22.637),(111.056,22.649),(111.09,22.691),(111.059,22.73),(111.219,22.749),(111.359,22.89),(111.363,22.969),(111.435,23.039),(111.434,23.073),(111.376,23.087),(111.366,23.146),(111.399,23.16),(111.372,23.265),(111.351,23.273),(111.379,23.31),(111.362,23.331),(111.4,23.47),(111.43,23.467),(111.48,23.533),(111.487,23.627),(111.616,23.64),(111.626,23.677),(111.666,23.7),(111.667,23.72),(111.618,23.733),(111.655,23.834),(111.812,23.808),(111.823,23.912),(111.849,23.906),(111.854,23.948),(111.912,23.944),(111.941,23.982),(111.881,24.104),(111.878,24.228),(111.986,24.258),(112.062,24.368),(111.986,24.467),(112.011,24.502),(112.003,24.545),(111.928,24.63),(111.954,24.647),(111.939,24.687),(111.962,24.722),(112.015,24.732),(112.06,24.8),(112.167,24.854),(112.176,24.928),(112.129,24.951),(112.122,24.985),(112.156,25.027),(112.188,25.185),(112.24,25.188),(112.257,25.16),(112.303,25.157),(112.365,25.192),(112.408,25.141),(112.445,25.187),(112.459,25.152),(112.508,25.138),(112.66,25.133),(112.714,25.083),(112.716,25.026),(112.745,24.958),(112.783,24.943),(112.781,24.897),(112.87,24.896),(113.013,24.946),(112.979,25.027),(113.019,25.084),(112.969,25.143),(112.975,25.169),(113.035,25.2),(112.993,25.248),(112.869,25.249),(112.853,25.337),(112.895,25.34),(112.925,25.297),(112.969,25.351),(113.024,25.347),(113.093,25.418),(113.132,25.415),(113.119,25.448),(113.152,25.493),(113.181,25.472),(113.229,25.512),(113.284,25.495),(113.304,25.517),(113.314,25.443),(113.36,25.438),(113.375,25.401),(113.419,25.399),(113.446,25.36),(113.544,25.368),(113.582,25.343),(113.583,25.307),(113.748,25.366),(113.759,25.33),(113.817,25.33),(113.879,25.382),(113.888,25.438),(114.001,25.444),(113.986,25.406),(114.043,25.391),(114.024,25.374),(114.051,25.364),(114.03,25.329),(114.057,25.312),(114.015,25.281),(114.038,25.251),(114.14,25.312),(114.302,25.29),(114.307,25.335),(114.382,25.316),(114.429,25.342),(114.448,25.387),(114.482,25.371),(114.541,25.418),(114.601,25.387),(114.628,25.331),(114.715,25.315),(114.742,25.277),(114.749,25.232),(114.681,25.195),(114.736,25.155),(114.736,25.122),(114.641,25.074),(114.562,25.078),(114.511,25.003),(114.424,24.973),(114.396,24.951),(114.403,24.878),(114.344,24.812),(114.336,24.75),(114.273,24.701),(114.169,24.687),(114.19,24.658),(114.175,24.646),(114.259,24.642),(114.305,24.575),(114.356,24.591),(114.393,24.564),(114.402,24.499),(114.43,24.486),(114.532,24.56),(114.593,24.538),(114.666,24.584),(114.706,24.526),(114.738,24.565),(114.731,24.611),(114.752,24.616),(114.827,24.589),(114.85,24.603),(114.869,24.562),(114.914,24.665),(114.94,24.65),(115.025,24.67),(115.055,24.71),(115.121,24.665),(115.279,24.755),(115.361,24.735),(115.407,24.794),(115.527,24.716),(115.574,24.617),(115.672,24.605),(115.688,24.546),(115.845,24.563),(115.762,24.669),(115.799,24.676),(115.809,24.701),(115.771,24.709),(115.757,24.748),(115.793,24.837),(115.783,24.863),(115.808,24.861),(115.82,24.907),(115.866,24.891),(115.862,24.864),(115.907,24.881),(115.884,24.938),(116.02,24.905),(116.091,24.839),(116.192,24.878),(116.223,24.83),(116.251,24.828),(116.246,24.794),(116.335,24.822),(116.362,24.87),(116.396,24.878),(116.418,24.841),(116.376,24.805),(116.442,24.718),(116.489,24.718),(116.526,24.605),(116.598,24.655),(116.632,24.641),(116.803,24.677),(116.813,24.646),(116.757,24.551),(116.86,24.464),(116.84,24.443),(116.904,24.371),(116.915,24.288),(116.938,24.282),(116.935,24.221),(116.999,24.181),(116.927,24.102),(116.953,24.056),(116.94,24.032),(116.982,24.0),(116.98,23.941),(116.955,23.921),(116.98,23.884),(116.96,23.867),(117.022,23.839),(117.056,23.694),(117.125,23.646),(117.191,23.634),(117.193,23.562),(117.045,23.54),(117.015,23.504),(116.922,23.533),(116.899,23.52),(116.872,23.413),(116.783,23.314),(116.796,23.249),(116.823,23.238),(116.815,23.208),(116.726,23.215),(116.666,23.158),(116.567,23.135),(116.552,23.11),(116.577,23.015),(116.536,22.99),(116.509,22.933),(116.383,22.92),(116.308,22.953),(116.105,22.817),(116.046,22.843),(115.96,22.797),(115.884,22.786),(115.819,22.731),(115.797,22.74),(115.795,22.8),(115.761,22.835),(115.645,22.864),(115.587,22.832),(115.542,22.759),(115.609,22.753),(115.565,22.691),(115.574,22.651),(115.471,22.698),(115.383,22.684),(115.343,22.726),(115.339,22.777),(115.232,22.776),(115.26,22.818),(115.199,22.822),(115.191,22.773),(115.152,22.803),(115.058,22.78),(115.022,22.732),(115.04,22.712),(114.938,22.638),(114.926,22.552),(114.888,22.539),(114.876,22.591),(114.746,22.583),(114.728,22.658),(114.753,22.76),(114.7,22.788),(114.598,22.729),(114.607,22.694),(114.513,22.659),(114.604,22.656),(114.56,22.577),(114.615,22.546),(114.629,22.509),(114.508,22.439),(114.478,22.46),(114.465,22.538),(114.39,22.604),(114.233,22.541),(114.167,22.559),(114.058,22.5),(113.967,22.51),(113.889,22.443),(113.746,22.727),(113.698,22.738),(113.679,22.727),(113.741,22.535),(113.632,22.477),(113.569,22.412),(113.604,22.404),(113.625,22.443),(113.672,22.431),(113.605,22.341),(113.597,22.233),(113.56,22.213)),
    ((105.097,24.929),(105.21,24.996),(105.262,24.963),(105.268,24.93),(105.429,24.932),(105.498,24.81),(105.6,24.808),(105.806,24.702),(105.864,24.73),(105.939,24.727),(105.962,24.678),(106.023,24.633),(106.048,24.685),(106.173,24.761),(106.198,24.813),(106.198,24.887),(106.153,24.962),(106.188,24.952),(106.216,24.982),(106.306,24.975),(106.439,25.018),(106.591,25.088),(106.654,25.169),(106.889,25.183),(106.922,25.249),(106.998,25.241),(107.015,25.346),(106.987,25.36),(106.964,25.438),(106.997,25.443),(107.016,25.496),(107.07,25.513),(107.065,25.56),(107.087,25.569),(107.161,25.57),(107.229,25.605),(107.232,25.557),(107.329,25.497),(107.31,25.41),(107.421,25.394),(107.408,25.353),(107.434,25.29),(107.482,25.301),(107.473,25.214),(107.518,25.208),(107.576,25.257),(107.599,25.25),(107.633,25.311),(107.665,25.315),(107.662,25.26),(107.695,25.198),(107.754,25.242),(107.79,25.155),(107.767,25.118),(107.843,25.116),(108.002,25.197),(108.116,25.212),(108.152,25.318),(108.144,25.392),(108.193,25.406),(108.158,25.441),(108.193,25.46),(108.25,25.428),(108.241,25.462),(108.333,25.538),(108.4,25.492),(108.429,25.438),(108.473,25.459),(108.501,25.448),(108.623,25.307),(108.623,25.391),(108.588,25.422),(108.611,25.479),(108.634,25.465),(108.608,25.492),(108.634,25.52),(108.691,25.52),(108.659,25.553),(108.689,25.624),(108.784,25.629),(108.801,25.577),(108.781,25.556),(108.813,25.526),(108.868,25.559),(108.898,25.541),(108.953,25.557),(109.025,25.513),(109.078,25.537),(109.089,25.551),(109.048,25.572),(109.031,25.633),(109.08,25.721),(109.001,25.736),(108.946,25.677),(108.9,25.683),(108.898,25.717),(108.964,25.733),(109.001,25.761),(108.998,25.785),(109.079,25.777),(109.118,25.81),(109.145,25.796),(109.148,25.742),(109.191,25.775),(109.178,25.806),(109.207,25.789),(109.208,25.74),(109.281,25.715),(109.341,25.732),(109.34,25.834),(109.368,25.841),(109.432,25.927),(109.406,25.965),(109.483,26.03),(109.524,25.996),(109.636,26.048),(109.652,26.015),(109.694,25.998),(109.711,26.014),(109.73,25.99),(109.683,25.934),(109.686,25.881),(109.768,25.892),(109.789,25.868),(109.827,25.917),(109.783,25.989),(109.792,26.018),(109.815,26.041),(109.869,26.031),(109.907,26.146),(109.967,26.196),(110.04,26.163),(110.1,26.17),(110.065,26.051),(110.099,26.02),(110.116,26.035),(110.156,26.019),(110.197,26.068),(110.246,26.022),(110.249,25.966),(110.309,25.968),(110.378,26.095),(110.472,26.179),(110.508,26.177),(110.554,26.284),(110.613,26.334),(110.712,26.292),(110.742,26.314),(110.732,26.27),(110.762,26.25),(110.915,26.276),(110.928,26.255),(110.926,26.32),(110.946,26.375),(110.975,26.386),(111.041,26.322),(111.207,26.308),(111.213,26.27),(111.28,26.272),(111.293,26.253),(111.259,26.152),(111.267,26.099),(111.245,26.078),(111.268,26.058),(111.215,26.021),(111.19,25.954),(111.222,25.937),(111.25,25.867),(111.291,25.854),(111.345,25.907),(111.492,25.869),(111.432,25.845),(111.44,25.771),(111.309,25.719),(111.311,25.646),(111.344,25.605),(111.293,25.437),(111.104,25.285),(111.113,25.218),(110.999,25.162),(110.952,25.044),(110.97,24.973),(110.992,24.96),(110.979,24.915),(111.098,24.941),(111.104,25.038),(111.201,25.075),(111.275,25.151),(111.322,25.105),(111.396,25.129),(111.438,25.099),(111.419,25.042),(111.469,25.02),(111.432,24.968),(111.471,24.93),(111.45,24.857),(111.479,24.798),(111.433,24.685),(111.561,24.64),(111.59,24.691),(111.64,24.683),(111.643,24.73),(111.706,24.783),(111.998,24.76),(112.024,24.74),(111.962,24.722),(111.939,24.687),(111.954,24.647),(111.928,24.63),(112.006,24.54),(111.986,24.467),(112.024,24.44),(112.062,24.355),(111.986,24.258),(111.878,24.228),(111.881,24.104),(111.941,23.982),(111.912,23.944),(111.854,23.948),(111.849,23.906),(111.823,23.912),(111.813,23.81),(111.655,23.834),(111.618,23.733),(111.667,23.72),(111.666,23.7),(111.626,23.677),(111.616,23.64),(111.487,23.627),(111.48,23.533),(111.43,23.467),(111.4,23.47),(111.362,23.331),(111.379,23.31),(111.351,23.273),(111.372,23.265),(111.399,23.16),(111.366,23.146),(111.376,23.087),(111.434,23.073),(111.435,23.039),(111.363,22.969),(111.359,22.89),(111.219,22.749),(111.059,22.73),(111.09,22.691),(111.056,22.649),(110.96,22.637),(110.95,22.611),(110.896,22.614),(110.888,22.584),(110.834,22.585),(110.801,22.558),(110.761,22.583),(110.748,22.474),(110.684,22.474),(110.714,22.439),(110.712,22.37),(110.743,22.361),(110.79,22.287),(110.759,22.275),(110.724,22.296),(110.655,22.24),(110.68,22.173),(110.63,22.149),(110.56,22.196),(110.504,22.144),(110.432,22.207),(110.382,22.165),(110.349,22.196),(110.327,22.153),(110.365,22.126),(110.354,21.977),(110.391,21.954),(110.373,21.934),(110.393,21.895),(110.336,21.889),(110.291,21.919),(110.255,21.881),(110.197,21.9),(110.143,21.882),(110.128,21.903),(110.07,21.858),(109.987,21.88),(109.945,21.848),(109.942,21.736),(109.906,21.694),(109.903,21.653),(109.802,21.628),(109.767,21.668),(109.744,21.602),(109.786,21.457),(109.683,21.473),(109.603,21.56),(109.583,21.554),(109.567,21.478),(109.542,21.467),(109.246,21.426),(109.148,21.387),(109.039,21.444),(109.059,21.481),(109.147,21.519),(109.14,21.567),(108.938,21.59),(108.882,21.628),(108.836,21.61),(108.796,21.626),(108.748,21.598),(108.711,21.647),(108.709,21.607),(108.677,21.589),(108.705,21.62),(108.692,21.656),(108.665,21.64),(108.621,21.682),(108.581,21.668),(108.479,21.548),(108.33,21.541),(108.224,21.49),(108.209,21.502),(108.252,21.571),(108.236,21.604),(108.151,21.559),(108.194,21.521),(108.118,21.506),(108.035,21.546),(107.959,21.535),(107.861,21.652),(107.788,21.653),(107.614,21.598),(107.58,21.615),(107.547,21.587),(107.487,21.597),(107.502,21.614),(107.47,21.66),(107.378,21.595),(107.307,21.738),(107.248,21.703),(107.217,21.711),(107.094,21.804),(107.019,21.819),(107.013,21.851),(107.062,21.894),(107.028,21.941),(106.959,21.923),(106.927,21.968),(106.814,21.977),(106.752,22.014),(106.695,21.965),(106.682,21.995),(106.719,22.075),(106.692,22.136),(106.71,22.157),(106.674,22.183),(106.706,22.217),(106.694,22.275),(106.671,22.284),(106.664,22.332),(106.562,22.346),(106.591,22.391),(106.561,22.456),(106.589,22.474),(106.615,22.603),(106.657,22.573),(106.727,22.585),(106.729,22.643),(106.784,22.706),(106.769,22.739),(106.84,22.803),(106.77,22.809),(106.718,22.882),(106.676,22.892),(106.653,22.865),(106.606,22.926),(106.526,22.947),(106.49,22.902),(106.375,22.881),(106.354,22.856),(106.271,22.875),(106.271,22.908),(106.212,22.976),(106.154,22.989),(106.021,22.991),(106.003,22.941),(105.926,22.944),(105.885,22.916),(105.84,22.988),(105.742,23.031),(105.725,23.062),(105.575,23.066),(105.56,23.086),(105.565,23.162),(105.528,23.245),(105.65,23.347),(105.701,23.329),(105.671,23.354),(105.693,23.369),(105.632,23.402),(105.7,23.402),(105.759,23.46),(105.811,23.471),(105.816,23.508),(105.853,23.527),(105.986,23.49),(106.0,23.448),(106.142,23.57),(106.12,23.601),(106.158,23.71),(106.138,23.796),(106.192,23.825),(106.173,23.862),(106.198,23.872),(106.082,23.993),(106.097,24.021),(106.057,24.047),(106.051,24.09),(106.012,24.101),(106.008,24.125),(105.925,24.124),(105.893,24.041),(105.853,24.058),(105.842,24.031),(105.797,24.024),(105.802,24.063),(105.766,24.074),(105.701,24.066),(105.653,24.032),(105.629,24.127),(105.598,24.139),(105.529,24.129),(105.493,24.018),(105.413,24.039),(105.32,24.117),(105.273,24.102),(105.294,24.075),(105.259,24.062),(105.183,24.134),(105.184,24.168),(105.229,24.164),(105.243,24.207),(105.16,24.28),(105.196,24.338),(105.146,24.376),(105.112,24.373),(105.108,24.414),(105.043,24.442),(104.936,24.409),(104.756,24.458),(104.753,24.437),(104.718,24.443),(104.704,24.422),(104.722,24.341),(104.695,24.323),(104.612,24.376),(104.63,24.398),(104.617,24.422),(104.578,24.421),(104.55,24.521),(104.521,24.536),(104.506,24.629),(104.451,24.639),(104.491,24.655),(104.53,24.731),(104.597,24.71),(104.629,24.662),(104.738,24.62),(104.765,24.657),(104.843,24.677),(104.876,24.74),(105.034,24.788),(105.039,24.873),(105.097,24.929)),
    ((110.107,20.027),(110.151,20.077),(110.29,20.057),(110.312,20.104),(110.353,20.116),(110.527,20.076),(110.698,20.163),(110.74,20.077),(110.822,20.026),(110.962,20.024),(111.045,19.764),(111.069,19.619),(110.921,19.553),(110.824,19.43),(110.735,19.386),(110.677,19.287),(110.62,19.152),(110.588,18.806),(110.566,18.774),(110.5,18.752),(110.5,18.652),(110.262,18.619),(110.117,18.507),(110.081,18.383),(109.795,18.345),(109.741,18.186),(109.585,18.145),(109.356,18.216),(109.288,18.266),(109.148,18.263),(109.118,18.322),(108.953,18.309),(108.884,18.416),(108.659,18.463),(108.64,18.532),(108.658,18.728),(108.588,18.839),(108.639,18.922),(108.593,19.106),(108.606,19.266),(108.665,19.375),(108.766,19.401),(109.049,19.62),(109.094,19.69),(109.155,19.711),(109.162,19.797),(109.256,19.868),(109.265,19.905),(109.309,19.918),(109.499,19.874),(109.527,19.944),(109.664,20.014),(109.719,20.018),(109.77,19.979),(109.942,19.996),(109.998,19.98),(110.107,20.027)),
    ((109.468,26.832),(109.487,26.761),(109.438,26.756),(109.413,26.721),(109.382,26.728),(109.362,26.695),(109.284,26.699),(109.318,26.654),(109.357,26.658),(109.409,26.537),(109.363,26.473),(109.383,26.457),(109.323,26.424),(109.275,26.315),(109.34,26.265),(109.467,26.314),(109.44,26.239),(109.477,26.148),(109.515,26.129),(109.505,26.098),(109.45,26.103),(109.454,26.056),(109.483,26.03),(109.406,25.965),(109.432,25.927),(109.368,25.841),(109.34,25.834),(109.342,25.733),(109.281,25.715),(109.208,25.74),(109.207,25.789),(109.178,25.806),(109.191,25.775),(109.148,25.742),(109.145,25.796),(109.118,25.81),(109.079,25.777),(108.998,25.785),(109.001,25.761),(108.964,25.733),(108.898,25.717),(108.9,25.683),(108.946,25.677),(109.001,25.736),(109.08,25.721),(109.031,25.63),(109.048,25.572),(109.089,25.55),(109.025,25.513),(108.949,25.558),(108.827,25.551),(108.813,25.526),(108.781,25.556),(108.801,25.577),(108.784,25.629),(108.689,25.624),(108.659,25.553),(108.691,25.52),(108.634,25.52),(108.608,25.492),(108.634,25.465),(108.611,25.479),(108.588,25.422),(108.623,25.391),(108.625,25.308),(108.501,25.448),(108.473,25.459),(108.429,25.438),(108.4,25.492),(108.333,25.538),(108.241,25.462),(108.25,25.428),(108.193,25.46),(108.158,25.441),(108.193,25.406),(108.144,25.392),(108.152,25.318),(108.115,25.211),(108.002,25.197),(107.843,25.116),(107.767,25.118),(107.79,25.155),(107.758,25.196),(107.763,25.229),(107.742,25.24),(107.698,25.196),(107.662,25.26),(107.665,25.315),(107.633,25.311),(107.599,25.25),(107.576,25.257),(107.518,25.208),(107.473,25.214),(107.489,25.285),(107.477,25.302),(107.434,25.29),(107.408,25.353),(107.425,25.391),(107.381,25.414),(107.359,25.394),(107.315,25.403),(107.328,25.499),(107.232,25.557),(107.236,25.594),(107.212,25.609),(107.178,25.575),(107.065,25.56),(107.07,25.513),(107.016,25.496),(106.997,25.443),(106.964,25.438),(106.987,25.36),(107.015,25.346),(106.998,25.241),(106.922,25.249),(106.889,25.183),(106.654,25.169),(106.591,25.088),(106.439,25.018),(106.306,24.975),(106.216,24.982),(106.188,24.952),(106.153,24.962),(106.198,24.887),(106.198,24.813),(106.173,24.761),(106.048,24.685),(106.023,24.633),(105.962,24.678),(105.939,24.727),(105.864,24.73),(105.806,24.702),(105.6,24.808),(105.501,24.809),(105.443,24.922),(105.37,24.944),(105.268,24.93),(105.262,24.963),(105.21,24.996),(105.075,24.917),(105.038,24.87),(105.034,24.788),(104.894,24.75),(104.843,24.677),(104.765,24.657),(104.729,24.619),(104.53,24.731),(104.54,24.813),(104.714,24.998),(104.685,25.056),(104.617,25.06),(104.687,25.077),(104.748,25.215),(104.815,25.156),(104.808,25.227),(104.828,25.238),(104.816,25.263),(104.736,25.269),(104.708,25.297),(104.641,25.296),(104.646,25.357),(104.54,25.405),(104.557,25.525),(104.451,25.497),(104.436,25.473),(104.429,25.576),(104.332,25.601),(104.309,25.661),(104.328,25.76),(104.374,25.732),(104.432,25.822),(104.442,25.871),(104.415,25.912),(104.471,26.01),(104.456,26.077),(104.5,26.071),(104.527,26.101),(104.543,26.254),(104.593,26.317),(104.659,26.335),(104.684,26.368),(104.666,26.435),(104.634,26.451),(104.615,26.51),(104.57,26.525),(104.565,26.586),(104.465,26.596),(104.468,26.652),(104.422,26.713),(104.35,26.62),(104.129,26.645),(104.069,26.575),(104.074,26.523),(104.053,26.508),(103.821,26.529),(103.765,26.586),(103.749,26.625),(103.772,26.727),(103.727,26.742),(103.703,26.813),(103.723,26.852),(103.78,26.875),(103.764,26.907),(103.778,26.946),(103.706,27.05),(103.676,27.052),(103.615,27.006),(103.63,27.018),(103.602,27.062),(103.617,27.08),(103.66,27.066),(103.654,27.093),(103.62,27.098),(103.628,27.117),(103.71,27.137),(103.708,27.161),(103.803,27.268),(103.866,27.282),(103.934,27.444),(103.98,27.417),(104.016,27.43),(104.021,27.376),(104.131,27.327),(104.175,27.264),(104.191,27.288),(104.22,27.28),(104.215,27.302),(104.255,27.295),(104.248,27.336),(104.362,27.469),(104.518,27.4),(104.542,27.326),(104.611,27.308),(104.755,27.346),(104.78,27.318),(104.81,27.355),(104.857,27.333),(104.852,27.301),(104.872,27.291),(105.071,27.42),(105.166,27.41),(105.191,27.372),(105.261,27.516),(105.258,27.541),(105.233,27.544),(105.243,27.578),(105.305,27.612),(105.302,27.696),(105.354,27.749),(105.442,27.776),(105.511,27.769),(105.559,27.722),(105.605,27.716),(105.642,27.659),(105.767,27.719),(105.847,27.706),(105.881,27.739),(105.922,27.747),(105.933,27.728),(106.064,27.777),(106.194,27.755),(106.334,27.815),(106.345,27.828),(106.317,27.839),(106.339,27.876),(106.301,27.909),(106.329,27.958),(106.295,28.004),(106.246,28.014),(106.268,28.067),(106.25,28.093),(106.126,28.168),(106.038,28.139),(106.034,28.107),(105.98,28.106),(105.945,28.144),(105.879,28.124),(105.86,28.168),(105.891,28.237),(105.851,28.255),(105.788,28.336),(105.771,28.326),(105.788,28.31),(105.738,28.304),(105.732,28.272),(105.641,28.312),(105.661,28.365),(105.646,28.43),(105.61,28.444),(105.624,28.519),(105.686,28.535),(105.693,28.589),(105.743,28.616),(105.758,28.591),(105.785,28.611),(105.905,28.606),(105.89,28.671),(105.937,28.683),(105.969,28.762),(106.029,28.72),(106.033,28.693),(106.084,28.685),(106.104,28.637),(106.166,28.637),(106.205,28.568),(106.293,28.538),(106.331,28.482),(106.377,28.479),(106.384,28.562),(106.484,28.531),(106.506,28.541),(106.475,28.6),(106.51,28.565),(106.525,28.576),(106.494,28.606),(106.503,28.661),(106.528,28.678),(106.455,28.776),(106.471,28.835),(106.562,28.759),(106.587,28.688),(106.618,28.691),(106.62,28.665),(106.651,28.651),(106.618,28.645),(106.637,28.612),(106.612,28.608),(106.616,28.55),(106.566,28.521),(106.565,28.485),(106.594,28.511),(106.633,28.504),(106.636,28.485),(106.665,28.494),(106.697,28.479),(106.693,28.457),(106.728,28.455),(106.747,28.468),(106.729,28.543),(106.783,28.568),(106.76,28.611),(106.786,28.627),(106.813,28.59),(106.83,28.623),(106.867,28.625),(106.89,28.695),(106.86,28.692),(106.83,28.736),(106.843,28.779),(106.925,28.811),(106.952,28.768),(106.986,28.774),(106.983,28.852),(107.02,28.86),(107.017,28.883),(107.06,28.869),(107.067,28.896),(107.067,28.866),(107.098,28.891),(107.194,28.89),(107.195,28.839),(107.228,28.836),(107.22,28.773),(107.249,28.763),(107.262,28.793),(107.346,28.826),(107.335,28.845),(107.386,28.85),(107.441,28.944),(107.364,29.01),(107.396,29.042),(107.37,29.094),(107.412,29.095),(107.428,29.128),(107.402,29.186),(107.447,29.204),(107.474,29.172),(107.562,29.222),(107.599,29.148),(107.629,29.166),(107.701,29.142),(107.752,29.2),(107.812,29.14),(107.785,29.049),(107.824,29.035),(107.811,28.984),(107.867,28.959),(107.884,29.008),(107.932,29.036),(108.024,29.039),(108.069,29.087),(108.135,29.053),(108.197,29.072),(108.232,29.028),(108.271,29.092),(108.307,29.08),(108.32,28.962),(108.358,28.894),(108.354,28.816),(108.39,28.793),(108.334,28.677),(108.475,28.628),(108.567,28.663),(108.635,28.638),(108.604,28.59),(108.613,28.544),(108.574,28.529),(108.611,28.422),(108.578,28.391),(108.58,28.345),(108.612,28.325),(108.669,28.336),(108.665,28.384),(108.697,28.404),(108.642,28.458),(108.724,28.492),(108.783,28.428),(108.761,28.39),(108.784,28.379),(108.771,28.315),(108.726,28.275),(108.763,28.191),(108.822,28.246),(108.847,28.201),(108.923,28.218),(108.93,28.191),(108.988,28.161),(109.026,28.221),(109.087,28.186),(109.103,28.208),(109.082,28.249),(109.142,28.321),(109.153,28.417),(109.202,28.478),(109.274,28.495),(109.258,28.438),(109.289,28.374),(109.273,28.31),(109.305,28.275),(109.339,28.294),(109.364,28.285),(109.355,28.266),(109.4,28.274),(109.357,28.234),(109.299,28.037),(109.339,28.063),(109.38,28.033),(109.303,27.959),(109.348,27.839),(109.333,27.781),(109.378,27.74),(109.366,27.723),(109.415,27.726),(109.471,27.681),(109.463,27.566),(109.312,27.486),(109.301,27.425),(109.246,27.419),(109.208,27.451),(109.156,27.417),(109.142,27.448),(109.109,27.419),(109.144,27.425),(109.104,27.337),(109.047,27.335),(109.042,27.277),(108.986,27.271),(108.908,27.208),(108.928,27.161),(108.886,27.109),(108.791,27.085),(108.844,27.062),(108.876,27.0),(108.922,27.03),(108.922,27.01),(108.952,27.02),(108.941,27.047),(109.087,27.118),(109.135,27.118),(109.102,27.069),(109.164,27.066),(109.249,27.154),(109.273,27.128),(109.4,27.16),(109.441,27.119),(109.474,27.136),(109.46,27.085),(109.482,27.073),(109.456,27.066),(109.521,27.072),(109.543,27.01),(109.528,26.978),(109.556,26.947),(109.437,26.894),(109.468,26.832)),
    ((117.21,40.083),(117.086,40.075),(117.022,40.03),(116.931,40.056),(116.778,40.033),(116.758,39.964),(116.783,39.948),(116.787,39.888),(116.9,39.832),(116.918,39.848),(116.954,39.788),(116.9,39.759),(116.917,39.731),(116.883,39.719),(116.907,39.678),(116.851,39.668),(116.836,39.617),(116.785,39.594),(116.726,39.625),(116.701,39.621),(116.727,39.599),(116.711,39.589),(116.566,39.62),(116.525,39.597),(116.509,39.552),(116.472,39.555),(116.478,39.535),(116.438,39.526),(116.444,39.511),(116.402,39.528),(116.413,39.482),(116.444,39.482),(116.457,39.459),(116.435,39.443),(116.337,39.457),(116.259,39.501),(116.241,39.564),(116.186,39.592),(116.131,39.568),(115.996,39.577),(115.979,39.596),(115.96,39.562),(115.911,39.602),(115.888,39.551),(115.819,39.531),(115.829,39.508),(115.753,39.513),(115.668,39.616),(115.58,39.59),(115.546,39.619),(115.515,39.592),(115.522,39.641),(115.479,39.652),(115.501,39.691),(115.493,39.739),(115.426,39.774),(115.569,39.813),(115.515,39.838),(115.521,39.902),(115.427,39.951),(115.455,40.03),(115.591,40.097),(115.6,40.12),(115.741,40.133),(115.777,40.178),(115.853,40.148),(115.849,40.185),(115.873,40.188),(115.898,40.237),(115.97,40.265),(115.919,40.354),(115.859,40.363),(115.771,40.443),(115.782,40.492),(115.736,40.504),(115.754,40.539),(115.908,40.618),(115.968,40.606),(115.984,40.579),(116.122,40.63),(116.111,40.647),(116.165,40.664),(116.248,40.792),(116.31,40.752),(116.33,40.774),(116.462,40.771),(116.335,40.922),(116.371,40.944),(116.399,40.906),(116.475,40.896),(116.456,40.981),(116.563,40.994),(116.603,40.977),(116.631,41.062),(116.692,41.041),(116.678,40.972),(116.723,40.928),(116.714,40.911),(116.805,40.842),(116.877,40.821),(116.97,40.707),(117.208,40.695),(117.318,40.658),(117.41,40.688),(117.515,40.661),(117.501,40.637),(117.463,40.653),(117.449,40.628),(117.422,40.636),(117.422,40.57),(117.312,40.578),(117.25,40.549),(117.264,40.514),(117.214,40.513),(117.265,40.442),(117.225,40.371),(117.275,40.333),(117.296,40.278),(117.332,40.29),(117.346,40.235),(117.391,40.229),(117.385,40.188),(117.408,40.188),(117.352,40.174),(117.355,40.14),(117.24,40.113),(117.21,40.083)),
    ((117.419,40.249),(117.565,40.229),(117.577,40.179),(117.653,40.126),(117.65,40.092),(117.708,40.095),(117.776,40.06),(117.745,40.019),(117.798,40.011),(117.782,39.967),(117.696,39.988),(117.633,39.969),(117.592,39.997),(117.536,39.996),(117.548,39.978),(117.504,39.92),(117.568,39.8),(117.539,39.761),(117.597,39.746),(117.577,39.726),(117.603,39.706),(117.646,39.701),(117.667,39.643),(117.622,39.593),(117.707,39.576),(117.686,39.566),(117.717,39.53),(117.768,39.601),(117.931,39.579),(117.9,39.475),(117.871,39.455),(117.872,39.412),(117.848,39.408),(117.866,39.379),(117.804,39.362),(117.838,39.352),(117.853,39.37),(117.848,39.329),(118.027,39.292),(118.066,39.235),(117.978,39.206),(117.965,39.173),(117.847,39.089),(117.854,38.964),(117.899,38.942),(117.848,38.856),(117.779,38.869),(117.646,38.829),(117.655,38.778),(117.742,38.755),(117.729,38.681),(117.657,38.661),(117.64,38.627),(117.527,38.602),(117.479,38.618),(117.369,38.565),(117.369,38.583),(117.351,38.562),(117.254,38.557),(117.24,38.579),(117.262,38.587),(117.237,38.585),(117.26,38.608),(117.23,38.644),(117.099,38.587),(117.053,38.642),(117.068,38.681),(117.039,38.688),(117.046,38.706),(116.878,38.682),(116.868,38.746),(116.745,38.753),(116.752,38.832),(116.724,38.853),(116.709,38.933),(116.755,39.004),(116.757,39.051),(116.872,39.055),(116.927,39.12),(116.912,39.15),(116.864,39.154),(116.856,39.216),(116.894,39.228),(116.863,39.298),(116.891,39.335),(116.871,39.358),(116.83,39.339),(116.818,39.374),(116.843,39.376),(116.839,39.411),(116.876,39.435),(116.785,39.467),(116.821,39.481),(116.827,39.514),(116.788,39.549),(116.812,39.577),(116.801,39.604),(116.836,39.617),(116.851,39.668),(116.907,39.678),(116.921,39.707),(116.951,39.707),(116.946,39.671),(116.975,39.638),(117.017,39.654),(117.128,39.617),(117.173,39.638),(117.154,39.736),(117.206,39.765),(117.157,39.818),(117.261,39.845),(117.153,39.876),(117.159,39.91),(117.137,39.921),(117.198,39.993),(117.183,40.06),(117.224,40.066),(117.212,40.097),(117.24,40.113),(117.355,40.14),(117.352,40.174),(117.407,40.187),(117.385,40.188),(117.378,40.219),(117.419,40.249)),
    ((121.976,31.617),(121.992,31.478),(121.919,31.436),(121.846,31.433),(121.609,31.508),(121.435,31.591),(121.396,31.586),(121.373,31.554),(121.119,31.76),(121.201,31.836),(121.311,31.873),(121.386,31.834),(121.432,31.77),(121.594,31.705),(121.976,31.617)),
    ((121.521,31.396),(121.723,31.304),(121.963,31.048),(121.999,30.9),(121.955,30.826),(121.97,30.79),(121.944,30.777),(121.905,30.814),(121.648,30.816),(121.518,30.776),(121.362,30.68),(121.275,30.678),(121.271,30.733),(121.219,30.785),(121.118,30.785),(121.138,30.827),(121.122,30.851),(121.067,30.85),(121.038,30.814),(121.015,30.836),(120.991,30.823),(121.022,30.876),(120.991,30.896),(120.991,31.015),(120.953,31.03),(120.911,31.011),(120.902,31.086),(120.857,31.105),(120.882,31.135),(121.069,31.149),(121.062,31.268),(121.089,31.293),(121.105,31.274),(121.162,31.283),(121.107,31.355),(121.16,31.406),(121.147,31.444),(121.235,31.493),(121.26,31.479),(121.344,31.513),(121.521,31.396)),
    ((107.059,30.044),(107.085,30.064),(107.081,30.095),(107.104,30.091),(107.24,30.237),(107.36,30.457),(107.517,30.644),(107.457,30.683),(107.425,30.741),(107.499,30.811),(107.48,30.839),(107.577,30.849),(107.636,30.818),(107.719,30.889),(107.761,30.864),(107.765,30.817),(107.851,30.793),(107.955,30.873),(107.995,30.909),(107.945,30.924),(107.943,30.989),(107.983,30.984),(108.004,31.026),(108.06,31.05),(108.025,31.063),(108.01,31.11),(108.091,31.202),(108.078,31.231),(108.032,31.218),(108.02,31.245),(108.181,31.326),(108.154,31.372),(108.209,31.399),(108.226,31.453),(108.191,31.492),(108.34,31.509),(108.34,31.539),(108.388,31.546),(108.379,31.571),(108.434,31.629),(108.466,31.619),(108.547,31.666),(108.506,31.733),(108.536,31.758),(108.344,31.861),(108.26,31.968),(108.308,31.998),(108.352,31.972),(108.37,31.987),(108.33,32.02),(108.364,32.038),(108.348,32.07),(108.451,32.075),(108.37,32.174),(108.403,32.196),(108.477,32.182),(108.51,32.202),(108.673,32.104),(108.74,32.104),(108.776,32.056),(108.838,32.039),(108.892,31.99),(109.04,31.961),(109.191,31.856),(109.194,31.819),(109.276,31.8),(109.282,31.778),(109.253,31.766),(109.282,31.718),(109.39,31.706),(109.556,31.731),(109.733,31.7),(109.738,31.629),(109.766,31.604),(109.729,31.549),(109.839,31.556),(109.896,31.52),(109.982,31.514),(109.946,31.47),(109.988,31.476),(110.055,31.411),(110.115,31.413),(110.145,31.388),(110.16,31.257),(110.2,31.159),(110.186,31.126),(110.121,31.09),(110.137,30.987),(110.174,30.98),(110.082,30.8),(110.021,30.83),(110.008,30.885),(109.895,30.9),(109.73,30.815),(109.719,30.779),(109.657,30.761),(109.652,30.724),(109.591,30.694),(109.574,30.647),(109.534,30.64),(109.529,30.664),(109.459,30.616),(109.354,30.487),(109.338,30.521),(109.361,30.556),(109.304,30.632),(109.145,30.521),(109.104,30.566),(109.119,30.64),(109.043,30.656),(108.968,30.625),(108.812,30.492),(108.79,30.514),(108.729,30.504),(108.691,30.587),(108.667,30.589),(108.641,30.575),(108.649,30.538),(108.569,30.47),(108.509,30.504),(108.415,30.477),(108.431,30.416),(108.402,30.376),(108.461,30.36),(108.583,30.254),(108.553,30.164),(108.568,30.157),(108.514,30.058),(108.532,30.056),(108.543,29.998),(108.519,29.868),(108.429,29.881),(108.387,29.861),(108.367,29.82),(108.404,29.836),(108.422,29.774),(108.445,29.776),(108.438,29.741),(108.507,29.709),(108.505,29.731),(108.55,29.746),(108.523,29.765),(108.579,29.847),(108.602,29.866),(108.662,29.854),(108.688,29.69),(108.715,29.699),(108.711,29.68),(108.761,29.646),(108.784,29.656),(108.753,29.689),(108.787,29.692),(108.782,29.637),(108.82,29.633),(108.835,29.67),(108.83,29.652),(108.887,29.634),(108.869,29.599),(108.909,29.595),(108.867,29.473),(108.944,29.411),(108.919,29.327),(109.034,29.36),(109.054,29.403),(109.081,29.392),(109.113,29.361),(109.107,29.289),(109.143,29.271),(109.111,29.217),(109.123,29.191),(109.14,29.17),(109.181,29.173),(109.23,29.128),(109.236,29.088),(109.311,29.068),(109.32,29.047),(109.234,28.864),(109.241,28.778),(109.299,28.748),(109.295,28.72),(109.254,28.692),(109.268,28.669),(109.181,28.621),(109.203,28.599),(109.301,28.627),(109.321,28.581),(109.274,28.539),(109.274,28.495),(109.202,28.478),(109.153,28.417),(109.142,28.321),(109.082,28.249),(109.103,28.208),(109.087,28.186),(109.026,28.221),(108.988,28.161),(108.93,28.191),(108.923,28.218),(108.847,28.201),(108.822,28.246),(108.762,28.192),(108.726,28.275),(108.771,28.315),(108.784,28.379),(108.761,28.39),(108.783,28.428),(108.711,28.502),(108.642,28.458),(108.697,28.405),(108.659,28.368),(108.676,28.344),(108.613,28.325),(108.581,28.344),(108.578,28.391),(108.611,28.422),(108.574,28.529),(108.613,28.544),(108.604,28.59),(108.635,28.637),(108.567,28.663),(108.475,28.628),(108.334,28.677),(108.39,28.793),(108.354,28.816),(108.351,28.934),(108.32,28.962),(108.303,29.084),(108.27,29.09),(108.232,29.028),(108.197,29.072),(108.135,29.053),(108.069,29.087),(108.024,29.039),(107.932,29.036),(107.884,29.008),(107.867,28.959),(107.811,28.984),(107.824,29.035),(107.785,29.049),(107.812,29.14),(107.751,29.2),(107.7,29.142),(107.629,29.166),(107.59,29.15),(107.566,29.222),(107.474,29.172),(107.447,29.204),(107.405,29.188),(107.428,29.128),(107.412,29.095),(107.37,29.094),(107.396,29.042),(107.364,29.01),(107.441,28.944),(107.386,28.85),(107.335,28.845),(107.346,28.826),(107.262,28.793),(107.249,28.763),(107.22,28.773),(107.228,28.836),(107.195,28.839),(107.194,28.89),(107.098,28.891),(107.067,28.866),(107.067,28.896),(107.06,28.869),(107.017,28.883),(107.02,28.86),(106.983,28.852),(106.986,28.774),(106.952,28.768),(106.925,28.811),(106.844,28.78),(106.829,28.738),(106.86,28.692),(106.89,28.695),(106.867,28.625),(106.83,28.623),(106.813,28.59),(106.786,28.627),(106.76,28.611),(106.783,28.568),(106.729,28.543),(106.747,28.468),(106.728,28.455),(106.693,28.457),(106.697,28.479),(106.665,28.494),(106.636,28.485),(106.633,28.504),(106.594,28.511),(106.565,28.485),(106.566,28.521),(106.616,28.55),(106.612,28.608),(106.637,28.612),(106.618,28.645),(106.651,28.651),(106.62,28.665),(106.618,28.691),(106.587,28.688),(106.562,28.759),(106.471,28.835),(106.455,28.776),(106.528,28.678),(106.503,28.661),(106.494,28.606),(106.525,28.576),(106.51,28.565),(106.475,28.6),(106.502,28.535),(106.399,28.571),(106.375,28.526),(106.343,28.533),(106.347,28.586),(106.305,28.65),(106.322,28.665),(106.245,28.813),(106.254,28.866),(106.174,28.921),(106.146,28.902),(106.049,28.907),(106.046,28.953),(105.987,28.979),(105.91,28.921),(105.914,28.901),(105.881,28.934),(105.798,28.937),(105.81,28.957),(105.742,29.04),(105.758,29.069),(105.729,29.106),(105.753,29.134),(105.731,29.133),(105.704,29.178),(105.692,29.279),(105.716,29.296),(105.666,29.277),(105.658,29.252),(105.636,29.28),(105.608,29.256),(105.606,29.275),(105.519,29.265),(105.51,29.286),(105.46,29.289),(105.475,29.311),(105.458,29.329),(105.421,29.312),(105.443,29.399),(105.418,29.424),(105.373,29.421),(105.399,29.439),(105.391,29.457),(105.327,29.446),(105.338,29.465),(105.295,29.534),(105.321,29.61),(105.336,29.592),(105.354,29.627),(105.38,29.624),(105.39,29.677),(105.478,29.676),(105.491,29.721),(105.539,29.695),(105.544,29.733),(105.566,29.725),(105.576,29.745),(105.584,29.819),(105.619,29.847),(105.709,29.841),(105.738,29.863),(105.739,29.892),(105.703,29.925),(105.748,30.033),(105.688,30.039),(105.675,30.071),(105.641,30.074),(105.643,30.102),(105.57,30.135),(105.597,30.159),(105.556,30.146),(105.537,30.165),(105.661,30.209),(105.619,30.235),(105.626,30.276),(105.674,30.253),(105.735,30.26),(105.715,30.323),(105.742,30.319),(105.766,30.398),(105.818,30.438),(105.847,30.393),(105.863,30.411),(105.876,30.388),(105.906,30.407),(105.902,30.387),(105.942,30.372),(106.031,30.375),(106.061,30.339),(106.09,30.346),(106.106,30.311),(106.132,30.303),(106.125,30.325),(106.172,30.307),(106.171,30.251),(106.207,30.205),(106.232,30.213),(106.242,30.178),(106.248,30.198),(106.271,30.186),(106.263,30.215),(106.297,30.205),(106.301,30.239),(106.335,30.227),(106.429,30.254),(106.408,30.276),(106.437,30.278),(106.441,30.31),(106.51,30.29),(106.558,30.316),(106.636,30.267),(106.644,30.247),(106.607,30.231),(106.649,30.165),(106.678,30.16),(106.673,30.123),(106.704,30.118),(106.699,30.076),(106.732,30.027),(106.786,30.018),(106.839,30.051),(106.913,30.025),(106.982,30.086),(107.021,30.037),(107.059,30.044)),
    ((114.233,22.541),(114.215,22.524),(114.263,22.548),(114.284,22.509),(114.343,22.506),(114.254,22.446),(114.219,22.466),(114.239,22.452),(114.206,22.438),(114.278,22.436),(114.348,22.478),(114.348,22.438),(114.369,22.459),(114.412,22.411),(114.386,22.412),(114.397,22.365),(114.363,22.333),(114.321,22.39),(114.284,22.389),(114.279,22.329),(114.318,22.295),(114.305,22.258),(114.276,22.26),(114.266,22.295),(114.251,22.283),(114.267,22.201),(114.239,22.214),(114.218,22.191),(114.2,22.233),(114.166,22.227),(114.121,22.272),(114.145,22.304),(114.079,22.33),(114.034,22.3),(114.029,22.263),(114.006,22.268),(114.027,22.229),(114.01,22.213),(113.977,22.231),(113.85,22.191),(113.844,22.229),(113.898,22.31),(113.951,22.321),(113.956,22.299),(114.026,22.346),(113.924,22.365),(113.919,22.419),(114.0,22.491),(114.025,22.481),(114.029,22.504),(114.061,22.501),(114.096,22.534),(114.167,22.559),(114.233,22.541)),
)


def _font(size):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
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


def _color(level=None, wind=None):
    lv = str(level or '').strip()
    if lv in _NMC_COLOR:
        return _NMC_COLOR[lv]
    return _NMC_COLOR.get({16: 'SuperTY', 14: 'STY', 12: 'TY', 10: 'STS', 8: 'TS'}.get(_power_of(level, wind), 'TD'), (255, 230, 0))


def _display_name(view):
    cn, en, num = view.get('cn') or '', view.get('en') or '', view.get('num') or ''
    return (cn or en or (f'编号{num}' if num else '未命名')), (en if cn else '')


# ---------- 官网路径产品图 ----------

def _parse_pub_page(html):
    html = html or ''
    m = re.search(r'<title>([^<]+)</title>', html, re.I)
    title = m.group(1) if m else ''
    mt = re.search(r'路径预报[_．.\s]*([\u4e00-\u9fffA-Za-z0-9·]{2,20})', title)
    imgs = re.findall(r'data-img="(https?://image\.nmc\.cn/product/[^"]+)"', html)
    if not imgs:
        imgs = re.findall(
            r'(https?://image\.nmc\.cn/product/[^"\'?\s]+TCBU[^"\'?\s]+\.(?:JPG|jpg|PNG|png))', html,
        )
    latest = imgs[0].split('?')[0].replace('/medium/', '/') if imgs else ''
    return {
        'cn': mt.group(1).strip() if mt else '', 'title': title, 'img': latest,
        'codes': sorted(set(re.findall(r'0W\d{6,10}', html))), 'count': len(imgs),
    }


def _official_match(info, cn, num):
    icn = info.get('cn') or ''
    if cn and icn and (cn in icn or icn in cn):
        return True
    if not num:
        return False
    n4 = num[-4:] if len(num) >= 4 else num
    return any(
        num in c or (len(n4) >= 2 and n4 in c)
        for c in (info.get('codes') or []) if 'null' not in c.lower()
    )


async def nmc_official_maps():
    cached = _cache_get('official_maps')
    if cached is not None:
        return cached

    async def one(page):
        html = await http_text(f'{NMC_PUB}/{page}', timeout=8, headers=_PUB_HEADERS)
        info = _parse_pub_page(html) if html else None
        if not info or not info.get('img'):
            return None
        cn = info.get('cn') or ''
        if cn in ('ll号台风', '号台风') or 'null' in (info.get('img') or '').lower():
            if not any('null' not in c.lower() for c in (info.get('codes') or [])):
                return None
        info['page'] = page
        return info

    out, seen = [], set()
    for info in await asyncio.gather(*[one(p) for p in _PUB_PAGES], return_exceptions=True):
        img = None if isinstance(info, Exception) or not info else info.get('img')
        if not img or img in seen:
            continue
        seen.add(img)
        out.append(info)
    return _cache_set('official_maps', out, _OFFICIAL_TTL)


async def fetch_official_track_png(view):
    if view.get('status') == 'stop':
        return None, 'no_official'
    cn, num = view.get('cn') or '', str(view.get('num') or '')
    ck = f'offimg:{view.get("id") or cn or num}'
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    hit = next((i for i in await nmc_official_maps() if _official_match(i, cn, num)), None)
    if not hit:
        return None, 'no_official'
    data = await http_bytes(hit['img'], timeout=15, headers=_PUB_HEADERS)
    if not data or len(data) < 1000:
        mid = hit['img'].replace('/TCBU/', '/TCBU/medium/')
        if '/medium/medium/' not in mid:
            data = await http_bytes(mid, timeout=12, headers=_PUB_HEADERS)
    if not data or len(data) < 1000:
        return None, 'download_fail'
    return _cache_set(ck, (data, hit.get('img')), _IMG_TTL)


def _tshort(t):
    s = str(t or '').strip()
    m = re.search(r'(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\s+(\d{1,2})', s)
    if m:
        return f'{int(m.group(3))}日{int(m.group(4)):02d}时'
    if len(s) >= 12 and s.isdigit():
        return f'{int(s[6:8])}日{s[8:10]}时'
    return s[:10] or '-'


def _tw(draw, text, font):
    if hasattr(draw, 'textbbox'):
        b = draw.textbbox((0, 0), text, font=font)
        return b[2] - b[0], b[3] - b[1]
    return len(text) * 7, 12


def _put(draw, x, y, text, font, fill, anchor='lt', stroke=None):
    tw, th = _tw(draw, text, font)
    h, v = (anchor + 'lt')[:2]
    ax = x - (tw / 2 if h == 'm' else tw if h == 'r' else 0)
    ay = y - (th / 2 if v == 'm' else th if v == 'b' else 0)
    if stroke:
        for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            draw.text((ax + ox, ay + oy), text, font=font, fill=stroke)
    draw.text((ax, ay), text, font=font, fill=fill)
    return tw, th


def _wind_txt(wind):
    try:
        ms = float(wind)
        s = str(int(ms)) if ms == int(ms) else f'{ms:g}'
        return f'{s}m/s({ms * 3.6:.0f}km/h)'
    except (TypeError, ValueError):
        return f'{wind or "-"}m/s'


def _ll(p):
    lat, lng = _to_float(p.get('lat')), _to_float(p.get('lng'))
    return (lng, lat, p) if lat is not None and lng is not None else None


def _collect_track(view):
    hist = [x for x in (_ll(p) for p in view.get('points') or []) if x]
    fc = []
    if hist and view.get('status') != 'stop':
        fc = [x for x in (_ll(fp) for fp in hist[-1][2].get('forecasts') or []) if x]
    return hist, fc


def _pick_marks(hist, forecast, view=None):
    marks, have = [], {}
    if hist:
        n = max(len(hist) - 1, 1)
        idxs = {0, len(hist) - 1, max(range(len(hist)), key=lambda i: _to_float(hist[i][2].get('wind')) or 0)}
        idxs.update(round(k * n / 10) for k in range(1, 10))
        prev_w = None
        for i, (_, _, p) in enumerate(hist):
            w = _to_float(p.get('wind'))
            if w is None:
                continue
            if prev_w is not None and abs(w - prev_w) >= 5:
                idxs.add(i)
            prev_w = w
        marks = [('实况', *hist[i]) for i in sorted(idxs) if 0 <= i < len(hist)]
    for lng, lat, p in forecast:
        try:
            have[int(p.get('hour'))] = (lng, lat, p)
        except (TypeError, ValueError):
            pass
    marks += [('预报', *have[h]) for h in (6, 12, 18, 24, 36, 48, 60, 72, 96, 120) if h in have]
    out = []
    for m in marks:
        if any(abs(m[1] - x) < 0.28 and abs(m[2] - y) < 0.28 for _, x, y, _ in out):
            continue
        if view:
            lo, hi, la, ha = view
            if not (lo <= m[1] <= hi and la <= m[2] <= ha):
                continue
        out.append(m)
    return out[:20]


def _draw_dot(d, x, y, c, style='hist'):
    """hist实心 / fc空心 / end方块 / cur靶心。"""
    if style == 'fc':
        d.ellipse((x - 5, y - 5, x + 5, y + 5), outline=c, width=2)
        d.ellipse((x - 1.5, y - 1.5, x + 1.5, y + 1.5), fill=c)
    elif style == 'end':
        d.rectangle((x - 5, y - 5, x + 5, y + 5), fill=c, outline=(30, 30, 30))
    elif style == 'cur':
        d.ellipse((x - 9, y - 9, x + 9, y + 9), outline=(220, 30, 30), width=3)
        d.line([(x - 15, y), (x + 15, y)], fill=(220, 30, 30), width=2)
        d.line([(x, y - 15), (x, y + 15)], fill=(220, 30, 30), width=2)
        d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=(220, 30, 30))
    else:
        d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=c, outline=(25, 25, 25))


def _stroke(draw, pts, fill, dash=False):
    if not dash or len(pts) < 2:
        draw.line(pts, fill=fill, width=2)
        return
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy) or 1
        n = max(1, int(L / 10))
        for k in range(n):
            if k % 2:
                continue
            t0, t1 = k / n, min((k + 1) / n, 1)
            draw.line([(x0 + dx * t0, y0 + dy * t0), (x0 + dx * t1, y0 + dy * t1)], fill=fill, width=2)


def _fit_view(hist, forecast):
    lo, hi, la, ha = _VIEW_MAX
    recent = hist[-max(12, len(hist) // 3):] if hist else []
    pts = (recent + forecast) if (recent or forecast) else (hist[-8:] if hist else [])
    if not pts:
        return _VIEW_MAX
    lngs = [a for a, _, _ in pts] + ([hist[-1][0]] if hist else [])
    lats = [b for _, b, _ in pts] + ([hist[-1][1]] if hist else [])
    min_lng, max_lng = min(lngs), max(lngs)
    min_lat, max_lat = min(lats), max(lats)
    if max_lng - min_lng < 10:
        mid = (max_lng + min_lng) / 2
        min_lng, max_lng = mid - 5, mid + 5
    if max_lat - min_lat < 7.5:
        mid = (max_lat + min_lat) / 2
        min_lat, max_lat = mid - 3.8, mid + 3.8
    pad_lng = max((max_lng - min_lng) * 0.38, 2.8)
    pad_lat = max((max_lat - min_lat) * 0.34, 2.2)
    min_lng, max_lng = min_lng - pad_lng, max_lng + pad_lng
    min_lat, max_lat = min_lat - pad_lat, max_lat + pad_lat
    if max_lng - min_lng > 32:
        c = hist[-1][0] if hist else (min_lng + max_lng) / 2
        min_lng, max_lng = c - 16, c + 16
    if max_lat - min_lat > 24:
        c = hist[-1][1] if hist else (min_lat + max_lat) / 2
        min_lat, max_lat = c - 12, c + 12
    if hist:
        clng, clat, cur = hist[-1]
        rkm = max((_to_float(cur.get(k)) or 0) for k in ('radius7', 'radius10', 'radius12'))
        if rkm > 0:
            dlat = rkm / 111.0 * 1.08
            dlng = rkm / (111.0 * max(math.cos(math.radians(clat)), 0.25)) * 1.08
            min_lng, max_lng = min(min_lng, clng - dlng), max(max_lng, clng + dlng)
            min_lat, max_lat = min(min_lat, clat - dlat), max(max_lat, clat + dlat)
    min_lng, max_lng = max(min_lng, lo), min(max_lng, hi)
    min_lat, max_lat = max(min_lat, la), min(max_lat, ha)
    return _VIEW_MAX if max_lng <= min_lng or max_lat <= min_lat else (min_lng, max_lng, min_lat, max_lat)


def _near_city(lng, lat):
    return min(_CN_LABELS, key=lambda t: (t[0] - lng) ** 2 + (t[1] - lat) ** 2)[2]


def _wind_ring(lng, lat, quads, step=6):
    """四象限风圈折线（北起顺时针：NE/SE/SW/NW）。"""
    pts = []
    for qi, (a0, a1) in enumerate(((0, 90), (90, 180), (180, 270), (270, 360))):
        r = max(float(quads[qi]), 1.0)
        for k in range(step):
            ang = math.radians(a0 + (a1 - a0) * k / step)
            dlat = (r / 111.0) * math.cos(ang)
            dlng = (r / (111.0 * max(math.cos(math.radians(lat)), 0.2))) * math.sin(ang)
            pts.append((lng + dlng, lat + dlat))
    return pts


def _hit(a, b, pad=6):
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


def _box_hits_pts(box, pts, pad=12):
    x0, y0, x1, y1 = box
    for px, py in pts:
        if x0 - pad <= px <= x1 + pad and y0 - pad <= py <= y1 + pad:
            return True
    return False


def _rounded_bubble(draw, box, fill, outline, radius=10, width=1):
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    except Exception:
        draw.rectangle(box, fill=fill, outline=outline, width=width)


def _bubble_tail(draw, box, tx, ty, fill, outline, *, tip_gap=7, base=8):
    bx0, by0, bx1, by1 = box
    cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
    dx, dy = tx - cx, ty - cy
    if abs(dx) < 0.1 and abs(dy) < 0.1:
        return None
    dist = math.hypot(dx, dy) or 1
    tip = (tx - dx / dist * tip_gap, ty - dy / dist * tip_gap)
    _, ax, ay = min(
        (
            ('r', bx1, min(max(ty, by0 + 6), by1 - 6)),
            ('l', bx0, min(max(ty, by0 + 6), by1 - 6)),
            ('b', min(max(tx, bx0 + 6), bx1 - 6), by1),
            ('t', min(max(tx, bx0 + 6), bx1 - 6), by0),
        ),
        key=lambda s: (s[1] - tip[0]) ** 2 + (s[2] - tip[1]) ** 2,
    )
    vx, vy = tip[0] - ax, tip[1] - ay
    L = math.hypot(vx, vy) or 1
    p1 = (ax - vy / L * base, ay + vx / L * base)
    p2 = (ax + vy / L * base, ay - vx / L * base)
    draw.polygon([p1, p2, tip], fill=fill, outline=outline)
    draw.line([p1, p2], fill=fill, width=3)
    return tip


def _draw_callout(draw, x, y, lines, font, bounds, occupied, path_pts=None, *, kind='hist'):
    """聊天气泡标注：圆角白底 + 尖尖拉到对应路径点。"""
    gap = 1
    sizes = [_tw(draw, t, font) for t in lines]
    tw = max((w for w, _ in sizes), default=40)
    th = sum(h for _, h in sizes) + gap * max(0, len(lines) - 1)
    bw, bh = tw + 14, th + 10
    mx0, my0, mx1, my1 = bounds
    path_pts = path_pts or []
    fill = (255, 252, 245) if kind != '预报' else (245, 248, 255)
    outline = (90, 110, 130) if kind != '预报' else (70, 100, 160)
    dirs = (
        (1, 0), (-1, 0), (0, -1), (0, 1),
        (0.9, -0.9), (0.9, 0.9), (-0.9, -0.9), (-0.9, 0.9),
        (1.15, -0.4), (-1.15, -0.4), (1.15, 0.4), (-1.15, 0.4),
    )
    best = None
    for dist in (40, 56, 72, 90, 110, 130, 150):
        for dx, dy in dirs:
            L = math.hypot(dx, dy) or 1
            ux, uy = dx / L, dy / L
            bx0 = int(x + ux * dist - bw / 2)
            by0 = int(y + uy * dist - bh / 2)
            bx0 = min(max(bx0, mx0 + 6), mx1 - bw - 6)
            by0 = min(max(by0, my0 + 6), my1 - bh - 6)
            box = (bx0, by0, bx0 + bw, by0 + bh)
            if abs((bx0 + bw / 2) - x) < 20 and abs((by0 + bh / 2) - y) < 20:
                continue
            if any(_hit(box, o, 7) for o in occupied) or _box_hits_pts(box, path_pts, 12):
                continue
            steps = max(4, int(dist / 12))
            corridor_hit = 0
            for k in range(1, steps):
                t = k / steps
                px = bx0 + bw / 2 + (x - (bx0 + bw / 2)) * t
                py = by0 + bh / 2 + (y - (by0 + bh / 2)) * t
                if _box_hits_pts((px - 2, py - 2, px + 2, py + 2), path_pts, 6):
                    corridor_hit += 1
            cxb, cyb = bx0 + bw / 2, by0 + bh / 2
            mind = min((math.hypot(px - cxb, py - cyb) for px, py in path_pts), default=100)
            tip_dist = math.hypot(cxb - x, cyb - y)
            score = mind * 800 + tip_dist * 2 - corridor_hit * 120 - abs(dist - 70) * 2
            if best is None or score > best[0]:
                best = (score, box)
    if not best:
        return None
    _, (bx0, by0, bx1, by1) = best
    box = (bx0, by0, bx1, by1)
    _bubble_tail(draw, box, x, y, fill, outline, tip_gap=6, base=7)
    _rounded_bubble(draw, box, fill, outline, radius=11, width=1)
    ty = by0 + 5
    for i, (t, (_, h)) in enumerate(zip(lines, sizes)):
        color = (25, 45, 80) if i == 0 else (40, 55, 75)
        draw.text((bx0 + 7, ty), t, font=font, fill=color)
        ty += h + gap
    occupied.append(box)
    occupied.append((min(bx0, x) - 4, min(by0, y) - 4, max(bx1, x) + 4, max(by1, y) + 4))
    return box


def _draw_info_panel(draw, lines, font, bounds, occupied, path_pts, prefer='tr', anchor=None):
    """角落聊天气泡信息卡：尖尖拉到当前点。"""
    gap = 1
    sizes = [_tw(draw, t, font) for t in lines]
    tw = max((w for w, _ in sizes), default=80)
    th = sum(h for _, h in sizes) + gap * max(0, len(lines) - 1)
    bw, bh = tw + 16, th + 14
    mx0, my0, mx1, my1 = bounds
    corners = {
        'tl': (mx0 + 8, my0 + 8),
        'tr': (mx1 - bw - 8, my0 + 8),
        'bl': (mx0 + 8, my1 - bh - 8),
        'br': (mx1 - bw - 8, my1 - bh - 8),
    }
    fill, outline = (255, 255, 255), (45, 95, 160)
    chosen = None
    for key in dict.fromkeys((prefer, 'tr', 'tl', 'br', 'bl')):
        bx0, by0 = corners[key]
        box = (bx0, by0, bx0 + bw, by0 + bh)
        if any(_hit(box, o, 6) for o in occupied) or _box_hits_pts(box, path_pts, 10):
            continue
        chosen = box
        break
    if not chosen:
        bx0, by0 = corners['tr']
        chosen = (bx0, by0, bx0 + bw, by0 + bh)
    bx0, by0, bx1, by1 = chosen
    if anchor:
        _bubble_tail(draw, chosen, anchor[0], anchor[1], fill, outline, tip_gap=8, base=9)
    _rounded_bubble(draw, chosen, fill, outline, radius=12, width=2)
    draw.rectangle((bx0 + 2, by0 + 8, bx0 + 5, by1 - 8), fill=(30, 120, 210))
    ty = by0 + 7
    for i, (t, (_, h)) in enumerate(zip(lines, sizes)):
        color = (20, 50, 100) if i == 0 else (30, 40, 60)
        draw.text((bx0 + 11, ty), t, font=font, fill=color)
        ty += h + gap
    occupied.append(chosen)
    if anchor:
        occupied.append((
            min(bx0, anchor[0]) - 4, min(by0, anchor[1]) - 4,
            max(bx1, anchor[0]) + 4, max(by1, anchor[1]) + 4,
        ))
    return chosen


def _clip_poly(ring, minx, maxx, miny, maxy):
    """裁到视野，避免顶点出画布后 Pillow 填色失败。"""
    pts = list(ring or ())
    if len(pts) < 3:
        return []

    def clip(src, inside, inter):
        if not src:
            return []
        out, prev = [], src[-1]
        prev_in = inside(prev)
        for cur in src:
            cur_in = inside(cur)
            if cur_in:
                if not prev_in:
                    out.append(inter(prev, cur))
                out.append(cur)
            elif prev_in:
                out.append(inter(prev, cur))
            prev, prev_in = cur, cur_in
        return out

    def lerp(a, b, axis, v):
        x1, y1 = a
        x2, y2 = b
        if axis == 'x':
            t = 0 if x2 == x1 else (v - x1) / (x2 - x1)
            return (v, y1 + t * (y2 - y1))
        t = 0 if y2 == y1 else (v - y1) / (y2 - y1)
        return (x1 + t * (x2 - x1), v)

    pts = clip(pts, lambda p: p[0] >= minx, lambda a, b: lerp(a, b, 'x', minx))
    pts = clip(pts, lambda p: p[0] <= maxx, lambda a, b: lerp(a, b, 'x', maxx))
    pts = clip(pts, lambda p: p[1] >= miny, lambda a, b: lerp(a, b, 'y', miny))
    pts = clip(pts, lambda p: p[1] <= maxy, lambda a, b: lerp(a, b, 'y', maxy))
    return pts if len(pts) >= 3 else []


# 内陆补块（川渝陕滇甘等无省界数据时，裁剪后仍能填陆地）
_INLAND_FILL = (
    (102.0, 24.0), (104.2, 22.8), (107.2, 23.4), (110.2, 25.0), (113.2, 26.6),
    (116.2, 28.6), (118.2, 31.2), (119.6, 34.2), (119.2, 37.6), (116.6, 40.2),
    (112.2, 38.6), (108.2, 36.6), (104.2, 35.0), (101.4, 32.0), (101.2, 27.4),
    (102.0, 24.0),
)


def _paint_land(ld, lp, min_lng, max_lng, min_lat, max_lat):
    pad_lng = max((max_lng - min_lng) * 0.04, 0.4)
    pad_lat = max((max_lat - min_lat) * 0.04, 0.3)
    box = (min_lng - pad_lng, max_lng + pad_lng, min_lat - pad_lat, max_lat + pad_lat)
    land, coast, border = (232, 220, 190), (150, 140, 120), (70, 60, 50)

    def fill_rings(rings, outline=None):
        for ring in rings:
            xs = [a for a, _ in ring]
            ys = [b for _, b in ring]
            if max(xs) < box[0] or min(xs) > box[1] or max(ys) < box[2] or min(ys) > box[3]:
                continue
            clipped = _clip_poly(ring, *box)
            if len(clipped) < 3:
                continue
            ld.polygon([lp(*p) for p in clipped], fill=land, outline=outline)

    fill_rings((_INLAND_FILL,))
    fill_rings(_LAND_POLYS, outline=coast)
    fill_rings(_PROV_LINES, outline=border)


def render_track_png(view):
    """纯 2D 俯视平面图：信息写在路径上，无底表/注脚。"""
    from PIL import Image, ImageDraw

    hist, forecast = _collect_track(view)
    if not hist:
        return None
    min_lng, max_lng, min_lat, max_lat = _fit_view(hist, forecast)
    marks = _pick_marks(hist, forecast, view=(min_lng, max_lng, min_lat, max_lat))

    W, H, HEAD = 940, 780, 100
    pad_l, pad_b, pad_r = 58, 40, 18
    mx0, my0 = pad_l, HEAD
    mw, mh = W - pad_l - pad_r, H - HEAD - pad_b
    img = Image.new('RGB', (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    ft, fs, fxs = _font(38), _font(26), _font(24)

    cn, _ = _display_name(view)
    num = str(view.get('num') or '')
    no = num[-2:] if len(num) >= 2 else (num or '?')
    try:
        no = str(int(no))
    except ValueError:
        pass
    hours = max((int(x[2].get('hour') or 0) for x in forecast), default=0) if forecast else 0
    alive = bool(forecast)  # 仅最新点有预报才算「进行中」
    title = f'今年第{no}号台风“{cn}”' + (f'未来{hours}小时路径预报图' if alive else '路径实况图')
    if ft:
        tw, _ = _tw(d, title, ft)
        d.text(((W - tw) / 2, 8), title, font=ft, fill=(20, 20, 20))
    sub = f'{_tshort(hist[0][2].get("time"))} — {_tshort(hist[-1][2].get("time"))}'
    sub += (f' · 预报{hours}h' if alive else ' · 已停编/无预报') + '（北京时）'
    if fs:
        sw, _ = _tw(d, sub, fs)
        d.text(((W - sw) / 2, 56), sub, font=fs, fill=(80, 80, 80))

    sl, sa = max(max_lng - min_lng, 0.01), max(max_lat - min_lat, 0.01)

    def proj(lng, lat):
        return mx0 + (lng - min_lng) / sl * mw, my0 + (max_lat - lat) / sa * mh

    # 底图：海 + 陆 + 省界（画在子图层再贴上，自动裁切）
    layer = Image.new('RGB', (mw, mh), (186, 216, 236))
    ld = ImageDraw.Draw(layer)

    def lp(lng, lat):
        return (lng - min_lng) / sl * mw, (max_lat - lat) / sa * mh

    _paint_land(ld, lp, min_lng, max_lng, min_lat, max_lat)
    for i in range(5):
        x, y = mw * i / 4, mh * i / 4
        ld.line([(x, 0), (x, mh)], fill=(160, 190, 210), width=1)
        ld.line([(0, y), (mw, y)], fill=(160, 190, 210), width=1)
    img.paste(layer, (mx0, my0))
    d = ImageDraw.Draw(img)
    for i in range(5):
        x, y = mx0 + mw * i / 4, my0 + mh * i / 4
        if fxs:
            lon = min_lng + (max_lng - min_lng) * i / 4
            lat = max_lat - (max_lat - min_lat) * i / 4
            _put(d, x, my0 + mh + 6, f'{lon:.0f}°E', fxs, (70, 90, 110), 'mt')
            _put(d, mx0 - 6, y, f'{lat:.0f}°N', fxs, (70, 90, 110), 'rm')
    d.rectangle((mx0, my0, mx0 + mw, my0 + mh), outline=(40, 40, 40), width=2)

    if fs:
        for lng, lat, name in _CN_LABELS:
            if not (min_lng <= lng <= max_lng and min_lat <= lat <= max_lat):
                continue
            x, y = proj(lng, lat)
            _put(d, x + 8, y, name, fs, (40, 55, 80), 'lm', stroke=(255, 255, 255))

    cur_lng, cur_lat, cur = hist[-1]
    if alive:
        wind_ov = Image.new('RGBA', img.size, (0, 0, 0, 0))
        wod = ImageDraw.Draw(wind_ov)
        for qkey, fill, edge in (
            ('radius7q', (255, 215, 0, 45), (220, 180, 0, 160)),
            ('radius10q', (255, 140, 0, 55), (230, 110, 0, 180)),
            ('radius12q', (230, 40, 40, 65), (200, 20, 20, 200)),
        ):
            qs = cur.get(qkey)
            if not qs:
                r = _to_float(cur.get(qkey[:-1]))
                qs = [r, r, r, r] if r else None
            if not qs:
                continue
            poly = [proj(a, b) for a, b in _wind_ring(cur_lng, cur_lat, qs)]
            if len(poly) >= 3:
                wod.polygon(poly, fill=fill, outline=edge)
        img = Image.alpha_composite(img.convert('RGBA'), wind_ov).convert('RGB')
        d = ImageDraw.Draw(img)

    if forecast:
        cone = [(cur_lng, cur_lat)] + [(a, b) for a, b, _ in forecast]
        left, right = [], []
        for i, (lng, lat) in enumerate(cone):
            half = 0.22 + i * 0.16
            j0, j1 = max(i - 1, 0), min(i + 1, len(cone) - 1)
            dx, dy = cone[j1][0] - cone[j0][0], cone[j1][1] - cone[j0][1]
            L = math.hypot(dx, dy) or 1
            left.append(proj(lng - dy / L * half, lat + dx / L * half))
            right.append(proj(lng + dy / L * half, lat - dx / L * half))
        poly = left + right[::-1]
        if len(poly) >= 3:
            ov = Image.new('RGBA', img.size, (0, 0, 0, 0))
            ImageDraw.Draw(ov).polygon(poly, fill=(160, 80, 200, 45))
            img = Image.alpha_composite(img.convert('RGBA'), ov).convert('RGB')
            d = ImageDraw.Draw(img)

    def in_view(lng, lat):
        return min_lng <= lng <= max_lng and min_lat <= lat <= max_lat

    def draw_path(pts, fill, dash=False):
        buf = []
        for lng, lat, *_ in pts:
            if in_view(lng, lat):
                buf.append(proj(lng, lat))
            else:
                if len(buf) >= 2:
                    _stroke(d, buf, fill, dash)
                buf = []
        if len(buf) >= 2:
            _stroke(d, buf, fill, dash)

    if len(hist) > 1:
        draw_path(hist, (35, 35, 35))
    if forecast:
        draw_path([hist[-1]] + forecast, (70, 70, 100), dash=True)

    step = max(1, len(hist) // 40)
    for lng, lat, p in hist[::step]:
        if in_view(lng, lat):
            _draw_dot(d, *proj(lng, lat), _color(p.get('level'), p.get('wind')), 'hist')
    for lng, lat, p in forecast:
        if in_view(lng, lat):
            _draw_dot(d, *proj(lng, lat), _color(p.get('level'), p.get('wind')), 'fc')

    cx, cy = proj(cur_lng, cur_lat)
    _draw_dot(d, cx, cy, _color(cur.get('level'), cur.get('wind')), 'cur' if alive else 'end')

    path_pts = [proj(a, b) for a, b, *_ in hist[::max(1, len(hist) // 50)] + hist[-1:] if in_view(a, b)]
    path_pts += [proj(a, b) for a, b, *_ in forecast if in_view(a, b)]
    path_pts.append((cx, cy))
    bounds = (mx0, my0, mx0 + mw, my0 + mh)
    occupied = []

    if fxs:
        lx, ly = mx0 + 8, my0 + 8
        for name, col, st in (
            ('实况', (70, 130, 255), 'hist'),
            ('预报', (70, 130, 255), 'fc'),
            ('当前' if alive else '终点', (220, 30, 30), 'cur' if alive else 'end'),
        ):
            _draw_dot(d, lx + 10, ly + 12, col, st)
            _put(d, lx + 24, ly + 12, name, fxs, (40, 40, 40), 'lm')
            ly += 32
        if alive and any(cur.get(k) for k in ('radius7', 'radius10', 'radius12')):
            for name, col in (('7级风圈', (255, 215, 0)), ('10级风圈', (255, 140, 0)), ('12级风圈', (230, 40, 40))):
                d.rectangle((lx, ly + 6, lx + 18, ly + 22), fill=col, outline=(60, 60, 60))
                _put(d, lx + 24, ly + 14, name, fxs, (40, 40, 40), 'lm')
                ly += 32
        occupied.append((mx0 + 6, my0 + 6, mx0 + 180, ly + 4))

    place = _near_city(cur_lng, cur_lat)
    info_lines = [f'{"当前实况" if alive else "最后实况"} {_tshort(cur.get("time"))}', f'近{place}']
    info_lines += [f'{cur_lat}°N  {cur_lng}°E', f'风速 {_wind_txt(cur.get("wind"))}']
    if cur.get('strong'):
        info_lines.append(f'等级 {cur.get("strong")}')
    if cur.get('pressure') not in (None, ''):
        info_lines.append(f'气压 {cur.get("pressure")} hPa')
    if cur.get('move'):
        mv = str(cur.get('move'))
        if cur.get('movespeed') not in (None, ''):
            mv += f' {cur.get("movespeed")}km/h'
        info_lines.append(f'移向 {mv}')
    if alive:
        rparts = _radius_txt(cur)
        if rparts:
            info_lines.append('风圈 ' + ' / '.join(rparts))
    prefer = 'tl' if cx > mx0 + mw * 0.5 else 'tr'
    _draw_info_panel(
        d, info_lines, fxs or fs, bounds, occupied, path_pts,
        prefer=prefer, anchor=(cx, cy),
    )

    for kind, lng, lat, p in marks:
        if not in_view(lng, lat) or (abs(lng - cur_lng) < 0.35 and abs(lat - cur_lat) < 0.35):
            continue
        strong = p.get('strong') or _LEVEL_CN.get(str(p.get('level') or ''), '') or ''
        if kind == '预报':
            lines = [f'预报 +{p.get("hour")}h', _wind_txt(p.get('wind'))]
            if strong:
                lines.append(strong)
            if p.get('pressure') not in (None, ''):
                lines.append(f'{p.get("pressure")}hPa')
        else:
            lines = [f'实况 {_tshort(p.get("time"))}', _wind_txt(p.get('wind'))]
            if strong:
                lines.append(strong)
            if p.get('pressure') not in (None, ''):
                lines.append(f'{p.get("pressure")}hPa')
            if p.get('move'):
                mv = str(p.get('move'))
                if p.get('movespeed') not in (None, ''):
                    mv += f'{p.get("movespeed")}km/h'
                lines.append(mv)
        _draw_callout(d, *proj(lng, lat), lines, fxs or fs, bounds, occupied, path_pts, kind=kind)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def _stamp_caption(data, text, *, max_side=900):
    try:
        from PIL import Image, ImageDraw
        im = Image.open(io.BytesIO(data)).convert('RGB')
        w, h = im.size
        m = max(w, h) or 1
        if m > max_side:
            im = im.resize((max(1, int(w * max_side / m)), max(1, int(h * max_side / m))), Image.Resampling.LANCZOS)
            w, h = im.size
        font = _font(28) or _font(22)
        probe, max_w, lines = ImageDraw.Draw(im), w - 20, []
        for raw in (text or '').split('\n'):
            raw = raw.rstrip()
            if not raw or not font:
                lines.append(raw[:42] if raw else '')
                continue
            cur = ''
            for ch in raw:
                if cur and _tw(probe, cur + ch, font)[0] > max_w:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            if cur:
                lines.append(cur)
        lines = lines[:16]
        bar = 16 + 36 * max(1, len(lines))
        out = Image.new('RGB', (w, h + bar), (255, 255, 255))
        out.paste(im, (0, 0))
        d = ImageDraw.Draw(out)
        d.line([(0, h), (w, h)], fill=(200, 200, 200))
        for i, line in enumerate(lines):
            d.text((10, h + 8 + i * 36), line, font=font, fill=(25, 35, 50))
        blob = data
        for q in (76, 58):
            buf = io.BytesIO()
            out.save(buf, format='JPEG', quality=q, optimize=True)
            blob = buf.getvalue()
            if len(blob) <= 380_000:
                break
        return blob, out.size
    except Exception as e:
        _tf_log.warning('图文合成失败: %s', e)
        return data, (900, 700)


def _reply_ok(result):
    if result in (False, None):
        return False
    if isinstance(result, tuple):
        return bool(result) and result[0] is True
    if isinstance(result, dict):
        return result.get('code') in (None, 0, '0')
    return bool(result)


async def _send_one(event, image_bytes, title, buttons, size=None):
    from core.application import get_app
    if not size:
        try:
            from PIL import Image
            size = Image.open(io.BytesIO(image_bytes)).size
        except Exception:
            size = (900, 700)
    w, h = size
    app = get_app()
    bot = app.get_bot(event.appid) if app else None
    hosting = app.module_manager.get('image_hosting') if app and app.module_manager else None
    url = None
    if hosting:
        try:
            url = await hosting.upload_any(
                image_bytes, 'typhoon_track.jpg',
                token_manager=getattr(bot, 'token_manager', None) if bot else None,
            )
        except Exception as e:
            _tf_log.warning('图床上传失败: %s', e)
    head = _tf_at(event)
    if url:
        md = f'{head}\n![台风路径 #{w}px #{h}px]({url})' if head else f'![台风路径 #{w}px #{h}px]({url})'
        for force in (True, False):
            try:
                r = await event.reply(
                    md, buttons=buttons, msg_type=2, skip_suffix=True,
                    force_verify_image_resource=force,
                )
                if _reply_ok(r):
                    return True
                _tf_log.warning('合并 Markdown 失败(force=%s): %s', force, r)
            except Exception as e:
                _tf_log.warning('合并 Markdown 异常(force=%s): %s', force, e)
    cap = f'{head}\n{title}' if head else (title or '台风路径')
    sent = False
    try:
        sender = getattr(bot, 'sender', None)
        fi = await sender.upload_media(event, image_bytes, 1, file_name='typhoon_track.jpg') if sender else None
        if fi:
            sent = _reply_ok(await event.reply(cap, media={'file_info': fi}, skip_suffix=True))
    except Exception as e:
        _tf_log.warning('媒体上传失败: %s', e)
    if not sent:
        try:
            sent = _reply_ok(await event.reply_image(image_bytes, cap))
        except Exception as e:
            _tf_log.warning('reply_image 失败: %s', e)
    if sent and buttons:
        await safe_reply(event, '快捷操作', buttons)
    return sent


def _defense_tips(view):
    pts = view.get('points') or []
    if view.get('status') == 'stop' or not pts:
        return []
    p = pts[-1]
    wind, level = _to_float(p.get('wind')) or 0, str(p.get('level') or '')
    strong = p.get('strong') or _LEVEL_CN.get(level, '') or ''
    if wind >= 41 or level in ('STY', 'SuperTY', 'SUPERTY') or '强台风' in strong or '超强' in strong:
        tips = [
            '可能出现极端狂风暴雨，请进入坚固建筑避险',
            '停止一切户外作业与海上活动，服从转移安排',
            '储备饮水干粮手电；远离玻璃幕墙与临时搭建物',
            '注意山洪地质灾害与城镇内涝，勿涉水驾车',
        ]
    elif wind >= 24.5 or level in ('TY', 'STY', 'SuperTY', 'SUPERTY') or '台风' in strong:
        tips = [
            '严格按预警行动，非必要不外出、不上山不下海',
            '加固门窗、收起阳台物品；停驶高架/临海道路',
            '避开工地、广告牌、大树；停电备手电与饮水',
            '沿海渔排、养殖设施提前加固或撤离人员',
        ]
    else:
        tips = [
            '关注当地气象预警，减少不必要外出',
            '关好门窗，移走阳台易坠物、加固遮阳棚',
            '低洼/临水处注意积水与内涝，远离广告牌树下',
        ]
    r7 = _to_float(p.get('radius7')) or 0
    if r7 >= 200:
        tips.append(f'当前7级风圈约{int(r7)}km，影响范围较大请提前防范')
    return tips


def fmt_detail(view, *, map_note='', plain=False):
    cn, en = _display_name(view)
    title = f'{cn}' + (f'（{en}）' if en and en != cn else '')
    if not plain:
        title = f'**{title}**'
    pts = view.get('points') or []
    if not pts:
        return f'{title}\n暂无路径点' if plain else title + '\n```\n暂无路径点\n```'
    p = pts[-1]
    alive = view.get('status') != 'stop'
    block = []
    if view.get('num'):
        block.append(f'编号  {view.get("num")}')
    block += [
        f'状态  {"活跃" if alive else "停编"}',
        f'强度  {p.get("strong") or "-"}',
        f'气压  {p.get("pressure", "-")} hPa',
        f'风速  {_wind_txt(p.get("wind"))}',
        f'位置  {p.get("lat")}°N  {p.get("lng")}°E',
    ]
    if p.get('move'):
        mv = str(p.get('move'))
        if p.get('movespeed') not in (None, ''):
            mv += f'  {p.get("movespeed")} km/h'
        block.append(f'移向  {mv}')
    rparts = _radius_txt(p)
    if rparts:
        block.append('风圈  ' + ' / '.join(rparts))
    if p.get('time'):
        block.append(f'时间  {p.get("time")}（北京时）')
    block.append(f'参考  近{_near_city(_to_float(p.get("lng")) or 0, _to_float(p.get("lat")) or 0)}')
    fc = (p.get('forecasts') or []) if alive else []
    if fc:
        block.append('预报  ' + ' · '.join(
            f'+{f.get("hour")}h {_LEVEL_CN.get(f.get("level"), f.get("level") or "")}' for f in fc[:5]
        ))
    if map_note:
        block.append(map_note)
    tips = _defense_tips(view) if alive else []
    if plain:
        extra = (['防御建议：'] + [f'{i}. {t}' for i, t in enumerate(tips, 1)]) if tips else []
        return '\n'.join([title, *block, *extra])
    md = f'{title}\n\n```\n' + '\n'.join(block) + '\n```'
    if tips:
        md += '\n\n**防御建议**\n```\n' + '\n'.join(f'{i}. {t}' for i, t in enumerate(tips, 1)) + '\n```'
    return md


async def reply_detail(event, view, buttons, *, t0=None):
    png, map_note, src = None, '', ''
    try:
        off, src = (None, 'no_official') if view.get('status') == 'stop' else await fetch_official_track_png(view)
        if off and isinstance(src, str) and src.startswith('http'):
            png, map_note = off, '配图：中央气象台官方路径产品图'
        elif src == 'download_fail':
            _tf_log.warning('官网路径图下载失败，改用本地绘图')
    except Exception as e:
        _tf_log.warning('官网路径图失败: %s', e)
    if not png:
        try:
            png = await asyncio.to_thread(render_track_png, view)
            if png:
                map_note = '配图：本地路径图（官网暂无该台风产品图）'
        except Exception as e:
            _tf_log.warning('本地路径图失败: %s', e)

    suffix = f'\n耗时 {_ms(t0)}ms' if t0 is not None else ''
    caption = fmt_detail(view, map_note=map_note, plain=True) + suffix
    title = _display_name(view)[0] or '台风路径'
    if png:
        raw, size = await asyncio.to_thread(_stamp_caption, png, caption)
        if await _send_one(event, raw, title, buttons, size):
            return True
        _tf_log.warning('单条图文发送失败，退回纯文字')
    await safe_reply(event, fmt_detail(view, map_note=map_note) + suffix, buttons)
    return True


def save_jilu(uid, keyword, view):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            'INSERT INTO jilu (user, keyword, time, summary) VALUES (?, ?, ?, ?)',
            (uid, keyword, int(time.time()), f'{view.get("cn")}({view.get("num") or view.get("id")})'),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------- 指令 ----------

async def _send_year(event, start, year, refresh, extra):
    bundle = await nmc_list(year)
    if bundle is None:
        await safe_reply(event, _FAIL)
        return
    if year:
        bundle['year'] = year
    md = (fmt_year(bundle) if year else fmt_list_active(bundle['list'])) + f'\n\n耗时：{_ms(start)}ms'
    await safe_reply(event, md, _btns([('刷新', refresh), extra], _QH))


@handler(r'^\s*/?台风\s*$', name='台风', desc='当前最强活跃台风', ignore_at_check=True)
@guard
async def cmd_strongest(event, match):
    start = time.time()
    bundle = await nmc_list()
    if bundle is None:
        await safe_reply(event, _FAIL)
        return
    active = [x for x in bundle['list'] if x['status'] == 'start']
    if not active:
        await safe_reply(event, '当前暂无活跃台风')
        return
    views = await asyncio.gather(*[nmc_view(it['id']) for it in active], return_exceptions=True)
    best_view, best_wind = None, -1
    for it, view in zip(active, views):
        if isinstance(view, Exception) or not view or not view.get('points'):
            continue
        w = _to_float(view['points'][-1].get('wind')) or 0
        if w >= best_wind:
            best_wind, best_view = w, view
    if not best_view:
        await safe_reply(event, '活跃台风详情获取失败')
        return
    await reply_detail(event, best_view, _btns([('刷新', '台风'), ('列表', '台风列表')], _QH), t0=start)


@handler(r'^\s*/?台风(?:列表|活跃)(?:\s+(\d{4}))?\s*$', name='台风列表', desc='活跃或按年列表', ignore_at_check=True)
@guard
async def cmd_list(event, match):
    year = int(match.group(1)) if match.group(1) else None
    await _send_year(event, time.time(), year, f'台风列表 {year}' if year else '台风列表', ('本年', '台风年份'))


@handler(r'^\s*/?台风年份(?:\s+(\d{4}))?\s*$', name='台风年份', desc='按年列表', ignore_at_check=True)
@guard
async def cmd_year(event, match):
    year = int(match.group(1)) if match.group(1) else datetime.now().year
    await _send_year(event, time.time(), year, f'台风年份 {year}', ('活跃', '台风列表'))


@handler(r'^\s*/?台风查询\s*(.+?)\s*$', name='台风详情', desc='查详情与路径图', ignore_at_check=True)
@guard
async def cmd_detail(event, match):
    start = time.time()
    keyword = (match.group(1) or '').strip()
    if not keyword:
        await safe_reply(event, '请输入名称/编号/ID，例如：台风查询 沙德尔')
        return
    if re.fullmatch(r'(19|20)\d{2}', keyword):
        await _send_year(event, start, int(keyword), f'台风查询 {keyword}', ('活跃', '台风列表'))
        return
    tid, err = await resolve_id(keyword)
    if tid is None:
        await safe_reply(event, err or '未找到')
        return
    view = await nmc_view(tid)
    if not view:
        await safe_reply(event, f'详情获取失败（ID {tid}）')
        return
    save_jilu(event.user_id, keyword, view)
    await reply_detail(event, view, _btns([('刷新', f'台风查询 {keyword}'), ('列表', '台风列表')], _QH), t0=start)


@handler(r'^\s*/?台风帮助\s*$', name='台风帮助', desc='帮助', ignore_at_check=True)
@guard
async def _tf_help(event, match):
    md = (
        '**台风查询** v1.0.0 · 中央气象台\n\n'
        f'> {_inline("台风")} 当前最强活跃台风 + 路径图\n'
        f'> {_inline("台风列表")} 活跃列表\n'
        f'> {_inline("台风列表 2024")} / {_inline("台风年份")} 按年\n'
        f'> {_inline("台风查询 ", "台风查询")} 名称/编号/ID\n'
        f'> {_inline("我的台风记录")}\n\n'
        f'示例：{_inline("台风查询 沙德尔")} · {_inline("台风查询 2618")} · {_inline("台风查询 巴威")}\n\n'
        '配图：优先中央台官方路径产品图；官网没有则本地聊天气泡标注图\n'
        '活跃台风附防御建议（按强度分级）\n'
        '另有：台风官网*（只要官方图）· 台风本地*（只要本地图）\n'
        '说明文档：docs/台风插件/\n'
        '数据：typhoon.nmc.cn / www.nmc.cn\n'
        '原创：茉莉奶绿 · 修改优化：飞行漂绒'
    )
    await safe_reply(event, md, _btns([('台风', '台风'), ('列表', '台风列表')], _QH))


@handler(r'^\s*/?我的台风记录\s*$', name='我的记录', desc='查询历史', ignore_at_check=True)
@guard
async def cmd_history(event, match):
    name = await get_name(event, event.user_id)
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        'SELECT keyword, time, summary FROM jilu WHERE user = ? ORDER BY time DESC LIMIT 10',
        (event.user_id,),
    ).fetchall()
    conn.close()
    if not rows:
        await safe_reply(event, f'{name} 暂无查询记录')
        return
    md = f'**{name} 的查询记录**\n\n'
    for row in rows:
        md += f'{datetime.fromtimestamp(row[1]).strftime("%m-%d %H:%M")} {row[0]} → {row[2]}\n'
    await safe_reply(event, md, _btns([('刷新', '我的台风记录'), ('列表', '台风列表')], _QH))


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
_NSFW_HARD_CLASSES = frozenset({  # 敏感暴露类别 → 直接跳过 (QQ 必拦级别)
    "exposed_anus", "exposed_breasts", "exposed_buttocks",
    "exposed_female_genitalia", "exposed_male_genitalia", "exposed_pussy",
})
_NSFW_HARD_TH = 0.60              # 硬过滤阈值 (更确定才拦截, 减少二次元画风误杀)
# 软过滤已移除: 腋下/肚子/脚等二次元常见元素不再拦截, 交给 QQ 审核与黑名单兜底

_chahua_blacklist: set = set()
_chahua_blacklist_loaded = False
_chahua_violations = 0
_chahua_cooldown_until = 0.0
_chahua_user_count: dict = {}          # uid -> 当前组内已发次数
_chahua_user_lock_until: dict = {}     # uid -> 组间 15 秒锁定解除时间
_nsfw_detector = None
_nsfw_lock = threading.Lock()
_chahua_send_url = contextvars.ContextVar("chahua_send_url", default="")
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


def _chahua_is_nsfw(data: bytes) -> bool:
    """本地 NSFW 检测: 暴露/擦边 → True(跳过)。检测失败/模型不可用 → False(放行)"""
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
            return False
        for item in results:
            cls = str(item.get("class", "")).lower()
            score = float(item.get("score", 0) or 0)
            if cls in _NSFW_HARD_CLASSES and score >= _NSFW_HARD_TH:
                return True
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("插画 NSFW 检测异常(放行): %s", e)
        return False


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


async def _chahua_pick(max_try: int = 10):
    """随机取一张安全图: 排除黑名单 + NSFW 过滤, 优先 jpg/png。
    返回 (jpeg, title, url); 无可用图返回 (None, None, None)"""
    cards = _chahua_load_cards()
    if not cards:
        return None, None, None
    hi = [c for c in cards if not c["img"].endswith(".webp")]
    pool = list(hi if len(hi) >= max_try // 2 else cards)
    random.shuffle(pool)
    for card in pool[:max_try]:
        url = card["img"]
        if _chahua_is_blacklisted(url):
            continue
        data = await _chahua_download(url)
        if not data or not _chahua_has_content(data):
            continue
        if _chahua_is_nsfw(data):
            log.info("插画跳过擦边图: %s", url[:80])
            continue
        return _chahua_to_jpeg(data), card.get("title", ""), url
    # 兜底: 放宽质量过滤再来一轮 (仍走黑名单+NSFW)
    for card in random.sample(cards, min(8, len(cards))):
        url = card["img"]
        if _chahua_is_blacklisted(url):
            continue
        data = await _chahua_download(url)
        if data and len(data) >= 30000:
            if _chahua_is_nsfw(data):
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
            # 重置错误标记, 避免上一轮违规残留导致误判
            with contextlib.suppress(Exception):
                event.error = None
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
        except Exception as e:  # noqa: BLE001
            log.warning("插画发图失败: %s", e)
            return await event.reply("❌ 图片发送失败，再试一次吧")
        # 检查发送是否违规被拦截 (QQ 返回字段可能是 code 或 err_code)
        err = getattr(event, "error", None)
        is_violation = (
            isinstance(err, dict)
            and (err.get("code") == 40034006 or err.get("err_code") == 40034006)
        )
        if is_violation:
            # 黑名单/计数已由 send_failed 钩子处理, 这里只负责换图重试
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
        log.info("插画图库自动更新: 开始采集")
        items = await _chahua_fetch_list_pages()
        if not items:
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
            log.info("插画图库自动更新完成: %d 张 (%d 个详情页)", len(uniq), len(items))
        except Exception as e:  # noqa: BLE001
            log.warning("插画图库写入失败: %s", e)


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

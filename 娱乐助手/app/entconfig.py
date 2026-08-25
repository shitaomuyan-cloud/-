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
    # 生图服务 (OpenAI 兼容接口; 密钥/代理仅存本地 data/ 配置, 不进仓库)
    "draw_api_base": "",
    "draw_api_key": "",
    "draw_model": "gpt-image-2",
    "draw_proxy": "",
    "draw_safety_enabled": True,  # 生图描述安全审核 (调 ai_llm 模块 LLM 判定)
    "draw_blocked_words": "裸体,裸露,裸照,裸聊,半裸,全裸,光着身子,不穿衣服,没穿衣服,不穿上衣,一丝不挂,脱衣,脱光,强奸,强暴,迷奸,轮奸,诱奸,性侵,猥亵,性骚扰,做爱,性交,交媾,床事,啪啪,打炮,色情,情色,淫秽,淫乱,黄色网站,黄网,黄片,毛片,成人电影,成人影片,色情电影,色情片,av,女优,男优,av女优,乳房,胸部,乳沟,屁股,翘臀,阴部,阴茎,阴道,阴唇,阴蒂,生殖器,性器官,龟头,精液,口交,乳交,肛交,手淫,自慰,打飞机,撸管,春药,迷药,催情药,伟哥,援交,卖淫,嫖娼,招嫖,约炮,一夜情,裸贷,色情服务,性交易,血腥,斩首,肢解,虐杀,酷刑,分尸,碎尸,活埋,枪决,屠杀,灭门,吃人,虐待狂,习近平,毛泽东,邓小平,江泽民,胡锦涛,温家宝,李克强,天安门事件,六四,六四事件,法轮功,台独,藏独,疆独,港独,独立建国,颠覆国家,反动,冰毒,海洛因,大麻,可卡因,摇头丸,麻古,k粉,制毒,毒品配方,冰毒制作,制毒工艺,枪支,弹药,制枪,炸弹制作,爆炸物,雷管,赌博,赌场,博彩,六合彩,百家乐,兼职刷单,刷单,传销,电信诈骗,洗钱,裸聊诈骗,代开发票,发票代开,办假证,假证,高仿,精仿,傻逼,妈的,他妈,草泥马,去死,贱人,婊子,狗日的,操你妈,日你妈,脑残,智障,nsfw,porn,xxx,nude,naked,sexy,sex,sexual,erotic,blowjob,handjob,gangbang,milf,tits,asshole,pussy,dick,cock,penis,vagina,boobs,busty,kill,murder,torture,blood,gore,rape,性感,淫荡,裸照门,情色小说,色情漫画,成人网站,夜场,嫖客,情趣内衣,丁字裤,情色交易,肉偿,偷拍,透视装,裸体艺术,幼女,恋童,杀戮",  # 本地敏感词(逗号分隔, 留空禁用本地检查)
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

"""娱乐助手 - 游戏逻辑: 签到、抽奖、抢劫、反甲、同归于尽、红包、扣分。"""

import random
import time
from datetime import datetime

from . import points as p
from . import entconfig

# 红包有效期: 30 分钟
REDPACK_TTL = 30 * 60

# ---------- 通用积分开销 (默认值, 实际按群配置) ----------
MUTE_COST = entconfig.DEFAULTS["mute_cost"]
REVOKE_COST = entconfig.DEFAULTS["revoke_cost"]
ARMOR_COST = entconfig.DEFAULTS["armor_cost"]
DRAW_COST = entconfig.DEFAULTS["draw_cost"]


def can_afford(user_id, cost) -> bool:
    return p.get_points(user_id) >= cost


def charge(user_id, cost) -> bool:
    """尝试扣除 cost 积分，成功返回 True。"""
    if not can_afford(user_id, cost):
        return False
    p.add_points(user_id, -cost)
    return True


def refund(user_id, cost) -> None:
    """退还积分（用于失败回滚）。"""
    p.add_points(user_id, cost)


# ---------- 签到 ----------

SIGN_LO = entconfig.DEFAULTS["sign_lo"]
SIGN_HI = entconfig.DEFAULTS["sign_hi"]


def sign(user_id, today_str: str = None):
    """签到: 每日一次, 随机积分 (按群配置)。返回 (gained, total, already_signed)。"""
    if today_str is None:
        today_str = p.today_sign_key()
    if p.last_sign_date(user_id) == today_str:
        return 0, p.get_points(user_id), True
    cfg = entconfig.get_current()
    gained = p.random_points(cfg["sign_lo"], cfg["sign_hi"])
    total = p.add_points(user_id, gained)
    p.set_last_sign_date(user_id, today_str)
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
        return 0, p.get_points(user_id), True, False
    charge(user_id, cost)
    is_win = random.random() < cfg["lottery_win_rate"]
    if is_win:
        won = p.random_points(cfg["lottery_lo"], cfg["lottery_hi"])
        total = p.add_points(user_id, won)
    else:
        won = 0
        total = p.get_points(user_id)
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
    stolen = p.random_points(cfg["robbery_lo"], cfg["robbery_hi"])
    if stolen == 0:
        return 0, p.get_points(attacker_id), p.get_points(defender_id), False, False

    # 反甲反弹
    if p.has_armor(defender_id):
        p.consume_armor(defender_id)
        actual = min(stolen, p.get_points(attacker_id))
        if actual > 0:
            p.add_points(attacker_id, -actual)
            p.add_points(defender_id, actual)
        return actual, p.get_points(attacker_id), p.get_points(defender_id), False, True

    success = random.random() < entconfig.get_current()["robbery_rate"]
    if success:
        defender_pts = p.get_points(defender_id)
        actual = min(stolen, defender_pts)
        p.add_points(attacker_id, actual)
        p.add_robbed(attacker_id, actual)
        p.add_points(defender_id, -actual)
        return actual, p.get_points(attacker_id), p.get_points(defender_id), True, False

    attacker_pts = p.get_points(attacker_id)
    actual = min(stolen, attacker_pts)
    if actual == 0:
        return 0, attacker_pts, p.get_points(defender_id), False, False
    p.add_points(attacker_id, -actual)
    p.add_points(defender_id, actual)
    return actual, p.get_points(attacker_id), p.get_points(defender_id), False, False


# ---------- 同归于尽 ----------

def mutual_destruction(initiator_id, target_id):
    """同归于尽：双方按'当前余额'结算（抢劫已实时改过余额，先来后到自然成立）。
    双方各扣 min(双方余额)：余额少者归零，余额多者保留差额。
    任意一方余额不足 1 则无法发动。原子结算见 points.settle_mutual。
    返回 (deducted, initiator_total, target_total, ok)。
    """
    return p.settle_mutual(initiator_id, target_id)


# ---------- 红包 ----------

def create_redpack(sender_id, total_points: int, count: int, password: str = ""):
    """创建一个红包。口令为 1~4 位数字(作为红包标识)。返回 (ok, msg, pack_id)。"""
    from . import redpack_store

    if total_points < count:
        return False, "红包总积分不能少于份数", None
    if count < 1 or count > 100:
        return False, "份数须在 1~100 之间", None
    password = str(password or "").strip()
    if not (password.isdigit() and 1 <= len(password) <= 4):
        return False, "口令需为 1~4 位数字", None
    if redpack_store.load(password) is not None:
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
    redpack_store.save(pack_id, {
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
    from . import redpack_store

    redpack_store.save(pack_id, data)
    if unclaimed > 0:
        p.add_points(data.get("sender_id"), unclaimed)
    return unclaimed


def claim_redpack(user_id, pack_id: str):
    """抢红包: 返回 (ok, amount, msg, remaining)。"""
    from . import redpack_store

    data = redpack_store.load(pack_id)
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
    redpack_store.save(pack_id, data)
    p.add_points(user_id, amount)
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
    from . import redpack_store

    available = []
    for pack_id, data in redpack_store.list_all().items():
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
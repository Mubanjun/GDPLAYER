"""掼蛋共享工具模块：牌面解析、级牌排序、牌型识别、牌型比较。"""
import os
import re
from itertools import combinations

# 牌面常量
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A']
SUITS = ['S', 'H', 'D', 'C']
_WILD_SUIT = 'H'
RANK_VAL = {r: i for i, r in enumerate(RANKS)}             # 2→0, A→12
VAL_RANK = {i: r for r, i in RANK_VAL.items()}
SUIT_ORDER = {'S': 0, 'H': 1, 'D': 2, 'C': 3}

# 炸弹等级映射（用于比较）
BOMB_TYPE_ORDER = {
    "bomb": 0, "straight_flush": 1, "quad_kings": 99,
}
_TYPE_ORDER = [
    "single", "pair", "triple", "full_house", "straight",
    "plate", "steel", "bomb", "straight_flush", "quad_kings",
]


def rank_of(card: str) -> str:
    """取牌面点数。R/B 返回自身。"""
    if card in ('R', 'B'):
        return card
    return card[1]


def suit_of(card: str) -> str:
    """取牌面花色。R/B 返回 ''。"""
    if card in ('R', 'B'):
        return ''
    return card[0]


def is_special(card: str) -> bool:
    return card in ('R', 'B')


def is_wild(card: str, level: str) -> bool:
    """判断是否为逢人配（红桃级牌）。"""
    return card == f"{_WILD_SUIT}{level}"


def make_sort_key(level: str):
    """根据级牌构造排序键函数。普通牌按点数+S<H<D<C，级牌在A之上，B<R在最后。"""

    def key(card: str) -> tuple:
        if card == 'B':
            return (2, 0, 0)
        if card == 'R':
            return (2, 1, 0)
        s = suit_of(card)
        r = rank_of(card)
        if r == level:
            return (1, 0, SUIT_ORDER.get(s, 0))
        return (0, RANK_VAL.get(r, 0), SUIT_ORDER.get(s, 0))

    return key


# ── 牌型识别 ──────────────────────────────────────────

def identify_hand_type(cards, level: str):
    """识别一手牌的 type 和比大小用的 rank。
    返回 (type_str, rank_str)，无法识别返回 (None, None)。
    """
    n = len(cards)
    if n == 0:
        return None, None

    nw = [c for c in cards if not is_wild(c, level)]
    wc = len(cards) - len(nw)
    nw_ranks = [rank_of(c) for c in nw]
    nw_suits = [suit_of(c) for c in nw]
    is_quad = sorted(cards) == ['B', 'B', 'R', 'R']

    # quad_kings
    if is_quad:
        return "quad_kings", "R"

    # 必须先确定所有非万能牌为统一合法结构
    rk_counts = {}
    for r in nw_ranks:
        rk_counts[r] = rk_counts.get(r, 0) + 1

    if n == 1:
        if wc:
            return None, None  # 逢人配不能单出
        return "single", nw_ranks[0]

    if n == 2:
        # pair: 非万能牌必须同点（或全部为万能牌）
        if wc == 0:
            if nw_ranks[0] == nw_ranks[1]:
                return "pair", nw_ranks[0]
        elif wc == 1:
            return "pair", nw_ranks[0]
        else:  # wc == 2
            # 两个逢人配可以组成任意对子 → 取级牌点数作为参考
            return "pair", nw_ranks[0] if nw_ranks else level
        return None, None

    if n == 3:
        # triple: 非万能牌必须全部同点（0-3个万能牌均可）
        if len(set(nw_ranks)) <= 1:
            rk = nw_ranks[0] if nw_ranks else level
            return "triple", rk
        return None, None

    if n == 5:
        # 优先炸弹：所有非万能牌同点 → 5 炸
        unique_ranks_5 = set(rk_counts.keys())
        if len(unique_ranks_5) == 1 and wc <= 2:
            return "bomb", list(unique_ranks_5)[0]

        # full_house or straight or straight_flush
        vals = [RANK_VAL.get(r, -1) for r in nw_ranks]
        real_suits = [s for s in nw_suits if s]

        # ── straight_flush 检查：必须先于 straight
        # 同花顺要求：所有非万能牌同花色 + (含万能牌) 能凑成 5 张连续
        if wc <= 2 and len(real_suits) >= 1 and len(set(real_suits)) == 1:
            # 找出主导花色
            suit = real_suits[0]
            suit_ranks_set = {r for s, r in zip(nw_suits, nw_ranks) if s == suit}
            suit_vals = sorted({RANK_VAL[r] for r in suit_ranks_set})
            # A-2-3-4-5 显式检测: A(→-1) + 2,3,4,5 in have
            have_ace_low = ({-1} | set(suit_vals)) if 'A' in suit_ranks_set else None
            # 从高到低迭代：优先补高位，wild 往高点补
            for start in range(8, -1, -1):
                needed = set(range(start, start + 5))
                have = set(suit_vals)
                if len(needed - have) <= wc:
                    return "straight_flush", VAL_RANK[start + 4]
                # A-2-3-4-5
                if have_ace_low and start == 0:
                    needed_ace = {-1, 0, 1, 2, 3}
                    if len(needed_ace - have_ace_low) <= wc:
                        return "straight_flush", '5'  # 最高牌=5

        # ── straight 检查
        if wc <= 2 and len(vals) >= 3:
            # 注意：如果真实牌同花，则同花顺分支已处理；这里只对"非同花"识别普通顺子
            same_suit = len(real_suits) >= 1 and len(set(real_suits)) == 1
            if not same_suit:
                have = {v for v in vals if v >= 0}
                have_ace_low = ({-1} | have) if 'A' in nw_ranks else None
                # 从高到低：优先补高位
                for start in range(8, -1, -1):
                    needed = set(range(start, start + 5))
                    if len(needed - have) <= wc:
                        return "straight", VAL_RANK[start + 4]
                    # A-2-3-4-5
                    if have_ace_low and start == 0:
                        needed_ace = {-1, 0, 1, 2, 3}
                        if len(needed_ace - have_ace_low) <= wc:
                            return "straight", '5'

        # ── full_house: 三带二
        if rk_counts:
            cnts = sorted(rk_counts.values(), reverse=True)
            if cnts[0] >= 3 - wc:
                trip_rk = max(rk_counts, key=rk_counts.get)
                trip_need = max(0, 3 - rk_counts.get(trip_rk, 0))
                # 对子部分：从非三张点中找最大的对子候选
                other_ranks = {k: v for k, v in rk_counts.items() if k != trip_rk}
                if other_ranks:
                    best_pair_rk = max(other_ranks, key=other_ranks.get)
                    pair_need = max(0, 2 - other_ranks[best_pair_rk])
                else:
                    pair_need = 2  # 全部是 trip_rk 同点
                if trip_need + pair_need <= wc:
                    return "full_house", trip_rk
        # 未能识别为上述 5 张牌型 → 继续往下（n>=4 炸弹兜底）

    if n == 6:
        # 优先炸弹：所有非万能牌同点 → 6 炸
        unique_ranks_6 = set(rk_counts.keys())
        if len(unique_ranks_6) == 1 and wc <= 2:
            return "bomb", list(unique_ranks_6)[0]

        # plate or steel
        # plate: 三个对子连续 (2,2,2)
        if rk_counts and max(rk_counts.values(), default=0) <= (2 + wc):
            # 从高到低：让万能牌优先补高位
            for start in range(10, -1, -1):
                trio = [VAL_RANK[start + i] for i in range(3)]
                need = 0
                for r in trio:
                    have = rk_counts.get(r, 0)
                    need += max(0, 2 - have)
                if need <= wc:
                    return "plate", VAL_RANK[start + 2]

        # steel: 两个连续三同张 (3,3) — 从高到低
        for start in range(11, -1, -1):
            duo = [VAL_RANK[start + i] for i in range(2)]
            need = 0
            for r in duo:
                have = rk_counts.get(r, 0)
                need += max(0, 3 - have)
            if need <= wc:
                return "steel", VAL_RANK[start + 1]
        # 未能识别为上述 6 张牌型 → 继续往下（n>=4 炸弹兜底）

    if n >= 4:
        # bomb: 所有牌同点（必须有至少 1 张真实牌，万能牌不能"全票"组炸弹）
        unique_ranks = set(rk_counts.keys())
        if len(unique_ranks) == 0:
            return None, None  # 全是万能牌
        if len(unique_ranks) == 1:
            return "bomb", list(unique_ranks)[0]
        return None, None

    return None, None


# ── 显示转换 ──────────────────────────────────────────

# ── Unicode 花色 → ASCII 映射 ──
_SUIT_LOOKUP = {
    '\u2660': 'S',  # ♠
    '\u2661': 'H',  # ♡
    '\u2662': 'D',  # ♢
    '\u2663': 'C',  # ♣
    '\u2664': 'S',  # ♤
    '\u2665': 'H',  # ♥
    '\u2666': 'D',  # ♦
    '\u2667': 'C',  # ♧
}


def card_to_display(card: str) -> str:
    """将内部牌表示转为纯文本格式。R→R B→B 花色+点数保持不变。"""
    if not card or card in ('R', 'B'):
        return card
    # 将 Unicode 花色字符转为 ASCII 防止 GBK 编码崩溃
    suit = card[0]
    if ord(suit) > 127:
        suit = _SUIT_LOOKUP.get(suit, '?')
    return suit + card[1:] if len(card) > 1 else suit


def cards_to_display(cards) -> str:
    """将牌列表转为空格分隔的纯文本字符串。"""
    if not cards:
        return 'pass'
    return ' '.join(cards)


def type_order_key(type_rank: tuple):
    """按牌型优先级排序：(type_index, bomb_size_or_rank, ...)。"""
    type_str = type_rank[0]
    if type_str == "bomb":
        return (7, 0, 0)  # 后续按张数比
    if type_str == "straight_flush":
        return (8, 0, 0)
    if type_str == "quad_kings":
        return (9, 0, 0)
    return (_TYPE_ORDER.index(type_str) if type_str in _TYPE_ORDER else 99, 0, 0)


def rank_cmp_value(rank: str, level: str, in_straight_like: bool = False) -> int:
    """返回级牌感知的比较值：越大越强。

    大小规则（项目规则）：
      - 大王 R = 100，小王 B = 99
      - **级牌** = 50+RANK_VAL['A'](=62)，仅在 单张/对子/三同张/炸弹 中抬升
      - 在 顺子/连对/钢板/同花顺 中按 **自然点数**（in_straight_like=True 时强制按自然点）
      - 其他牌 = RANK_VAL
    """
    if rank == 'R':
        return 100
    if rank == 'B':
        return 99
    if in_straight_like:
        # 顺子类：级牌按自然点处理
        return RANK_VAL.get(rank, -1)
    if rank == level:
        return 50 + RANK_VAL.get('A', 12)  # 级牌在 A 之上、王之下
    return RANK_VAL.get(rank, -1)


_STRAIGHT_LIKE_TYPES = {"straight", "plate", "steel", "straight_flush"}


def beats(my_cards, my_type, my_rank, last_cards, last_type, last_rank, level: str) -> bool:
    """判断 my_cards 是否能压过 last_cards。

    炸弹等级（项目官方）：
      4 炸 < 5 炸 < 6 炸 < 同花顺 < 7 炸 < 8 炸 < … < 四大天王
    """
    if last_cards is None or last_type is None:
        return True  # 自由出

    my_n = len(my_cards)
    last_n = len(last_cards)

    mine_is_bomb = my_type in ("bomb", "straight_flush", "quad_kings")
    theirs_is_bomb = last_type in ("bomb", "straight_flush", "quad_kings")

    # quad_kings 压一切
    if my_type == "quad_kings":
        return True
    if last_type == "quad_kings":
        return False

    # 炸弹可以压非炸弹
    if mine_is_bomb and not theirs_is_bomb:
        return True
    if theirs_is_bomb and not mine_is_bomb:
        return False

    # 双方都是炸弹 → 按炸弹等级
    if mine_is_bomb and theirs_is_bomb:
        # 同花顺 vs 普通炸弹
        # 规则：4/5/6 炸 < 同花顺 < 7/8/9... 炸
        if my_type == "straight_flush" and last_type == "bomb":
            return last_n <= 6
        if last_type == "straight_flush" and my_type == "bomb":
            return my_n >= 7
        # 同花顺 vs 同花顺 → 按最高点（同花顺按自然点，不抬升级牌）
        if my_type == "straight_flush" and last_type == "straight_flush":
            return rank_cmp_value(my_rank, level, in_straight_like=True) > \
                   rank_cmp_value(last_rank, level, in_straight_like=True)
        # 普通炸弹 vs 普通炸弹：先比张数，再比点数
        if my_n != last_n:
            return my_n > last_n
        return rank_cmp_value(my_rank, level) > rank_cmp_value(last_rank, level)

    # 非炸弹 vs 非炸弹：必须同型同张数
    if my_type != last_type:
        return False
    if my_n != last_n:
        return False

    # 顺子类按自然点；其他按级牌感知
    in_straight_like = my_type in _STRAIGHT_LIKE_TYPES
    return rank_cmp_value(my_rank, level, in_straight_like=in_straight_like) > \
           rank_cmp_value(last_rank, level, in_straight_like=in_straight_like)


def determine_last_player_role(last_player_seat, your_seat, teams):
    """根据座位和队伍信息判断上一手出牌者是队友还是对手。
    返回 "teammate" / "opponent" / None。
    """
    if last_player_seat is None or your_seat is None:
        return None
    if last_player_seat < 0 or your_seat < 0:
        return None
    if not teams or last_player_seat >= len(teams) or your_seat >= len(teams):
        return None
    if last_player_seat == your_seat:
        return None
    if teams[last_player_seat] == teams[your_seat]:
        return "teammate"
    return "opponent"


# ═══════════════════════════════════════════════════════════
#  共享工具函数：供 main.py / engine.py 共用，消除重复代码
# ═══════════════════════════════════════════════════════════

import os as _os
import json as _json

# 路径常量（由调用方在 import 后通过 set_base_dir 设置）
_BASE_DIR = _os.path.dirname(_os.path.abspath(__file__))


def set_base_dir(path: str):
    """设置项目根目录（供 main.py / engine.py 调用），自动推导各子路径。"""
    global _BASE_DIR
    _BASE_DIR = path


def get_config_file():
    return _os.path.join(_BASE_DIR, "config.cfg")


def get_temp_dir():
    return _os.path.join(_BASE_DIR, "temp")


def get_history_dir():
    return _os.path.join(_BASE_DIR, "history")


def get_logs_dir():
    return _os.path.join(get_history_dir(), "logs")


def get_stats_file():
    return _os.path.join(get_history_dir(), "stats.json")


def get_state_file():
    return _os.path.join(get_temp_dir(), "engine_state.json")


def get_log_file():
    return _os.path.join(get_temp_dir(), "engine_logs.jsonl")


def get_cmd_file():
    return _os.path.join(get_temp_dir(), "engine_cmd.json")


def validate_config_file(filepath: str) -> bool:
    """校验配置文件完整性。损坏时备份并移除，让调用方使用默认值。

    Returns:
        True if file is valid or was repaired, False if file is missing.
    """
    if not _os.path.exists(filepath):
        return False
    try:
        import re as _re
        # 文件为空 → 损坏（可能被截断），备份后移除
        if _os.path.getsize(filepath) == 0:
            _backup_and_remove(filepath)
            return True
        # 读取内容检查有效行数
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        valid_lines = _re.findall(r'\w+\s*=\s*"[^"]*"', content)
        # 少于 3 行有效配置 → 严重损坏
        if len(valid_lines) < 3:
            _backup_and_remove(filepath)
            return True
    except (OSError, UnicodeDecodeError):
        # 读取失败 → 文件损坏，备份后移除
        _backup_and_remove(filepath)
        return True
    return True


def _backup_and_remove(filepath: str):
    """备份损坏的配置文件后移除，避免后续读取继续失败。"""
    backup = filepath + ".bak"
    try:
        if _os.path.exists(backup):
            _os.remove(backup)
        _os.rename(filepath, backup)
    except OSError:
        # rename 失败则直接删除损坏文件
        try:
            _os.remove(filepath)
        except OSError:
            pass


def parse_config(filepath: str = None) -> dict:
    """解析 config.cfg，返回 {key: value} 字典。"""
    import re as _re
    if filepath is None:
        filepath = get_config_file()
    # 读取前先校验文件完整性
    validate_config_file(filepath)
    config = {}
    pattern = _re.compile(r'(\w+)\s*=\s*"([^"]*)"')
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = pattern.match(line)
                if m:
                    config[m.group(1)] = m.group(2)
    except FileNotFoundError:
        pass
    return config


def setup_dirs():
    """确保 temp/ 和 history/logs/ 目录存在。"""
    _os.makedirs(get_logs_dir(), exist_ok=True)
    _os.makedirs(get_temp_dir(), exist_ok=True)


def load_stats():
    """读取历史战绩统计。"""
    p = get_stats_file()
    if _os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return _json.load(f)
        except (json.JSONDecodeError, ValueError):
            # 文件正在写入导致读取不完整，返回默认值
            return {"total": 0, "wins": 0, "losses": 0, "games": []}
    return {"total": 0, "wins": 0, "losses": 0, "games": []}


def save_stats(stats: dict):
    """保存历史战绩统计。"""
    with open(get_stats_file(), "w", encoding="utf-8") as f:
        _json.dump(stats, f, ensure_ascii=False, indent=2)


def parse_teams_dict(raw_teams, seats: list = None) -> list:
    """将服务端返回的 teams dict 解析为座位对应的队伍列表 [t0, t1, t2, t3]。

    teams 格式可能是：
      - dict: {"team0": ["user1","user2"], "team1": ["user3","user4"]}
      - list: [0, 1, 0, 1]  → 直接返回
      - list: [0, 0, 0, 0]  → 尚未设置，用标准座位推算
    """
    if isinstance(raw_teams, list):
        # 如果全是 0 表示服务端未设置队伍信息，用标准掼蛋座位推算
        if len(raw_teams) >= 4 and all(t == 0 for t in raw_teams[:4]):
            return [0, 1, 0, 1]
        return list(raw_teams)
    if isinstance(raw_teams, dict) and seats:
        teams_list = [0, 0, 0, 0]
        matched = 0
        for team_key, members in raw_teams.items():
            team_num = int(team_key.replace("team", ""))
            for member in members:
                for idx, s in enumerate(seats[:4]):
                    if s == member:
                        teams_list[idx] = team_num
                        matched += 1
        if matched < 4:
            return [0, 1, 0, 1]  # 标准掼蛋座位 fallback
        return teams_list
    # 无法解析，用标准座位
    return [0, 1, 0, 1]

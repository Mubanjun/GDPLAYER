import json
import os

from utils import (
    RANKS, RANK_VAL,
    is_wild, rank_of, identify_hand_type, make_sort_key,
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(_BASE_DIR, "temp")
OUTPUT = os.path.join(TEMP_DIR, "markedcards.tmp")

# ═══════════════════════════════════════════════════════════
#  权重默认值（当 config.cfg 中缺少对应 key 时使用）
#  所有打分参数均从 config.cfg 读取，此处仅为 fallback
# ═══════════════════════════════════════════════════════════
_DEFAULT_WEIGHTS = {
    "W_CLEAR":             10000.0,
    "W_CONTROL":           20.0,
    "W_B":                 14.0,
    "W_WILD":              10.0,
    "W_LEVEL":             8.0,
    "W_BOMB_BASE_4":       10,
    "W_BOMB_BASE_5":       20,
    "W_BOMB_BASE_6":       35,
    "W_BOMB_BASE_7":       55,
    "W_BOMB_BASE_8":       80,
    "W_QUAD_KING":         55.0,
    "W_ROUND_PENALTY":     12.0,
    "W_SMALL_SINGLE":      5.0,
    "W_TEAMMATE_PASS":     55.0,
    "W_TEAMMATE_PLAY":     -25.0,
    "W_PLAY_BONUS":        68.0,
    "W_PASS_PENALTY":      80.0,
    "W_OPPONENT_BOMB":     18.0,
    "W_UNNECESSARY_BOMB":  -12.0,
    "W_BIG_SINGLE":        4.0,
    "W_PAIR_PENALTY":      1.0,
    "W_TIGHT_FOLLOW":      6.0,
    "W_REM_ISOLATE":       -6.0,
    "W_REM_PAIR":          8.0,
    "W_REM_TRIPLE":        12.0,
    "W_REM_4BOMB":         15,
    "W_REM_5BOMB":         28,
    "W_REM_6BOMB":         45,
    "W_REM_7BOMB":         65,
    "W_OPP_URGENCY_BONUS": 38.0,
    "W_OPP_URGENCY_PASS":  50.0,
    "W_BIG_CARD_EARLY":    -30.0,
    "W_SMALL_JOKER_EARLY": -20.0,
    "W_AGGRESSION_SHIFT":  20.0,
    "W_HAND_DECLINE_RATE": 25.0,
    "FAKE_FAME":           0,   # 神之一手模式：0=关闭, 1=开启。对方先手时等121秒跳过
}


def _load_weights_from_config():
    """从 config.cfg 加载权重，缺失的 key 用 _DEFAULT_WEIGHTS 补充并回写到文件。"""
    try:
        from utils import parse_config, get_config_file
        cfg = parse_config()
        cfg_path = get_config_file()
    except Exception:
        cfg = {}
        cfg_path = None

    merged = dict(_DEFAULT_WEIGHTS)
    missing_keys = []
    for k, default_val in _DEFAULT_WEIGHTS.items():
        raw = cfg.get(k)
        if raw is None:
            missing_keys.append(k)
            continue
        try:
            if isinstance(default_val, int):
                merged[k] = int(float(raw))
            else:
                merged[k] = float(raw)
        except (ValueError, TypeError):
            missing_keys.append(k)

    # 将缺失的 key 用默认值回写到 config.cfg
    if missing_keys and cfg_path:
        try:
            _append_missing_keys(cfg_path, missing_keys, merged)
        except Exception:
            pass  # 回写失败不影响加载

    return merged


def _append_missing_keys(cfg_path, missing_keys, merged_weights):
    """将缺失的 key 以默认值追加写入 config.cfg。"""
    # 读取现有文件内容（保留原始格式和注释）
    with open(cfg_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # 检测现有缩进风格（取最后一个 W_ 开头行的前导空格）
    indent = ""
    for line in reversed(lines):
        stripped = line.lstrip()
        if stripped.startswith("W_"):
            indent = line[:len(line) - len(stripped)]
            break

    # 收集已存在的 key（用于去重）
    existing_keys = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            existing_keys.add(key)

    # 只追加确实不在文件中的 key
    to_append = [k for k in missing_keys if k not in existing_keys]
    if not to_append:
        return

    # 确保文件以换行结尾
    content = "".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"

    # 追加缺失的 key
    content += "\n# ── 自动补填的缺失参数（默认值）──\n"
    for k in to_append:
        val = merged_weights[k]
        if isinstance(val, int):
            content += f"{indent}{k}=\"{val}\"\n"
        else:
            # 浮点数：去掉无意义的尾零
            content += f"{indent}{k}=\"{val:g}\"\n"

    # 原子写入：先写临时文件，再 os.replace 替换（与 main.py write_cmd 一致）
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, cfg_path)


# 从配置文件加载权重（模块加载时执行）
_w = _load_weights_from_config()

# ── dimension weights (loaded from config.cfg) ──────
W_CLEAR         = _w["W_CLEAR"]

# ── 控制牌保留加分 ──
W_CONTROL       = _w["W_CONTROL"]
W_B             = _w["W_B"]
W_WILD          = _w["W_WILD"]
W_LEVEL         = _w["W_LEVEL"]

# ── 炸弹结构 ──
W_BOMB_BASE     = {4: _w["W_BOMB_BASE_4"], 5: _w["W_BOMB_BASE_5"],
                   6: _w["W_BOMB_BASE_6"], 7: _w["W_BOMB_BASE_7"],
                   8: _w["W_BOMB_BASE_8"]}
W_QUAD_KING     = _w["W_QUAD_KING"]

# ── 回合效率 ──
W_ROUND_PENALTY = _w["W_ROUND_PENALTY"]
W_SMALL_SINGLE  = _w["W_SMALL_SINGLE"]

# ── 队友配合 ──
W_TEAMMATE_PASS = _w["W_TEAMMATE_PASS"]
W_TEAMMATE_PLAY = _w["W_TEAMMATE_PLAY"]

# ── 对手出牌回应 ──
W_PLAY_BONUS    = _w["W_PLAY_BONUS"]
W_PASS_PENALTY  = _w["W_PASS_PENALTY"]

# ── 炸弹策略 ──
W_OPPONENT_BOMB = _w["W_OPPONENT_BOMB"]
W_UNNECESSARY_BOMB = _w["W_UNNECESSARY_BOMB"]

# ── 领出策略 ──
W_BIG_SINGLE    = _w["W_BIG_SINGLE"]

# ── 跟牌紧贴度 ──
W_PAIR_PENALTY  = _w["W_PAIR_PENALTY"]
W_TIGHT_FOLLOW  = _w["W_TIGHT_FOLLOW"]

# ── 出牌后控制力（剩余手牌结构）──
W_REM_ISOLATE   = _w["W_REM_ISOLATE"]
W_REM_PAIR      = _w["W_REM_PAIR"]
W_REM_TRIPLE    = _w["W_REM_TRIPLE"]
W_REM_BOMB      = {4: _w["W_REM_4BOMB"], 5: _w["W_REM_5BOMB"],
                   6: _w["W_REM_6BOMB"], 7: _w["W_REM_7BOMB"]}

# ── 对手紧迫度 ──
W_OPP_URGENCY_BONUS = _w["W_OPP_URGENCY_BONUS"]
W_OPP_URGENCY_PASS  = _w["W_OPP_URGENCY_PASS"]

# ── 大牌使用时机 ──
W_BIG_CARD_EARLY   = _w["W_BIG_CARD_EARLY"]
W_SMALL_JOKER_EARLY = _w["W_SMALL_JOKER_EARLY"]

# ── v5 新增因子（基于多源统计分析：Pearson/Spearman + FDR + 逻辑回归 + RF/GBDT）──
#   aggression_shift: 后半程更积极出牌有助于收尾（统计显著）
#   hand_decline_rate: 手牌下降快表示出牌效率高（ML排名#12, 统计显著）
W_AGGRESSION_SHIFT  = _w["W_AGGRESSION_SHIFT"]
W_HAND_DECLINE_RATE = _w["W_HAND_DECLINE_RATE"]


def is_special_card(card: str) -> bool:
    return card in ('R', 'B')


def _remove_cards(hand, play, level):
    """Return remaining hand after removing play cards.
    
    移除策略：1)精确匹配 2)同点匹配 3)逢人配替代
    第2步使用 removed_indices 防止误删其他牌。
    """
    remaining = list(hand)
    removed_idx = set()
    
    for c in play:
        found = False
        # 1) 精确匹配（优先，保留花色信息）
        for i, rc in enumerate(remaining):
            if i in removed_idx:
                continue
            if rc == c:
                removed_idx.add(i)
                found = True
                break
        
        if found:
            continue
        
        # 2) 同点匹配（牌已被 play 的替代组合消费）
        r = rank_of(c)
        for i, rc in enumerate(remaining):
            if i in removed_idx:
                continue
            if not is_wild(rc, level) and not is_special_card(rc) and rank_of(rc) == r:
                removed_idx.add(i)
                found = True
                break
        
        if found:
            continue
        
        # 3) 逢人配替代
        for i, rc in enumerate(remaining):
            if i in removed_idx:
                continue
            if is_wild(rc, level):
                removed_idx.add(i)
                found = True
                break
    
    # 按索引从大到小排序后删除（避免下标偏移）
    for i in sorted(removed_idx, reverse=True):
        if i < len(remaining):
            remaining.pop(i)
    return remaining


def _estimate_rounds(hand, level):
    """Crude estimate of remaining rounds needed to clear hand."""
    if not hand:
        return 0
    n = len(hand)
    groups = {}
    wilds = 0
    r_cnt = 0
    b_cnt = 0
    for c in hand:
        if is_wild(c, level):
            wilds += 1
        elif c == 'R':
            r_cnt += 1
        elif c == 'B':
            b_cnt += 1
        else:
            r = rank_of(c)
            groups[r] = groups.get(r, 0) + 1

    bomb_savings = 0
    # quad_kings: 2R+2B 可一手清 4 张
    if r_cnt >= 2 and b_cnt >= 2:
        bomb_savings += 2
    for r, cnt in groups.items():
        if cnt >= 4:
            bomb_savings += 1
        elif cnt >= 3 and wilds > 0:
            bomb_savings += 0.5

    base = max(1, n // 3)
    return max(1, base - int(bomb_savings))


def score_play(hand, play, level, last_play, last_player, hand_counts=None, opp_near_clear=False):
    """Score a single feasible play.  Returns (score, cards).
    
    hand_counts: [4人手牌数], 用于参考（可选）。
    opp_near_clear: 对手是否接近清牌（<=8张），由engine精确计算后传入。
    """
    play_cards = list(play) if play else []
    remaining = _remove_cards(hand, play_cards, level)

    # ── 1. one-hand clear ──
    if len(remaining) == 0:
        return (W_CLEAR, play_cards)

    # ── identify types ──
    if play_cards:
        my_type, my_rank = identify_hand_type(play_cards, level)
    else:
        my_type, my_rank = "pass", ""

    last_type = None
    last_rank = None
    if last_play:
        last_type, last_rank = identify_hand_type(last_play, level)

    score = 0.0

    # ── 对手紧迫度判断 ──
    # 直接使用engine传入的opp_near_clear标志（精确判断，排除自身/队友）

    # ── 2. 队友配合 ──
    if last_player == "teammate":
        if my_type == "pass":
            score += W_TEAMMATE_PASS  # 55.0 — 强烈鼓励 pass
        else:
            # 队友领出时跟牌 → 惩罚（除非一手清）
            score += W_TEAMMATE_PLAY  # -25.0
    elif my_type == "pass":
        # pass vs 对手或自由领出 → 惩罚
        score -= W_PASS_PENALTY  # -62.0
        # 对手接近清牌时 pass → 额外惩罚
        if opp_near_clear:
            score -= W_OPP_URGENCY_PASS  # -32.0

    # ── 2b. 出牌奖励 ──
    if my_type != "pass" and last_player != "teammate":
        score += W_PLAY_BONUS  # 56.0
        # 对手接近清牌时出牌 → 额外奖励
        if opp_near_clear and last_player == "opponent":
            score += W_OPP_URGENCY_BONUS  # 25.0

    # ── 3. remaining-hand control analysis ──
    r_cnt, b_cnt, wild_cnt, lvl_cnt = 0, 0, 0, 0
    for c in remaining:
        rk = rank_of(c)
        if c == 'R' or rk == 'R':
            r_cnt += 1
        elif c == 'B' or rk == 'B':
            b_cnt += 1
        else:
            if is_wild(c, level):
                wild_cnt += 1
            elif rk == level:
                lvl_cnt += 1

    score += r_cnt * W_CONTROL
    score += b_cnt * W_B
    score += wild_cnt * W_WILD
    score += lvl_cnt * W_LEVEL

    if r_cnt >= 2 and b_cnt >= 2:
        score += W_QUAD_KING

    # ── 4. remaining-hand structure analysis ──
    rem_groups = {}
    for c in remaining:
        r = rank_of(c)
        if r not in ('R', 'B'):
            rem_groups[r] = rem_groups.get(r, 0) + 1

    # 4b. 出牌后控制力：分析剩余手牌的牌型结构
    isolated = 0     # 孤立单牌（无同点对/三/炸弹）
    pair_count = 0   # 自然对子数
    triple_count = 0 # 自然三同张数
    for r, cnt in rem_groups.items():
        if cnt == 1:
            isolated += 1
        elif cnt == 2:
            pair_count += 1
        elif cnt == 3:
            triple_count += 1
        elif cnt >= 4:
            score += W_REM_BOMB.get(min(cnt, 7), W_REM_BOMB.get(7, 65))

    score += isolated * W_REM_ISOLATE
    score += pair_count * W_REM_PAIR
    score += triple_count * W_REM_TRIPLE

    # ── 5. round penalty ──
    est = _estimate_rounds(remaining, level)
    score -= est * W_ROUND_PENALTY

    # ── 6. small-single penalty ──
    small = sum(1 for c in remaining
                if rank_of(c) in ('2','3','4','5','6','7','8','9')
                and not is_wild(c, level) and not is_special_card(c)
                and rank_of(c) != level)
    score -= small * W_SMALL_SINGLE

    # ── 6b. v5 新因子：手牌下降效率奖励 ──
    # 出牌张数占手牌比例越高 → 清牌效率越高 → 奖励
    # 统计依据：hand_decline_rate ML排名#12, 与胜率正相关
    hand_len_before = len(hand)
    if hand_len_before > 0 and len(play_cards) > 0:
        clear_ratio = len(play_cards) / hand_len_before
        # 出牌后手牌减少比例的奖励（鼓励多出牌、快速清牌）
        score += clear_ratio * W_HAND_DECLINE_RATE

    # ── 6c. v5 新因子：攻击性时序偏移 ──
    # 后半程（手牌<=13张，约一半）更积极出牌 → 奖励
    # 统计依据：aggression_shift 统计显著，胜局后半程出牌率更高
    # 动态比例：手牌越少奖励越大（1张→满额，13张→最小），不再使用硬编码 0.5
    if hand_len_before > 0 and hand_len_before <= 13 and my_type != "pass":
        aggression_ratio = (14 - hand_len_before) / 13.0
        score += aggression_ratio * W_AGGRESSION_SHIFT

    # ── 7. play-quality adjustments ──
    if my_type == "bomb":
        bomb_size = len(play_cards)
        bomb_base = W_BOMB_BASE.get(min(bomb_size, 8), W_BOMB_BASE.get(8, 80))
        if last_player == "opponent":
            score += bomb_base  # 按炸弹大小给基础分
            if bomb_size >= 6:
                score += W_OPPONENT_BOMB  # 大炸弹额外奖励
            else:
                score += W_OPPONENT_BOMB * 0.4  # 小炸弹奖励折半
        else:
            score += W_UNNECESSARY_BOMB  # -12.0
    elif my_type == "straight_flush":
        # 同花顺：仅对手出牌时给奖励，否则惩罚
        if last_player == "opponent":
            score += W_OPPONENT_BOMB * 1.2
        else:
            score += W_UNNECESSARY_BOMB  # -12.0
    elif my_type == "quad_kings":
        # 四大天王仅在对手出牌时使用
        if last_player == "opponent":
            score += 25
        else:
            score -= 80  # 大幅惩罚非必要使用
    elif my_type == "single" and my_rank and last_player is None:
        # 自由出牌时，出小单牌得到奖励（不浪费大牌）
        if my_rank == level:
            # 出级牌单 → 严重惩罚（级牌应留作对子/三同张/炸弹中抬升）
            score -= (50 + RANK_VAL.get('A', 12)) * W_BIG_SINGLE
        else:
            rv = RANK_VAL.get(my_rank, 0)
            if rv <= 5:  # 2-7 的小牌
                score += W_BIG_SINGLE * 4  # 加强小牌清理奖励
            elif rv <= 9:  # 8-T 的中牌
                score += W_BIG_SINGLE * 1  # 轻微奖励
            else:
                score -= rv * W_BIG_SINGLE  # 大牌惩罚加大

    # ── 7b. 大牌使用时机（借鉴"炸快不炸慢"原则）──
    # 用大王/小王跟单牌时，如果上家牌很小且对手不紧迫 → 额外惩罚（防止大牌过早消耗）
    if my_type == "single" and last_player == "opponent" and last_type == "single":
        my_rk = rank_of(play_cards[0]) if play_cards else ''
        last_v = RANK_VAL.get(last_rank, 0) if last_rank and last_rank not in ('R', 'B') else 0
        if my_rk == 'R' and last_v <= 9 and not opp_near_clear:
            score += W_BIG_CARD_EARLY  # -30.0 大王跟小牌惩罚
        elif my_rk == 'B' and last_v <= 9 and not opp_near_clear:
            score += W_SMALL_JOKER_EARLY  # -20.0 小王跟小牌惩罚

    # ── 8. 跟牌紧贴度（对手出牌时）──
    if last_player == "opponent" and my_type == last_type and my_type not in ("bomb","straight_flush","quad_kings","pass"):
        from utils import rank_cmp_value
        my_v = rank_cmp_value(my_rank, level)
        last_v = rank_cmp_value(last_rank, level)
        margin = my_v - last_v
        if margin <= 2 and margin > 0:
            score += W_TIGHT_FOLLOW  # 10.0 — 紧贴压牌独立奖励
        elif margin <= 5 and margin > 2:
            score -= W_PAIR_PENALTY * 0.5  # 中等跟牌轻微惩罚（v3: 负局中等跟牌27.8% vs 胜局19.8%）
        elif margin > 5:
            score -= margin * W_PAIR_PENALTY  # 松散跟牌惩罚（v3降低: 胜局松散跟牌更多, 抢控制权）

    return (score, play_cards)


def score_all(hand, feasible_plays, level, last_play, last_player, hand_counts=None, opp_near_clear=False):
    """Score and sort all feasible plays."""
    scored = [score_play(hand, p, level, last_play, last_player, hand_counts, opp_near_clear) for p in feasible_plays]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def run_scorer():
    """Read response.tmp / feasiblecards.tmp, score, write markedcards.tmp."""
    resp_path = os.path.join(TEMP_DIR, "response.tmp")
    feas_path = os.path.join(TEMP_DIR, "feasiblecards.tmp")

    with open(resp_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    questions = data.get("questions", [data])

    with open(feas_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = content.split("---\n")
    if len(blocks) < len(questions):
        blocks = [content] * len(questions)

    all_marked = []
    with open(OUTPUT, "w", encoding="utf-8") as out:
        for idx, q in enumerate(questions):
            level  = q.get("level", "")
            hand   = q.get("hand", [])
            lp     = q.get("last_play", [])
            lpl    = q.get("last_player")

            lines = blocks[idx].strip().split("\n") if idx < len(blocks) else []
            feasible = [json.loads(ln) for ln in lines if ln.strip()]

            scored = score_all(hand, feasible, level, lp, lpl)
            all_marked.append(scored)

            for s, cds in scored:
                out.write(json.dumps({"score": round(s, 2), "play": cds}, ensure_ascii=False) + "\n")
            if idx < len(questions) - 1:
                out.write("---\n")

    total = sum(len(m) for m in all_marked)
    print(f"markedcards.tmp written, {total} entries")
    return all_marked


if __name__ == "__main__":
    run_scorer()

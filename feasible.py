"""可行出牌生成模块：给定手牌和局面，枚举所有可能的合法出牌（含 pass）。"""
import json
import os
from collections import defaultdict
from itertools import combinations

from utils import (
    RANKS, SUITS, RANK_VAL, VAL_RANK, SUIT_ORDER,
    is_wild, rank_of, suit_of, is_special,
    identify_hand_type, beats,
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(_BASE_DIR, "temp")
OUTPUT = os.path.join(TEMP_DIR, "feasiblecards.tmp")


def _group_by_rank(hand, level):
    """按点数分组，返回 {rank: [真实牌列表]}, wild_cards列表, specials列表(R,B)."""
    groups = defaultdict(list)
    wilds = []
    specials = []
    for c in hand:
        if is_wild(c, level):
            wilds.append(c)
        elif c in ('R', 'B'):
            specials.append(c)
        else:
            r = rank_of(c)
            groups[r].append(c)
    return groups, wilds, specials


def _gen_by_rank(groups, wilds, specials, level):
    """枚举所有可能的合法牌型（含 pass），返回 (牌面列表, type, rank) 三元组。

    重要：只读不写传入的 groups / wilds / specials，避免污染调用者。
    """
    all_results = []

    # pass
    all_results.append(([], "pass", ""))

    # 所有真实牌 + 特殊牌列表（拷贝）
    real_cards = []
    for r in RANKS:
        real_cards.extend(groups[r])
    real_cards.extend(specials)
    total_cards = len(real_cards) + len(wilds)

    # ── single ──
    for c in real_cards:
        if not is_wild(c, level):
            t, rk = identify_hand_type([c], level)
            if t:
                all_results.append(([c], t, rk))

    # ── pair ──
    for r in RANKS:
        have = len(groups[r])
        need = 2 - have
        if 0 <= need <= len(wilds) and have >= 1:
            cards = list(groups[r][:min(have, 2)])
            cards.extend(wilds[:need])
            t, rk = identify_hand_type(cards, level)
            if t:
                all_results.append((cards, t, rk))

    # ── triple ──
    for r in RANKS:
        have = len(groups[r])
        need = 3 - have
        if 0 <= need <= len(wilds) and have >= 1:
            cards = list(groups[r][:min(have, 3)])
            cards.extend(wilds[:need])
            t, rk = identify_hand_type(cards, level)
            if t:
                all_results.append((cards, t, rk))

    # ── full_house (5): 三带二 ──
    for trip_r in RANKS:
        trip_have = len(groups[trip_r])
        trip_need = max(0, 3 - trip_have)
        if trip_need > len(wilds) or trip_have < 1:
            continue
        for pair_r in RANKS:
            if pair_r == trip_r:
                continue
            pair_have = len(groups[pair_r])
            pair_need = max(0, 2 - pair_have)
            if pair_need + trip_need <= len(wilds) and (pair_have >= 1 or pair_need == 2):
                cards = list(groups[trip_r][:min(trip_have, 3)])
                cards.extend(groups[pair_r][:min(pair_have, 2)])
                cards.extend(wilds[:trip_need + pair_need])
                t, rk = identify_hand_type(cards, level)
                if t:
                    all_results.append((cards, t, rk))

    # ── straight (5): 5 张连续单牌 (高到低 + A 可作 min) ──
    for start in range(8, -1, -1):
        needed_ranks = [VAL_RANK[start + i] for i in range(5)]
        need = sum(max(0, 1 - len(groups[r])) for r in needed_ranks)
        if need <= len(wilds):
            cards = []
            w_used = 0
            for r in needed_ranks:
                if groups[r]:
                    cards.append(groups[r][0])
                else:
                    cards.append(wilds[w_used])
                    w_used += 1
            t, rk = identify_hand_type(cards, level)
            if t:
                all_results.append((cards, t, rk))
    # A-2-3-4-5 (A 作最小)
    if groups.get('A') and len(groups['A']) >= 1:
        needed_ranks = ['A', '2', '3', '4', '5']
        need = sum(max(0, 1 - len(groups.get(r, []))) for r in needed_ranks)
        if need <= len(wilds):
            cards = []
            w_used = 0
            for r in needed_ranks:
                if groups.get(r):
                    cards.append(groups[r][0])
                else:
                    cards.append(wilds[w_used])
                    w_used += 1
            t, rk = identify_hand_type(cards, level)
            if t:
                all_results.append((cards, t, rk))

    # ── straight_flush (5, 同花) (高到低 + A 可作 min) ──
    for suit in SUITS:
        suit_ranks = {}
        for r in RANKS:
            for c in groups[r]:
                if suit_of(c) == suit:
                    suit_ranks.setdefault(r, []).append(c)
        if not suit_ranks:
            continue
        for start in range(8, -1, -1):
            needed_ranks = [VAL_RANK[start + i] for i in range(5)]
            need = sum(max(0, 1 - len(suit_ranks.get(r, []))) for r in needed_ranks)
            if need <= len(wilds):
                cards = []
                w_used = 0
                for r in needed_ranks:
                    if suit_ranks.get(r):
                        cards.append(suit_ranks[r][0])
                    else:
                        cards.append(wilds[w_used])
                        w_used += 1
                t, rk = identify_hand_type(cards, level)
                if t:
                    all_results.append((cards, t, rk))
        # A-2-3-4-5 同花
        if suit_ranks.get('A'):
            needed_ranks = ['A', '2', '3', '4', '5']
            need = sum(max(0, 1 - len(suit_ranks.get(r, []))) for r in needed_ranks)
            if need <= len(wilds):
                cards = []
                w_used = 0
                for r in needed_ranks:
                    if suit_ranks.get(r):
                        cards.append(suit_ranks[r][0])
                    else:
                        cards.append(wilds[w_used])
                        w_used += 1
                t, rk = identify_hand_type(cards, level)
                if t:
                    all_results.append((cards, t, rk))

    # ── plate (6): 三对连续 (高到低) ──
    for start in range(10, -1, -1):
        trio = [VAL_RANK[start + i] for i in range(3)]
        need = sum(max(0, 2 - len(groups[r])) for r in trio)
        if need <= len(wilds):
            cards = []
            w_used = 0
            for r in trio:
                have = min(len(groups[r]), 2)
                cards.extend(groups[r][:have])
                for _ in range(2 - have):
                    cards.append(wilds[w_used])
                    w_used += 1
            t, rk = identify_hand_type(cards, level)
            if t:
                all_results.append((cards, t, rk))

    # ── steel (6): 两个连续三同张 (高到低) ──
    for start in range(11, -1, -1):
        duo = [VAL_RANK[start + i] for i in range(2)]
        need = sum(max(0, 3 - len(groups[r])) for r in duo)
        if need <= len(wilds):
            cards = []
            w_used = 0
            for r in duo:
                have = min(len(groups[r]), 3)
                cards.extend(groups[r][:have])
                for _ in range(3 - have):
                    cards.append(wilds[w_used])
                    w_used += 1
            t, rk = identify_hand_type(cards, level)
            if t:
                all_results.append((cards, t, rk))

    # ── bomb (4+): 至少 1 张真实牌 ──
    for r in RANKS:
        have = len(groups[r])
        if have < 1:
            continue
        # 4..(have + 万能) 张炸弹；总张数不超过手上所有牌
        max_size = min(have + len(wilds), total_cards)
        for size in range(4, max_size + 1):
            need = size - have
            if 0 <= need <= len(wilds):
                cards = list(groups[r][:min(have, size)])
                cards.extend(wilds[:need])
                t, rk = identify_hand_type(cards, level)
                if t:
                    all_results.append((cards, t, rk))

    # ── quad_kings: 2R + 2B ──
    r_cnt = sum(1 for c in specials if c == 'R')
    b_cnt = sum(1 for c in specials if c == 'B')
    if r_cnt >= 2 and b_cnt >= 2:
        cards = ['R', 'R', 'B', 'B']
        t, rk = identify_hand_type(cards, level)
        if t:
            all_results.append((cards, t, rk))

    return all_results


def generate_feasible(hand, level, last_play, last_player):
    """返回所有可行出牌列表。每项为牌面字符串列表。"""
    # _gen_by_rank 只读不写传入参数，无需 deepcopy
    groups, wilds, specials = _group_by_rank(hand, level)
    wilds = list(wilds)
    specials = list(specials)

    last_type, last_rank = None, None
    if last_play:
        last_type, last_rank = identify_hand_type(last_play, level)

    is_free = (last_play is None or len(last_play) == 0)
    all_plays = _gen_by_rank(groups, wilds, specials, level)

    results = []
    seen = set()
    for cards, typ, rk in all_plays:
        key = tuple(sorted(cards))
        if key in seen:
            continue
        seen.add(key)

        if is_free:
            if cards:  # 自由出牌不能 pass
                results.append(cards)
        else:
            if not cards:
                results.append(cards)  # pass
            elif beats(cards, typ, rk, last_play, last_type, last_rank, level):
                results.append(cards)

    return results


def run_feasible(input_file=None):
    """读取 response.tmp，生成可行牌并写入 feasiblecards.tmp。"""
    if input_file is None:
        input_file = os.path.join(TEMP_DIR, "response.tmp")

    with open(input_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    questions = data.get("questions", [data])

    all_feasible = []
    for q in questions:
        level = q.get("level", "")
        hand = q.get("hand", [])
        last_play = q.get("last_play", [])
        last_player = q.get("last_player")
        feasible = generate_feasible(hand, level, last_play, last_player)
        all_feasible.append(feasible)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        for i, fea in enumerate(all_feasible):
            for cards in fea:
                f.write(json.dumps(cards, ensure_ascii=False) + "\n")
            if i < len(all_feasible) - 1:
                f.write("---\n")

    print(f"feasiblecards.tmp written, {sum(len(f) for f in all_feasible)} plays")
    return all_feasible


if __name__ == "__main__":
    run_feasible()

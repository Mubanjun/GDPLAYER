"""Step 10: 独立游戏引擎进程 — 与 Flask 前端通过文件分离，GIL 隔离。
运行方式：python engine.py
共享文件协议：
  temp/engine_state.json   ← 引擎写入完整状态（原子写入）
  temp/engine_logs.jsonl   ← 引擎追加日志（JSONL 流式）
  temp/engine_cmd.json     → Flask 写入控制命令
"""
import json
import os
import random
import string
import sys
import time
import traceback
from datetime import datetime

import requests

from feasible import generate_feasible
from scorer import score_all
from utils import (
    make_sort_key, rank_of, is_wild, is_special, identify_hand_type, rank_cmp_value,
    cards_to_display, determine_last_player_role, parse_config, setup_dirs,
    load_stats, save_stats, parse_teams_dict,
    get_config_file, get_temp_dir, get_logs_dir, get_stats_file,
    get_state_file, get_log_file, get_cmd_file,
)


# 路径由 utils.get_* 函数统一管理，避免与 main.py 重复定义

# 日志缓冲区（批量写盘，减少 IO）
_log_buf = []
_log_seq = 0
_LOG_FLUSH_INTERVAL = 1   # 每条日志立即 flush
_LOG_FLUSH_SECS = 0.0       # 无延迟
_last_flush_time = 0.0


def _raw_log(entry: dict):
    global _log_buf, _log_seq, _last_flush_time
    _log_seq += 1
    _log_buf.append(json.dumps(entry, ensure_ascii=False))
    now = time.time()
    if len(_log_buf) >= _LOG_FLUSH_INTERVAL or (now - _last_flush_time) >= _LOG_FLUSH_SECS:
        _flush_logs()
        _last_flush_time = now


def _flush_logs():
    global _log_buf
    if not _log_buf:
        return
    try:
        os.makedirs(get_temp_dir(), exist_ok=True)
        with open(get_log_file(), "a", encoding="utf-8") as f:
            for line in _log_buf:
                f.write(line + "\n")
        _log_buf.clear()
    except Exception as e:
        print(f"[ENGINE] FATAL: flush_logs failed: {e}", flush=True)
        # 不清空 buffer，下次重试
        if len(_log_buf) > 500:
            print(f"[ENGINE] WARNING: log buffer overflow ({len(_log_buf)}), discarding oldest entries", flush=True)
            _log_buf = _log_buf[-200:]  # 防内存泄漏


def e_log(msg: str, move: int = 0):
    """引擎日志 — 追加到 JSONL 文件。"""
    entry = {"t": datetime.now().strftime("%H:%M:%S"), "m": move, "msg": msg}
    _raw_log(entry)
    # 同时打印到 stdout 方便调试（Windows GBK 换行符安全处理）
    try:
        safe = str(entry).encode('utf-8', errors='replace').decode('utf-8', errors='replace')
        print(f"  [{entry['t']}] {msg}", flush=True)
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass


def write_state(s: dict):
    """原子写入引擎状态文件。"""
    try:
        state_file = get_state_file()
        os.makedirs(get_temp_dir(), exist_ok=True)
        s["engine_alive"] = True
        s["engine_pid"] = os.getpid()
        s["engine_ts"] = time.time()  # 时间戳用于心跳检测
        tmp = state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
        os.replace(tmp, state_file)
    except Exception as e:
        print(f"[ENGINE] FATAL: write_state failed: {e}", flush=True)


def read_cmd() -> dict:
    """读取 Flask 发来的控制命令（如果存在）。返回 {'cmd':'none'} 表示无命令。"""
    cmd_file = get_cmd_file()
    if not os.path.exists(cmd_file):
        return {"cmd": "none"}
    try:
        with open(cmd_file, "r", encoding="utf-8") as f:
            cmd = json.load(f)
        cmd["_read_ts"] = cmd.get("ts", 0)  # 记录读取时的 ts 用于后续安全清除
        return cmd
    except Exception:
        return {"cmd": "none"}


def clear_cmd(expected_ts: float = None):
    """消费命令后安全清除。若传入 expected_ts，则仅当文件时间戳匹配时才清除，防止竞态。"""
    cmd_file = get_cmd_file()
    try:
        if not os.path.exists(cmd_file):
            return
        if expected_ts is not None:
            with open(cmd_file, "r", encoding="utf-8") as f:
                current = json.load(f)
            if abs(current.get("ts", 0) - expected_ts) > 0.001:
                return  # 命令已被覆盖，不删除
        os.remove(cmd_file)
    except Exception:
        pass


def _rsa_encrypt(password: str, rsa_e_str: str, rsa_n_str: str) -> str:
    if not rsa_e_str or not rsa_n_str:
        return ""
    try:
        e = int(rsa_e_str)
        n = int(rsa_n_str)
    except (ValueError, TypeError):
        return ""
    random_part = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    plaintext = f"{password}\0{random_part}"
    plaintext_bytes = plaintext.encode("utf-8")
    password_int = int.from_bytes(plaintext_bytes, "big")
    if password_int >= n:
        password_int = password_int % (n - 1)
    encrypted_int = pow(password_int, e, n)
    return format(encrypted_int, "x")


def _build_auth_params(user: str, rsa_pass: str) -> str:
    return f"?user={user}&password={rsa_pass}"


def get_weights(config: dict) -> dict:
    # 默认值统一引用 scorer._DEFAULT_WEIGHTS，避免两处硬编码不一致
    import scorer as _sc
    _d = _sc._DEFAULT_WEIGHTS
    return {
        "W_CLEAR":             float(config.get("W_CLEAR", _d["W_CLEAR"])),
        "W_CONTROL":           float(config.get("W_CONTROL", _d["W_CONTROL"])),
        "W_B":                 float(config.get("W_B", _d["W_B"])),
        "W_WILD":              float(config.get("W_WILD", _d["W_WILD"])),
        "W_LEVEL":             float(config.get("W_LEVEL", _d["W_LEVEL"])),
        "W_BOMB_BASE": {
            4: int(float(config.get("W_BOMB_BASE_4", _d["W_BOMB_BASE_4"]))),
            5: int(float(config.get("W_BOMB_BASE_5", _d["W_BOMB_BASE_5"]))),
            6: int(float(config.get("W_BOMB_BASE_6", _d["W_BOMB_BASE_6"]))),
            7: int(float(config.get("W_BOMB_BASE_7", _d["W_BOMB_BASE_7"]))),
            8: int(float(config.get("W_BOMB_BASE_8", _d["W_BOMB_BASE_8"]))),
        },
        "W_QUAD_KING":         float(config.get("W_QUAD_KING", _d["W_QUAD_KING"])),
        "W_ROUND_PENALTY":     float(config.get("W_ROUND_PENALTY", _d["W_ROUND_PENALTY"])),
        "W_SMALL_SINGLE":      float(config.get("W_SMALL_SINGLE", _d["W_SMALL_SINGLE"])),
        "W_TEAMMATE_PASS":     float(config.get("W_TEAMMATE_PASS", _d["W_TEAMMATE_PASS"])),
        "W_TEAMMATE_PLAY":     float(config.get("W_TEAMMATE_PLAY", _d["W_TEAMMATE_PLAY"])),
        "W_OPPONENT_BOMB":     float(config.get("W_OPPONENT_BOMB", _d["W_OPPONENT_BOMB"])),
        "W_UNNECESSARY_BOMB":  float(config.get("W_UNNECESSARY_BOMB", _d["W_UNNECESSARY_BOMB"])),
        "W_BIG_SINGLE":        float(config.get("W_BIG_SINGLE", _d["W_BIG_SINGLE"])),
        "W_PAIR_PENALTY":      float(config.get("W_PAIR_PENALTY", _d["W_PAIR_PENALTY"])),
        "W_TIGHT_FOLLOW":      float(config.get("W_TIGHT_FOLLOW", _d["W_TIGHT_FOLLOW"])),
        "W_PLAY_BONUS":        float(config.get("W_PLAY_BONUS", _d["W_PLAY_BONUS"])),
        "W_PASS_PENALTY":      float(config.get("W_PASS_PENALTY", _d["W_PASS_PENALTY"])),
        "W_REM_ISOLATE":       float(config.get("W_REM_ISOLATE", _d["W_REM_ISOLATE"])),
        "W_REM_PAIR":          float(config.get("W_REM_PAIR", _d["W_REM_PAIR"])),
        "W_REM_TRIPLE":        float(config.get("W_REM_TRIPLE", _d["W_REM_TRIPLE"])),
        "W_REM_BOMB": {
            4: int(float(config.get("W_REM_4BOMB", _d["W_REM_4BOMB"]))),
            5: int(float(config.get("W_REM_5BOMB", _d["W_REM_5BOMB"]))),
            6: int(float(config.get("W_REM_6BOMB", _d["W_REM_6BOMB"]))),
            7: int(float(config.get("W_REM_7BOMB", _d["W_REM_7BOMB"]))),
        },
        "W_OPP_URGENCY_BONUS": float(config.get("W_OPP_URGENCY_BONUS", _d["W_OPP_URGENCY_BONUS"])),
        "W_OPP_URGENCY_PASS":  float(config.get("W_OPP_URGENCY_PASS", _d["W_OPP_URGENCY_PASS"])),
        "W_BIG_CARD_EARLY":    float(config.get("W_BIG_CARD_EARLY", _d["W_BIG_CARD_EARLY"])),
        "W_SMALL_JOKER_EARLY": float(config.get("W_SMALL_JOKER_EARLY", _d["W_SMALL_JOKER_EARLY"])),
        "W_AGGRESSION_SHIFT":  float(config.get("W_AGGRESSION_SHIFT", _d["W_AGGRESSION_SHIFT"])),
        "W_HAND_DECLINE_RATE": float(config.get("W_HAND_DECLINE_RATE", _d["W_HAND_DECLINE_RATE"])),
    }


def update_scorer_weights(config: dict):
    import scorer
    w = get_weights(config)
    scorer.W_CLEAR           = w["W_CLEAR"]
    scorer.W_CONTROL         = w["W_CONTROL"]
    scorer.W_B               = w["W_B"]
    scorer.W_WILD            = w["W_WILD"]
    scorer.W_LEVEL           = w["W_LEVEL"]
    scorer.W_BOMB_BASE       = w["W_BOMB_BASE"]
    scorer.W_QUAD_KING       = w["W_QUAD_KING"]
    scorer.W_ROUND_PENALTY   = w["W_ROUND_PENALTY"]
    scorer.W_SMALL_SINGLE    = w["W_SMALL_SINGLE"]
    scorer.W_TEAMMATE_PASS   = w["W_TEAMMATE_PASS"]
    scorer.W_TEAMMATE_PLAY   = w["W_TEAMMATE_PLAY"]
    scorer.W_OPPONENT_BOMB   = w["W_OPPONENT_BOMB"]
    scorer.W_UNNECESSARY_BOMB = w["W_UNNECESSARY_BOMB"]
    scorer.W_BIG_SINGLE      = w["W_BIG_SINGLE"]
    scorer.W_PAIR_PENALTY    = w["W_PAIR_PENALTY"]
    scorer.W_TIGHT_FOLLOW    = w["W_TIGHT_FOLLOW"]
    scorer.W_PLAY_BONUS      = w["W_PLAY_BONUS"]
    scorer.W_PASS_PENALTY    = w["W_PASS_PENALTY"]
    scorer.W_REM_ISOLATE     = w["W_REM_ISOLATE"]
    scorer.W_REM_PAIR        = w["W_REM_PAIR"]
    scorer.W_REM_TRIPLE      = w["W_REM_TRIPLE"]
    scorer.W_REM_BOMB        = w["W_REM_BOMB"]
    scorer.W_OPP_URGENCY_BONUS = w["W_OPP_URGENCY_BONUS"]
    scorer.W_OPP_URGENCY_PASS  = w["W_OPP_URGENCY_PASS"]
    scorer.W_AGGRESSION_SHIFT  = w["W_AGGRESSION_SHIFT"]
    scorer.W_HAND_DECLINE_RATE = w["W_HAND_DECLINE_RATE"]


def pick_by_baseline(hand, feasible, level, last_play, last_player):
    from utils import beats
    last_type = None
    last_rank = None
    if last_play:
        last_type, last_rank = identify_hand_type(last_play, level)
    is_free = (last_play is None or len(last_play) == 0)
    clears = []
    non_bombs = []
    bombs = []
    for cards in feasible:
        if not cards:
            continue
        my_type, my_rank = identify_hand_type(cards, level)
        if my_type is None:
            continue
        if len(cards) == len(hand):
            clears.append((cards, my_type, my_rank))
        elif my_type in ("bomb", "straight_flush", "quad_kings"):
            bombs.append((cards, my_type, my_rank))
        else:
            non_bombs.append((cards, my_type, my_rank))
    for cards, typ, rk in clears:
        if is_free or beats(cards, typ, rk, last_play, last_type, last_rank, level):
            return list(cards)
    if is_free:
        singles = []
        for cards in feasible:
            if len(cards) == 1 and not is_wild(cards[0], level):
                singles.append(cards)
        if singles:
            sorter = make_sort_key(level)
            singles.sort(key=lambda x: sorter(x[0]))
            return list(singles[0])
        return None
    if last_player == "teammate" and last_type:
        for cards, typ, rk in clears:
            if beats(cards, typ, rk, last_play, last_type, last_rank, level):
                return list(cards)
        return []
    if last_player == "opponent" and last_type:
        candidates = []
        for cards, typ, rk in non_bombs:
            if typ == last_type and beats(cards, typ, rk, last_play, last_type, last_rank, level):
                candidates.append((cards, rk))
        if candidates:
            candidates.sort(key=lambda x: rank_cmp_value(x[1], level))
            return list(candidates[0][0])
        return None
    return None


def derive_last_play_from_history(trick_history, your_seat, teams):
    if not trick_history:
        return None, None
    last_seat = None
    last_cards = None
    for entry in reversed(trick_history):
        if not entry or len(entry) < 2:
            continue
        seat = entry[0]
        cards = entry[1]
        if seat == your_seat:
            if cards and len(cards) > 0:
                return None, None
            continue
        if cards and len(cards) > 0:
            last_seat = seat
            last_cards = cards
            break
    if last_cards is None or last_seat is None:
        return None, None
    role = determine_last_player_role(last_seat, your_seat, teams)
    return last_cards, role


# ── 服务端错误分类 ──
_SERVER_ERROR_MAP = [
    ("cannot pass when leading",  "领出时不能pass",   "retry_other"),
    ("cannot beat",               "无法压过桌面牌",    "retry_other"),
    ("invalid",                   "无效出牌",         "retry_other"),
    ("already played",            "牌已出过",         "retry_other"),
    ("duplicate",                 "重复出牌",         "retry_other"),
    ("wrong turn",                "轮次错误",         "resync"),
    ("not your turn",             "不是你的回合",      "resync"),
    ("game over",                 "对局已结束",        "abort"),
    ("already finished",          "对局已结束",        "abort"),
    ("not found",                 "游戏不存在",        "abort"),
    ("too many",                  "出牌数量不对",      "retry_other"),
    ("card not in hand",          "牌不在手牌中",      "resync"),
]


def classify_server_error(error_msg: str) -> str:
    if not error_msg:
        return "unknown"
    em = str(error_msg).lower()
    for keyword, desc, action in _SERVER_ERROR_MAP:
        if keyword in em:
            return action
    return "unknown"


# ═══════════════════════════════════════════
#  引擎主循环
# ═══════════════════════════════════════════

def run_engine():
    """引擎主入口：轮询命令 + 执行游戏循环。"""
    config = parse_config()
    update_scorer_weights(config)
    setup_dirs()

    # 初始化状态
    state = {
        "running": False, "status": "idle", "game_id": None, "level": "",
        "your_seat": -1, "your_hand": [], "your_team": -1,
        "is_your_turn": False, "last_play": [], "last_player": -1,
        "current_turn": -1, "hand_counts": [0, 0, 0, 0],
        "ranking": [], "winner_team": -1, "completed": False,
        "trick_history": [], "seats": [], "teams": [],
        "chosen_play": [], "total_moves": 0, "error": None,
        "message": "Engine idle", "battle_active": True,
        "manual_mode": False, "manual_play": None,
    }
    write_state(state)
    e_log("[ENGINE] started, waiting for command")

    last_cmd_ts = 0
    last_heartbeat = 0.0

    while True:
        # ── 心跳机制：每5秒写一次状态让Flask知道引擎存活 ──
        now_ts = time.time()
        if now_ts - last_heartbeat > 5.0:
            last_heartbeat = now_ts
            write_state(state)

        # ── 检查命令 ──
        cmd = read_cmd()
        cmd_action = cmd.get("cmd", "none")
        cmd_ts = cmd.get("ts", 0)

        # ── 网络错误后自重启 ──
        if state.get("_auto_start"):
            state.pop("_auto_start", None)
            cmd_action = "start"
            cmd_ts = int(time.time() * 1000)

        if cmd_action == "stop" and cmd_ts > last_cmd_ts:
            last_cmd_ts = cmd_ts
            clear_cmd(cmd_ts)
            e_log("[ENGINE] received STOP command")
            state["running"] = False
            state["status"] = "idle"
            state["message"] = "Engine stopped by command"
            write_state(state)
            # 停止后等待下一个start
            time.sleep(1)
            continue

        if cmd_action == "manual_mode":
            state["manual_mode"] = cmd.get("manual_mode", False)
            e_log(f"[ENGINE] manual_mode = {state['manual_mode']}")
            clear_cmd()

        if cmd_action == "manual_play":
            state["manual_play"] = cmd.get("manual_play", None)
            e_log(f"[ENGINE] manual_play = {state['manual_play']}")
            clear_cmd()

        if cmd_action == "start" and cmd_ts > last_cmd_ts:
            last_cmd_ts = cmd_ts
            clear_cmd()
            state["manual_mode"] = cmd.get("manual_mode", False)
            e_log("[ENGINE] received START command")
            _play_one_game(config, state)
            # 对局结束后回到 idle
            state["running"] = False
            state["manual_play"] = None
            if state["status"] == "error":
                # 网络错误自动重试
                e_log("[ENGINE] error occurred, auto-restart in 10s")
                state["message"] = state.get("message", "") + " — auto-retry in 10s"
                state["status"] = "recovering"
                write_state(state)
                _flush_logs()
                time.sleep(10)
                state["_auto_start"] = True  # 内部标记：自触发重试
                state["status"] = "idle"
                state["message"] = "Auto-restarting..."
                write_state(state)
                continue
            if state["status"] == "finished":
                # 刷榜模式：游戏正常结束后自动开始下一局（可配置间隔）
                _config = parse_config()  # 重读配置以获取最新间隔
                _sleep_between = max(1, int(_config.get("sleep_between_games", "10")))
                update_scorer_weights(_config)  # 每局前刷新权重
                e_log(f"[ENGINE] game finished, auto-restart in {_sleep_between}s")
                state["running"] = True   # 标记为运行中，防止前端误判为已停止
                state["status"] = "waiting_restart"
                state["message"] = f"Game completed, next game in {_sleep_between}s"
                write_state(state)
                _flush_logs()
                # 倒计时等待，期间收到 stop 则取消自动重启
                _cancelled = False
                for i in range(_sleep_between):
                    _cmd = read_cmd()
                    if _cmd.get("cmd") == "stop":
                        clear_cmd()
                        e_log("[ENGINE] auto-restart cancelled by stop")
                        state["status"] = "idle"
                        state["message"] = "Engine stopped by command"
                        state["running"] = False
                        write_state(state)
                        _flush_logs()
                        _cancelled = True
                        break
                    # 每秒更新倒计时
                    state["message"] = f"Game completed, next game in {_sleep_between - i}s"
                    write_state(state)
                    time.sleep(1)
                if _cancelled:
                    continue
                # 等待结束，自动开始下一局
                state["_auto_start"] = True
                state["status"] = "idle"
                state["message"] = "Auto-starting next game..."
                write_state(state)
                _flush_logs()
                continue
            if state["status"] not in ("finished",):
                state["status"] = "idle"
            write_state(state)
            _flush_logs()
            continue

        if not state["running"] and state["status"] == "idle":
            # 等待命令，0.3s 轮询确保前端指令快速响应
            time.sleep(0.3)
            continue

        time.sleep(0.1)


def _play_one_game(config: dict, state: dict):
    """执行一局完整游戏循环。state 会被原地修改以反映最新状态。"""
    address = config.get("address", "")
    user = config.get("user", "")
    password = config.get("password", "")
    humans = config.get("humans", "0")
    sleep_min = int(config.get("sleep_min", "0"))
    sleep_max = int(config.get("sleep_max", "10"))
    sleep_after_play = float(config.get("sleep_after_play", "1.5"))
    sleep_on_reject = float(config.get("sleep_on_reject", "3"))

    # ── 神之一手模式：对方先手时等121秒跳过，不记录对局 ──
    _fake_fame = str(config.get("FAKE_FAME", "0")).strip() in ("1", "true", "True", "yes")
    _fake_fame_skip = False  # 标记本局是否因神之一手模式跳过
    _fake_fame_checked = False  # 确保只检测一次先手

    update_scorer_weights(config)
    setup_dirs()

    state["running"] = True
    state["status"] = "joining"
    state["message"] = "Joining game..."
    state["trick_history"] = []
    state["completed"] = False
    state["winner_team"] = -1
    state["total_moves"] = 0
    state["error"] = None
    state["consecutive_rejects"] = 0
    state["stuck_count"] = 0
    state["check_retries"] = 0
    state["chosen_play"] = []
    write_state(state)

    # RSA
    rsa_pass = config.get("password_encrypted", "").strip()
    if not rsa_pass:
        rsa_e = config.get("rsa_e", "")
        rsa_n = config.get("rsa_n", "")
        rsa_pass = _rsa_encrypt(password, rsa_e, rsa_n)
    if not rsa_pass:
        state["status"] = "error"
        state["message"] = "RSA config missing"
        state["running"] = False
        write_state(state)
        e_log("[ENGINE] RSA config missing, abort")
        return
    auth_params = _build_auth_params(user, rsa_pass)

    # Join（带命令检查，防止阻塞期间无法响应 stop）
    join_url = f"{address}/join_game?user={user}&password={rsa_pass}"
    if humans == "1":
        join_url += "&humans=1"
    e_log(f"[ENGINE] joining: {join_url[:100]}...")
    
    join_result = None
    join_error = None
    join_attempts = 6  # 最多6次，每次20s = 最多120s
    for jn in range(join_attempts):
        # 检查是否有 stop 命令
        cmd = read_cmd()
        if cmd.get("cmd") == "stop":
            state["status"] = "idle"
            state["message"] = "Join cancelled by stop"
            state["running"] = False
            write_state(state)
            clear_cmd()
            e_log("[ENGINE] join cancelled by stop")
            return
        try:
            resp = requests.post(join_url, timeout=20)
            resp.raise_for_status()
            join_result = resp.json()
            break
        except Exception as e:
            join_error = e
            if jn < join_attempts - 1:
                e_log(f"[ENGINE] join attempt {jn+1} failed: {e}, retrying...")
                state["message"] = f"Joining... (attempt {jn+1}/{join_attempts})"
                write_state(state)
                time.sleep(2)
    
    if join_result is None:
        state["status"] = "error"
        state["message"] = f"Join failed after {join_attempts} attempts: {join_error}"
        state["running"] = False
        write_state(state)
        e_log(f"[ENGINE] join failed: {join_error}")
        return

    e_log(f"[ENGINE] join result: {str(join_result)[:200]}")

    game_id = join_result.get("game_id")
    if not game_id:
        state["status"] = "error"
        state["message"] = f"Join returned no game_id: {join_result}"
        state["running"] = False
        write_state(state)
        e_log(f"[ENGINE] no game_id in join response")
        return

    state["game_id"] = game_id
    state["status"] = "playing"
    state["message"] = f"Game {game_id} started"
    write_state(state)

    check_url = f"{address}/check_game/{game_id}/{auth_params}"
    play_base = f"{address}/play_game/{game_id}/"
    play_auth_params = {"user": user, "password": rsa_pass}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    move_log = []
    turn_played = set()
    _last_turn_key = None

    pending_name = f"PENDING_game{game_id}_{timestamp}.json"
    pending_path = os.path.join(get_logs_dir(), pending_name)

    def flush_game_log():
        log_data = {
            "timestamp": timestamp, "game_id": game_id, "user": user,
            "address": address, "is_win": None,
            "moves": list(move_log), "final_state": dict(state),
        }
        try:
            tmp = pending_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(log_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, pending_path)
        except Exception:
            pass

    max_moves = 500
    game_timeout_minutes = int(config.get("game_timeout_minutes", "30"))
    game_timeout_seconds = game_timeout_minutes * 60
    loop_start_time = time.time()

    e_log(f"[GAME] game {game_id} loop started, timeout={game_timeout_minutes}min")
    while True:
        # ── 统一命令处理：所有控制命令在此处消费，避免漏处理 ──
        cmd = read_cmd()
        cmd_action = cmd.get("cmd", "none")

        if cmd_action == "stop":
            clear_cmd()
            e_log("[GAME] received STOP during play")
            state["running"] = False
            state["status"] = "idle"
            state["message"] = "Game stopped"
            write_state(state)
            return

        if cmd_action == "manual_mode":
            new_mode = cmd.get("manual_mode", False)
            state["manual_mode"] = new_mode
            if not new_mode:
                state["manual_play"] = None
            clear_cmd()
            e_log(f"[GAME] manual_mode = {new_mode}")
            write_state(state)

        if cmd_action == "manual_play" and state.get("manual_mode"):
            # 不在这里消费 manual_play，留给下方的 manual 等待循环处理
            # clear_cmd()  -- 注释掉，避免抢占消费
            pass

        elapsed = time.time() - loop_start_time
        if elapsed > game_timeout_seconds:
            state["status"] = "error"
            state["message"] = f"Game timeout after {game_timeout_minutes}min"
            write_state(state)
            e_log(f"[TIMEOUT] game exceeded {game_timeout_minutes}min")
            break

        # 检查 running 标志
        if not state["running"]:
            break

        check_retries = state.get("check_retries", 0)
        try:
            resp = requests.get(check_url, timeout=30)
            resp.raise_for_status()
            check_state = resp.json()
            state["check_retries"] = 0  # 成功则重置
        except Exception as e:
            check_retries += 1
            state["check_retries"] = check_retries
            if check_retries <= 5:
                wait = 5
                e_log(f"[CHECK] failed #{check_retries}: {e}, retry in {wait}s")
                state["message"] = f"Check failed, retry {check_retries}/5 in {wait}s..."
                write_state(state)
                time.sleep(wait)
                continue
            state["status"] = "error"
            state["message"] = f"Check failed after {check_retries} retries: {e}"
            state["running"] = False
            write_state(state)
            e_log(f"[ENGINE] check_game failed after {check_retries} retries: {e}")
            break

        # ── 同步状态（全权信任服务端数据）──
        _completed = check_state.get("completed", False)
        _level = check_state.get("level", "")
        _seats = check_state.get("seats", [])
        raw_teams = check_state.get("teams", [])
        _teams = parse_teams_dict(raw_teams, _seats)
        _prev_teams = state.get("teams", [])
        if _teams != _prev_teams:
            e_log(f"[TEAMS] {_teams} (seats={_seats[:4]})")
        # 修复：0 是合法值（seat=0, team=0 等），不能用 `or -1`（0 在 Python 中 falsy）
        _val = check_state.get("your_seat");      _your_seat = _val if _val is not None else -1
        _your_hand = check_state.get("your_hand", [])
        _val = check_state.get("your_team");      _your_team = _val if _val is not None else -1
        _is_your_turn = check_state.get("is_your_turn", False)
        _last_play = check_state.get("last_play", [])
        _val = check_state.get("last_player");    _last_player = _val if _val is not None else -1
        _val = check_state.get("current_turn");   _current_turn = _val if _val is not None else -1
        _hand_counts = check_state.get("hand_counts", [0, 0, 0, 0])
        _ranking = check_state.get("ranking", [])
        _val = check_state.get("winner_team");    _winner_team = _val if _val is not None else -1
        _trick_history = check_state.get("trick_history", [])
        _server_started = check_state.get("started", False)  # 服务端权威 started 字段

        state.update({
            "completed": _completed, "level": _level, "seats": _seats,
            "teams": _teams, "your_seat": _your_seat, "your_hand": _your_hand,
            "your_team": _your_team, "is_your_turn": _is_your_turn,
            "last_play": _last_play, "last_player": _last_player,
            "current_turn": _current_turn, "hand_counts": _hand_counts,
            "ranking": _ranking, "winner_team": _winner_team,
            "trick_history": _trick_history,
        })

        # 全权信任服务端 started 字段
        state["started"] = _server_started
        write_state(state)

        if _completed:
            state["status"] = "finished"
            state["running"] = False
            state["message"] = "Game completed"
            write_state(state)
            e_log(f"[GAME] completed, winner_team={_winner_team}, your_team={_your_team}")
            break

        # ── 未开局检测（信任服务端 started 字段，避免首轮误判为未开局）──
        if not _server_started and (not _trick_history and all(hc == 27 for hc in _hand_counts[:4]) and not _last_play):
            stuck = state.get("stuck_count", 0) + 1
            state["stuck_count"] = stuck
            write_state(state)
            if stuck % 5 == 1 or stuck == 1:
                e_log(f"[CHECK] Waiting for game start... ({int((stuck-1) * (sleep_min+sleep_max) / 2 + 5)}s elapsed, game_id={game_id})")
            if stuck > 60:
                state["status"] = "error"
                state["message"] = "Game stuck at start phase (>5min waiting)"
                state["running"] = False
                write_state(state)
                e_log("[CHECK] stuck too long, quitting")
                break
            time.sleep(random.randint(sleep_min, sleep_max))
            continue
        else:
            state["stuck_count"] = 0

        # ── 神之一手模式：首次检测先手 ──
        # join 后首次 check 即可判断：
        #   - hand_counts 全为 27 且 current_turn == your_seat → 我方先手
        #   - hand_counts 全为 27 且 current_turn != your_seat → 对方先手
        #   - hand_counts 不全为 27（join 晚了）→ 说明不是我们开局的 → 对方先手
        if (_fake_fame and not _fake_fame_checked and _server_started
                and _current_turn >= 0 and _your_seat >= 0 and _your_team >= 0):
            _fake_fame_checked = True
            _all_full = all(hc == 27 for hc in _hand_counts[:4])
            _first_team = _teams[_current_turn] if 0 <= _current_turn < len(_teams) else -1
            _is_our_lead = (_all_full and _first_team == _your_team)
            if not _is_our_lead:
                e_log(f"[FAKE_FAME] 对方先手(turn={_current_turn},seat={_your_seat},first_team={_first_team},your_team={_your_team})，等待121秒")
                state["message"] = "神之一手 === 😯👉"
                write_state(state)
                _fake_fame_skip = True
                # 等待121秒，期间仅检查 stop 命令（不调用 check_game 以免服务端重新计时）
                for _wi in range(121):
                    _cmd = read_cmd()
                    if _cmd.get("cmd") == "stop":
                        clear_cmd()
                        e_log("[FAKE_FAME] Received stop command, stopping engine")
                        state["status"] = "idle"
                        state["message"] = "Engine stopped by command"
                        state["running"] = False
                        write_state(state)
                        return
                    state["message"] = f"神之一手：{121 - _wi}秒后恢复牌桌"
                    write_state(state)
                    time.sleep(1)
                e_log("[FAKE_FAME] ")
                break
            else:
                e_log(f"[FAKE_FAME] 我方先手(turn={_current_turn},seat={_your_seat},first_team={_first_team},your_team={_your_team})")

        # ── 死循环检测（信任服务端判定，不自行猜测 winner_team）──
        _rejects = state.get("consecutive_rejects", 0)
        if _rejects >= 8 and not _completed:
            e_log(f"[DEADLOOP] {_rejects} consecutive rejects, force restart")
            state["status"] = "error"
            state["completed"] = True
            state["running"] = False
            state["consecutive_rejects"] = 0
            write_state(state)
            break

        # ── 我方回合 ──
        if _is_your_turn:
            state["message"] = "Calculating best move..."
            write_state(state)

            hand = _your_hand
            level = _level
            last_play_server = _last_play
            last_player_seat = _last_player if _last_player is not None else -1
            your_seat = _your_seat
            trick_history = _trick_history
            hand_counts = _hand_counts
            teams = _teams

            # ── 异常状态检测 ──
            _my_hand_size = len(hand)
            _game_active = _server_started or bool(trick_history)
            _is_game_start = (not trick_history and all(hc == 27 for hc in hand_counts[:4]))
            _genuine_desync = (
                _my_hand_size == 27 and _game_active
                and not _is_game_start
                and any(0 < hc <= 5 for hc in hand_counts[:4])
            )
            if _genuine_desync:
                e_log(f"[DESYNC-PLAY] hand={_my_hand_size} counts={hand_counts[:4]}, try play")
                is_leading = (not last_play_server or len(last_play_server) == 0)
                if not is_leading and last_play_server:
                    try:
                        pp = dict(play_auth_params)
                        pp["coord"] = json.dumps([])
                        rp = requests.get(play_base, params=pp, timeout=120)
                        rp.raise_for_status()
                        pr = rp.json()
                        if pr.get("is_success"):
                            e_log("[DESYNC-PASS] pass OK")
                            state["consecutive_rejects"] = max(0, state.get("consecutive_rejects", 0) - 1)
                            write_state(state)
                            time.sleep(sleep_after_play)
                            continue
                    except Exception as pe:
                        e_log(f"[DESYNC-PASS] pass error: {pe}")

                played_ok = False
                if is_leading:
                    candidates_to_try = [sorted(hand)[:1]]
                else:
                    sorted_desc = sorted(hand, reverse=True)
                    candidates_to_try = [[c] for c in sorted_desc[:15]]
                for cand in candidates_to_try:
                    try:
                        pp = dict(play_auth_params)
                        pp["coord"] = json.dumps(cand)
                        rp = requests.get(play_base, params=pp, timeout=120)
                        rp.raise_for_status()
                        pr = rp.json()
                        if pr.get("is_success"):
                            e_log(f"[DESYNC-OK] played {cards_to_display(cand)}")
                            state["consecutive_rejects"] = max(0, state.get("consecutive_rejects", 0) - 1)
                            state["total_moves"] += 1
                            state["chosen_play"] = cand
                            write_state(state)
                            played_ok = True
                            break
                    except Exception:
                        pass
                if played_ok:
                    time.sleep(sleep_after_play)
                    continue
                e_log("[DESYNC-FAIL] no card worked")
                state["consecutive_rejects"] = state.get("consecutive_rejects", 0) + 1
                write_state(state)
                time.sleep(sleep_on_reject)
                continue

            # ── 确定对手/队友 ──
            role = determine_last_player_role(last_player_seat, your_seat, teams)
            # 领出时role为None（无上一手或自己赢了上一墩），保持None传给scorer以触发自由出牌策略
            # 仅在有上家牌但无法判断角色时按对手处理（保守）
            if role is None and last_play_server and len(last_play_server) > 0:
                role = "opponent"
            last_play = last_play_server
            if last_play and trick_history:
                th_play, th_role = derive_last_play_from_history(trick_history, your_seat, teams)
                if th_play is not None:
                    e_log(f"[VERIFY] server last_play={last_play[:5] if len(last_play)>5 else last_play}, trick={th_play[:5] if len(th_play)>5 else th_play}")
                    # 如果不一致，优先使用 trick_history 推导的 last_play（更可靠）
                    if sorted(th_play) != sorted(last_play):
                        e_log(f"[CALIBRATE] replacing last_play={last_play} with trick_history: {th_play}")
                        last_play = th_play
                        if th_role is not None:
                            role = th_role

            # 生成可行牌
            feasible = generate_feasible(hand, level, last_play, role)
            if not feasible:
                feasible = [[]]

            _hand_len = len(hand)
            if _hand_len > 24:
                feas_cap = 30
            elif _hand_len > 20:
                feas_cap = 80
            elif _hand_len > 15:
                feas_cap = 150
            else:
                feas_cap = 200
            if len(feasible) > feas_cap:
                e_log(f"[FEASIBLE] capped {len(feasible)}→{feas_cap} (hand={_hand_len})")
                feasible = feasible[:feas_cap]

            # max_moves
            if state["total_moves"] >= max_moves:
                state["status"] = "finished"
                state["running"] = False
                state["message"] = f"Max moves ({max_moves}) reached"
                write_state(state)
                break

            # 过滤已出
            turn_key = (tuple(sorted(hand)), tuple(sorted(last_play)) if last_play else ())
            if turn_key != _last_turn_key:
                turn_played.clear()
                _last_turn_key = turn_key
            if len(turn_played) > 20:
                e_log("[SAFETY] turn_played > 20, force clear")
                turn_played.clear()
            fresh_feasible = []
            for play in feasible:
                key = tuple(sorted(play)) if play else ()
                if key not in turn_played:
                    fresh_feasible.append(play)
            if not fresh_feasible:
                fresh_feasible = [[]]
            feasible = fresh_feasible

            # ── 人工模式 ──
            best_play = None
            is_manual = state.get("manual_mode")
            if is_manual:
                e_log("[MANUAL] waiting for frontend play...")
                state["manual_play"] = None
                state["message"] = "Waiting for manual play..."
                write_state(state)
                manual_timeout = 120
                manual_wait = 0
                while manual_wait < manual_timeout:
                    mp = read_cmd()
                    if mp.get("cmd") == "manual_play":
                        clear_cmd()
                        best_play = mp.get("manual_play")
                        if isinstance(best_play, list):
                            best_play = list(best_play)
                        elif best_play is not None and not isinstance(best_play, list):
                            best_play = []
                        state["manual_play"] = None
                        e_log(f"[MANUAL] submit: {'pass' if not best_play else cards_to_display(best_play)}")
                        break
                    if mp.get("cmd") == "stop":
                        clear_cmd()
                        e_log("[MANUAL] stopped during wait")
                        state["running"] = False
                        state["status"] = "idle"
                        state["message"] = "Stopped by user"
                        write_state(state)
                        return
                    # 检查 mode 变化
                    if mp.get("cmd") == "manual_mode" and not mp.get("manual_mode", True):
                        clear_cmd()
                        state["manual_mode"] = False
                        e_log("[MANUAL] mode switched to auto")
                        best_play = None
                        break
                    time.sleep(0.3)
                    manual_wait += 0.3
                else:
                    e_log("[MANUAL] timeout, auto-resume")
                    state["manual_mode"] = False
                    best_play = None

            # ── AI 决策 ──
            if state.get("manual_mode") and best_play is not None:
                pass  # 人工已出牌
            else:
                best_play = None

                # 一手清优先检查（直接执行，不走scorer）
                baseline_play = pick_by_baseline(hand, feasible, level, last_play, role)
                if baseline_play is not None and len(baseline_play) == len(hand):
                    best_play = baseline_play
                    e_log(f"[CLEAR] {cards_to_display(best_play)}")
                else:
                    # 计算对手紧迫度标志（精确判断，排除自身/队友）
                    opp_near_clear = False
                    if role == "opponent" and last_play and last_player_seat >= 0:
                        if 0 <= last_player_seat < len(hand_counts) and 0 < hand_counts[last_player_seat] <= 8:
                            opp_near_clear = True

                    # 所有其他场景 → 走scorer评分（主决策路径）
                    scored = score_all(hand, feasible, level, last_play, role, hand_counts=hand_counts, opp_near_clear=opp_near_clear)
                    best_play = scored[0][1] if scored else []

                    # 日志
                    opp_info = f" opp:{hand_counts[last_player_seat]}张" if opp_near_clear else ""
                    e_log(f"[SCORE]{opp_info} {cards_to_display(best_play) if best_play else 'pass'} (feasible={len(feasible)})")

            if best_play:
                turn_played.add(tuple(sorted(best_play)))
            if best_play:
                bt, _ = identify_hand_type(best_play, level)
                if bt is None:
                    best_play = []

            is_leading_fn = (not last_play or len(last_play) == 0)
            if not best_play:
                non_pass = [f for f in feasible if f]
                if non_pass:
                    scored_fb = score_all(hand, non_pass, level, last_play, role, hand_counts=hand_counts)
                    best_play = scored_fb[0][1] if scored_fb else (non_pass[0] if non_pass else [])
                elif not is_leading_fn and [] in feasible:
                    # 确实没有牌能打过，且非领出：pass
                    best_play = []
                    e_log("[SAFETY] nothing can beat, pass")
                else:
                    non_wild = [c for c in hand if len(c) >= 2 and not is_wild(c, level)]
                    best_play = non_wild[:1] if non_wild else (hand[:1] if hand else [])
                    e_log(f"[SAFETY] force single: {cards_to_display(best_play)}")
            if not best_play:
                best_play = []

            if is_leading_fn and not best_play:
                e_log("[DEFENSE] leading but empty best_play, rescoring")
                scored_all = score_all(hand, feasible, level, last_play, role, hand_counts=hand_counts)
                if scored_all:
                    best_play = scored_all[0][1]
                if not best_play:
                    non_wild = [c for c in hand if len(c) >= 2 and not is_wild(c, level)]
                    best_play = non_wild[:1] if non_wild else (hand[:1] if hand else [])

            state["chosen_play"] = best_play
            state["message"] = f"Playing: {best_play if best_play else 'pass'}"
            write_state(state)

            # Submit
            play_params = dict(play_auth_params)
            play_params["coord"] = json.dumps(best_play)
            play_result = {}
            play_retries = 0
            while True:
                try:
                    e_log(f"[PLAY] {'pass' if not best_play else cards_to_display(best_play)}")
                    resp2 = requests.get(play_base, params=play_params, timeout=30)
                    resp2.raise_for_status()
                    play_result = resp2.json()
                    e_log(f"[RECV] is_success={play_result.get('is_success')}, error={play_result.get('error')}")
                    break
                except Exception as e:
                    play_retries += 1
                    if play_retries <= 2:
                        wait = play_retries * 10
                        e_log(f"[PLAY-RETRY] HTTP error #{play_retries}: {e}, retry in {wait}s")
                        state["message"] = f"Play error, retry {play_retries}/2..."
                        write_state(state)
                        time.sleep(wait)
                        continue
                    state["status"] = "error"
                    state["message"] = f"Play failed after {play_retries} retries: {e}"
                    state["running"] = False
                    write_state(state)
                    e_log(f"[ERROR] play HTTP failed after {play_retries} retries: {e}")
                    break
            if not state["running"]:
                break

            is_ok = play_result.get("is_success", False)
            error_msg = str(play_result.get("error") or play_result.get("message", ""))
            action = classify_server_error(error_msg) if not is_ok else "ok"

            if action == "ok":
                state["consecutive_rejects"] = 0
                state["total_moves"] += 1
                write_state(state)
                e_log(f"[OK] play success")

            elif action == "retry_other":
                state["consecutive_rejects"] = state.get("consecutive_rejects", 0) + 1
                write_state(state)
                e_log(f"[REJECT] {error_msg} (rejects={state['consecutive_rejects']})")
                
                # ── 重新同步状态后重试 ──
                retry_ok = False
                try:
                    rs = requests.get(check_url, timeout=60)
                    if rs.status_code == 200:
                        sync_state = rs.json()
                        fresh_hand = sync_state.get("your_hand", hand)
                        fresh_level = sync_state.get("level", level)
                        fresh_last_play = sync_state.get("last_play", [])
                        fresh_last_player = sync_state.get("last_player", -1)
                        fresh_teams = sync_state.get("teams", [])
                        fresh_teams_list = parse_teams_dict(fresh_teams, sync_state.get("seats", []))
                        fresh_role = determine_last_player_role(fresh_last_player, your_seat, fresh_teams_list)
                        if fresh_role is None:
                            fresh_role = "opponent"
                        
                        # 判断当前是否领出
                        is_leading = (not fresh_last_play or len(fresh_last_play) == 0)
                        if is_leading:
                            e_log(f"[LOG-DETAIL] server says leading, generating free plays")
                            fresh_feasible = generate_feasible(fresh_hand, fresh_level, [], None)
                            fresh_feasible = [f for f in fresh_feasible if f]
                        else:
                            fresh_feasible = generate_feasible(fresh_hand, fresh_level, fresh_last_play, fresh_role)
                        
                        fresh_feasible = [f for f in fresh_feasible if f and tuple(sorted(f)) not in turn_played]
                        if not fresh_feasible:
                            fresh_feasible = [f for f in generate_feasible(fresh_hand, fresh_level, fresh_last_play, fresh_role) if f]
                        
                        if fresh_feasible:
                            scored_fb = score_all(fresh_hand, fresh_feasible, fresh_level, fresh_last_play, fresh_role, hand_counts=hand_counts)
                            for s_score, retry_play in scored_fb[:5]:
                                key = tuple(sorted(retry_play))
                                if key in turn_played:
                                    continue
                                turn_played.add(key)
                                try:
                                    rp_params = dict(play_auth_params)
                                    rp_params["coord"] = json.dumps(retry_play)
                                    rp3 = requests.get(play_base, params=rp_params, timeout=120)
                                    fb_res = rp3.json()
                                    if fb_res.get("is_success"):
                                        state["consecutive_rejects"] = 0
                                        state["total_moves"] += 1
                                        state["chosen_play"] = retry_play
                                        write_state(state)
                                        e_log(f"[RETRY-OK] {cards_to_display(retry_play)}")
                                        # 记录到 move_log
                                        move_log.append({
                                            "move": state["total_moves"], "hand_len": len(fresh_hand), "level": fresh_level,
                                            "last_play": fresh_last_play, "last_player_role": fresh_role,
                                            "feasible_count": len(fresh_feasible),
                                            "chosen_play": retry_play, "result": fb_res,
                                            "server_state": {k: sync_state.get(k) for k in
                                                ("hand_counts", "current_turn", "last_play", "last_player",
                                                 "trick_history", "ranking", "completed", "is_your_turn")},
                                            "mode": "retry",
                                        })
                                        flush_game_log()
                                        retry_ok = True
                                        break
                                except Exception:
                                    pass
                        
                        # ── 如果前面都失败，尝试 pass（非领出时） ──
                        if not retry_ok and not is_leading:
                            e_log("[RETRY-PASS] trying pass as last resort")
                            if () not in turn_played:
                                turn_played.add(())
                                try:
                                    pp = dict(play_auth_params)
                                    pp["coord"] = json.dumps([])
                                    rp4 = requests.get(play_base, params=pp, timeout=120)
                                    pres = rp4.json()
                                    if pres.get("is_success"):
                                        state["consecutive_rejects"] = 0
                                        state["total_moves"] += 1
                                        state["chosen_play"] = []
                                        write_state(state)
                                        e_log("[RETRY-PASS] pass OK")
                                        move_log.append({
                                            "move": state["total_moves"], "hand_len": len(fresh_hand), "level": fresh_level,
                                            "last_play": fresh_last_play, "last_player_role": fresh_role,
                                            "feasible_count": 1,
                                            "chosen_play": [], "result": pres,
                                            "server_state": {k: sync_state.get(k) for k in
                                                ("hand_counts", "current_turn", "last_play", "last_player",
                                                 "trick_history", "ranking", "completed", "is_your_turn")},
                                            "mode": "retry_pass",
                                        })
                                        flush_game_log()
                                        retry_ok = True
                                except Exception:
                                    pass
                except Exception as ex:
                    e_log(f"[RETRY] re-sync failed: {ex}")

                if retry_ok:
                    time.sleep(sleep_after_play)
                    continue  # 重试成功，回到主循环
                else:
                    e_log(f"[RETRY-FAIL] all retries exhausted")
                    time.sleep(sleep_on_reject)

            elif action == "resync":
                state["consecutive_rejects"] = 0
                write_state(state)
                e_log(f"[REJECT] {error_msg} → resync")
                # 重新获取服务端状态
                try:
                    rs = requests.get(check_url, timeout=60)
                    if rs.status_code == 200:
                        fresh = rs.json()
                        state["your_hand"] = fresh.get("your_hand", state["your_hand"])
                        state["last_play"] = fresh.get("last_play", [])
                        _lp = fresh.get("last_player"); state["last_player"] = _lp if _lp is not None else -1
                        state["hand_counts"] = fresh.get("hand_counts", state["hand_counts"])
                        e_log(f"[RESYNC] hand updated, len={len(state['your_hand'])}")
                except Exception as ex:
                    e_log(f"[RESYNC] failed: {ex}")
                write_state(state)

            elif action == "abort":
                state["status"] = "finished"
                state["running"] = False
                state["message"] = f"Game aborted: {error_msg}"
                write_state(state)
                e_log(f"[ABORT] {error_msg}")
                break
            else:
                state["consecutive_rejects"] = state.get("consecutive_rejects", 0) + 1
                write_state(state)
                e_log(f"[UNKNOWN] {error_msg} (rejects={state['consecutive_rejects']})")

            if state.get("consecutive_rejects", 0) >= 6:
                state["consecutive_rejects"] = 0
                write_state(state)
                e_log("[ABORT] 10 consecutive rejects")
                time.sleep(sleep_on_reject)
                continue

            move_log.append({
                "move": state["total_moves"], "hand_len": len(hand), "level": level,
                "last_play": last_play, "last_player_role": role,
                "feasible_count": len(feasible) if feasible else 0,
                "chosen_play": best_play, "result": play_result,
                "server_state": {k: check_state.get(k) for k in
                    ("hand_counts", "current_turn", "last_play", "last_player",
                     "trick_history", "ranking", "completed", "is_your_turn")},
                "mode": "manual" if state.get("manual_mode") else "ai",
            })
            flush_game_log()
            time.sleep(sleep_after_play)

        else:
            # 不是我方回合 → 轮询等待，期间持续检查命令且每 3s 刷新服务端状态
            wait_secs = random.randint(sleep_min, sleep_max)
            _refresh_every = max(2, min(5, int(wait_secs * 0.4)))  # 2~5s 刷新服务端
            for i in range(wait_secs):
                if not state["running"]:
                    break
                # 检查命令（与顶层逻辑一致）
                c = read_cmd()
                ca = c.get("cmd", "none")
                if ca == "stop":
                    clear_cmd()
                    state["running"] = False
                    state["status"] = "idle"
                    state["message"] = "Game stopped"
                    write_state(state)
                    return
                if ca == "manual_mode":
                    new_mode = c.get("manual_mode", False)
                    state["manual_mode"] = new_mode
                    if not new_mode:
                        state["manual_play"] = None
                    clear_cmd()
                    e_log(f"[GAME] manual_mode = {new_mode}")
                    write_state(state)
                # 定期从服务端拉取最新牌桌状态（刷新前端显示）
                if i > 0 and i % _refresh_every == 0:
                    try:
                        _r = requests.get(check_url, timeout=15)
                        if _r.status_code == 200:
                            _fresh = _r.json()
                            state["hand_counts"] = _fresh.get("hand_counts", state["hand_counts"])
                            state["trick_history"] = _fresh.get("trick_history", state["trick_history"])
                            state["last_play"] = _fresh.get("last_play", [])
                            _lp = _fresh.get("last_player"); state["last_player"] = _lp if _lp is not None else state.get("last_player", -1)
                            state["ranking"] = _fresh.get("ranking", state.get("ranking", []))
                            # 提前检测对局结束（完整判定留给主循环）
                            if _fresh.get("completed"):
                                state["completed"] = True
                    except Exception:
                        pass
                state["status"] = "waiting"
                state["message"] = "Waiting for my turn..."
                write_state(state)
                time.sleep(1)
                # 如果在等待期间检测到对局结束，提前跳出
                if state.get("completed"):
                    break
            if state.get("completed"):
                continue

    # ── Game over ──（全权信任服务端数据判断胜负）
    state["running"] = False
    if state["status"] not in ("finished", "error"):
        state["status"] = "finished"
    write_state(state)
    _flush_logs()

    # ── 神之一手模式：跳过对局记录，直接返回 ──
    if _fake_fame_skip:
        e_log("[FAKE_FAME] 跳过对局记录保存，不产生战绩")
        state["status"] = "finished"
        state["message"] = "神之一手模式：本局已跳过"
        write_state(state)
        # 清理 pending 文件
        try:
            if os.path.exists(pending_path):
                os.remove(pending_path)
        except Exception:
            pass
        return

    try:
        # 信任服务端返回的 winner_team 和 your_team 直接判断胜负
        _wt = state.get("winner_team")
        _yt = state.get("your_team")
        if _wt is None or _yt is None or _wt == -1 or _yt == -1:
            e_log(f"[SKIP] server did not provide complete result (winner_team={_wt}, your_team={_yt}), skip stats")
            return
        setup_dirs()
        is_win = (state["winner_team"] == state["your_team"])
        prefix = "W" if is_win else "L"
        final_name = f"{prefix}_game{game_id}_{timestamp}.json"
        final_path = os.path.join(get_logs_dir(), final_name)
        log_data = {
            "timestamp": timestamp, "game_id": game_id, "user": user,
            "address": address, "is_win": is_win,
            "final_state": dict(state), "moves": move_log,
        }
        with open(final_path, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
        try:
            if os.path.exists(pending_path):
                os.remove(pending_path)
        except Exception:
            pass
        stats = load_stats()
        stats["total"] = stats.get("total", 0) + 1
        if is_win:
            stats["wins"] = stats.get("wins", 0) + 1
        else:
            stats["losses"] = stats.get("losses", 0) + 1
        stats.setdefault("games", []).append({
            "log": final_name, "game_id": str(game_id),
            "is_win": is_win, "timestamp": timestamp, "moves": len(move_log),
        })
        save_stats(stats)
        result_str = "WIN" if is_win else "LOSS"
        total = stats["total"]
        wins = stats["wins"]
        e_log(f"[DONE] game {result_str} | {wins}/{total} ({wins/total*100:.1f}%)")
        print(f"\n  Game #{game_id} {result_str} | {wins}/{total} ({wins/total*100:.1f}%)\n")
    except Exception as e:
        e_log(f"[ERROR] stats save failed: {e}")


if __name__ == "__main__":
    # ── 强制 UTF-8 编码，避免 Windows GBK 环境下的 UnicodeEncodeError ──
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    print("[ENGINE] Guandan engine process starting...", flush=True)
    print(f"[ENGINE] PID: {os.getpid()}", flush=True)
    os.makedirs(get_temp_dir(), exist_ok=True)
    try:
        run_engine()
    except KeyboardInterrupt:
        e_log("[ENGINE] interrupted by user")
        _flush_logs()
    except Exception as e:
        e_log(f"[ENGINE] FATAL: {e}\n{traceback.format_exc()}")
        _flush_logs()
        sys.exit(1)

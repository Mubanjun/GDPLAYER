"""Step 10: Flask Web 前端（轻量）— 引擎进程通过文件共享状态，GIL 隔离。
引擎文件:
  temp/engine_state.json  ← 引擎写入完整状态
  temp/engine_logs.jsonl  ← 引擎追加日志
  temp/engine_cmd.json    → 前端写入控制命令
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from scorer import score_all as _score_all
from feasible import generate_feasible
from utils import (
    determine_last_player_role, identify_hand_type, cards_to_display,
    parse_config, setup_dirs, load_stats, save_stats,
    get_temp_dir, get_logs_dir, get_stats_file, get_state_file, get_log_file, get_cmd_file,
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_BASE_DIR, "config.cfg")
TEMP_DIR = get_temp_dir()
LOGS_DIR = get_logs_dir()
STATS_FILE = get_stats_file()
STATE_FILE = get_state_file()
LOG_FILE = get_log_file()
CMD_FILE = get_cmd_file()

app = Flask(__name__, static_folder=None)

_engine_proc = None
_engine_lock = threading.Lock()
_user_stopped = False  # 用户主动停止标志，防止monitor误重启


def _check_engine_alive() -> bool:
    """检测引擎进程是否存活。"""
    global _engine_proc
    if _engine_proc is None:
        return False
    return _engine_proc.poll() is None


def read_engine_state() -> dict:
    """读取引擎状态（无锁，纯读文件）。"""
    engine_alive = _check_engine_alive()
    if not os.path.exists(STATE_FILE):
        return {
            "running": False, "status": "idle", "game_id": None, "level": "",
            "your_seat": -1, "your_hand": [], "your_team": -1,
            "is_your_turn": False, "last_play": [], "last_player": -1,
            "current_turn": -1, "hand_counts": [0, 0, 0, 0],
            "ranking": [], "winner_team": -1, "completed": False,
            "trick_history": [], "seats": [], "teams": [],
            "chosen_play": [], "total_moves": 0, "error": None,
            "message": "Engine not running" if not engine_alive else "Engine initializing...",
            "battle_active": True,
            "manual_mode": False, "started": False, "engine_alive": engine_alive,
            "engine_pid": _engine_proc.pid if _engine_proc else None,
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        # ── 清理内部字段，不暴露给前端 ──
        s.pop("_auto_start", None)
        # ── 心跳检测：引擎进程是否存活 + 状态文件是否过期 ──
        s["engine_alive"] = engine_alive
        s["engine_pid"] = _engine_proc.pid if _engine_proc else None
        if engine_alive:
            engine_ts = s.get("engine_ts", 0)
            now = time.time()
            # 如果状态文件超过 30 秒未更新但引擎进程存活 → 可能卡死
            if now - engine_ts > 30:
                s["engine_heartbeat_stale"] = True
                s["engine_last_seen_ago"] = round(now - engine_ts, 1)
            else:
                s["engine_heartbeat_stale"] = False
        # 补充计算字段（信任引擎端 started，不再客户端重算）
        hc = s.get("hand_counts", [0, 0, 0, 0])
        s["started"] = s.get("started", False)
        s["is_success"] = s.get("status") not in ("error",)
        s.setdefault("manual_mode", False)
        return s
    except Exception as e:
        return {
            "running": False, "status": "error",
            "message": f"Failed to read engine state: {e}",
            "started": False, "engine_alive": engine_alive,
        }


def write_cmd(cmd: dict):
    """写入控制命令给引擎。"""
    os.makedirs(TEMP_DIR, exist_ok=True)
    cmd["ts"] = time.time()
    tmp = CMD_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cmd, f)
    os.replace(tmp, CMD_FILE)


def read_logs_tail(max_lines: int = 500) -> list:
    """读取引擎日志（从 JSONL 文件尾部）。"""
    if not os.path.exists(LOG_FILE):
        return []
    entries = []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        start = max(0, len(lines) - max_lines)
        for line in lines[start:]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return entries


def launch_engine():
    """启动引擎子进程。"""
    global _engine_proc, _user_stopped
    with _engine_lock:
        _user_stopped = False  # 启动时重置用户停止标志
        # 杀旧进程
        if _engine_proc and _engine_proc.poll() is None:
            try:
                _engine_proc.kill()
                _engine_proc.wait(timeout=5)
            except Exception:
                pass
        # 清空旧日志和旧命令（每次引擎重启都从干净状态开始）
        try:
            open(LOG_FILE, "w", encoding="utf-8").close()
        except Exception:
            pass
        try:
            if os.path.exists(CMD_FILE):
                os.remove(CMD_FILE)
        except Exception:
            pass
        engine_py = os.path.join(_BASE_DIR, "engine.py")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        _engine_proc = subprocess.Popen(
            [sys.executable, engine_py],
            cwd=_BASE_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        print(f"[WEB] Engine process started, PID={_engine_proc.pid}", flush=True)
        return _engine_proc.pid


def stop_engine():
    """停止引擎进程。"""
    global _engine_proc, _user_stopped
    with _engine_lock:
        _user_stopped = True  # 标记用户主动停止，防止monitor自动重启
        write_cmd({"cmd": "stop"})
        time.sleep(0.5)
        if _engine_proc and _engine_proc.poll() is None:
            try:
                _engine_proc.kill()
                _engine_proc.wait(timeout=5)
            except Exception:
                pass
        _engine_proc = None


# ═══ Flask 路由 ═══

@app.route("/ping")
@app.route("/api/ping")
def api_ping():
    s = read_engine_state()
    return jsonify({"ok": True, "time": time.time(), "running": s.get("running", False)})


@app.route("/")
def index():
    here = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(here, "index.html")


@app.route("/play_replica")
def play_replica():
    here = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(here, "play_replica.html")


@app.route("/api/status")
def api_status():
    s = read_engine_state()
    s["recent_logs"] = read_logs_tail(300)  # 包含最近 300 条引擎日志
    # 引擎进程退出码（如果已退出）
    if _engine_proc and _engine_proc.poll() is not None:
        s["engine_exit_code"] = _engine_proc.returncode
    return jsonify(s)


@app.route("/api/stats")
def api_stats():
    return jsonify(load_stats())


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        old = parse_config(CONFIG_FILE)
        old.update({k: str(v) for k, v in data.items() if v is not None})
        lines = []
        for k, v in old.items():
            lines.append(f'{k}="{v}"')
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return jsonify({"ok": True})
    return jsonify(parse_config(CONFIG_FILE))


@app.route("/api/start", methods=["POST"])
def api_start():
    """启动新对局：先停旧引擎，再启动新引擎，发 START 命令。"""
    data = request.get_json(silent=True) or {}
    manual_mode = data.get("manual_mode", False)
    stop_engine()
    time.sleep(0.5)
    launch_engine()
    time.sleep(1.0)
    write_cmd({"cmd": "start", "manual_mode": manual_mode})
    return jsonify({"ok": True, "message": "Game restarting"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    write_cmd({"cmd": "stop"})
    stop_engine()
    return jsonify({"ok": True, "message": "Stopped"})


@app.route("/api/restart_engine", methods=["POST"])
def api_restart_engine():
    """手动重启引擎进程。"""
    data = request.get_json(silent=True) or {}
    manual_mode = data.get("manual_mode", False)
    stop_engine()
    time.sleep(0.5)
    pid = launch_engine()
    time.sleep(1.0)
    write_cmd({"cmd": "start", "manual_mode": manual_mode})
    return jsonify({"ok": True, "message": f"Engine restarted, PID={pid}"})


def _engine_monitor():
    """后台线程：每15秒检测引擎进程是否存活，挂了自动重启。"""
    print("[MONITOR] engine health monitor started", flush=True)
    while True:
        time.sleep(15)
        try:
            if not _check_engine_alive() and not _user_stopped:
                s = read_engine_state()
                was_manual = s.get("manual_mode", False)
                print("[MONITOR] engine process DIED, auto-restarting...", flush=True)
                launch_engine()
                time.sleep(1.0)
                write_cmd({"cmd": "start", "manual_mode": was_manual})
                print("[MONITOR] engine restarted", flush=True)
        except Exception as e:
            print(f"[MONITOR] error: {e}", flush=True)


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    write_cmd({"cmd": "stop"})
    time.sleep(0.5)
    return jsonify({"ok": True, "message": "Game cancelled"})


@app.route("/api/manual_mode", methods=["POST"])
def api_manual_mode():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", None)
    s = read_engine_state()
    cur = s.get("manual_mode", False)
    if mode == "manual":
        new_mode = True
    elif mode == "auto":
        new_mode = False
    else:
        new_mode = not cur
    write_cmd({"cmd": "manual_mode", "manual_mode": new_mode})
    return jsonify({"manual_mode": new_mode})


@app.route("/api/manual_play", methods=["POST"])
def api_manual_play():
    data = request.get_json(silent=True) or {}
    cards = data.get("coord", None)
    if cards is None:
        return jsonify({"ok": False, "message": "No coord provided"}), 400
    write_cmd({"cmd": "manual_play", "manual_play": list(cards)})
    return jsonify({"ok": True, "coord": cards})


@app.route("/api/ai_suggest")
def api_ai_suggest():
    """AI 建议出牌（运行在 Flask worker 中，手牌 ≤12 防止阻塞）。"""
    s = read_engine_state()
    if not s.get("is_your_turn"):
        return jsonify({"suggest": None, "message": "Not your turn"})
    hand = list(s.get("your_hand", []))
    level = s.get("level", "")
    last_play = s.get("last_play", [])
    last_player = s.get("last_player", -1)
    your_seat = s.get("your_seat", -1)
    teams = s.get("teams", [])

    role = None
    try:
        role = determine_last_player_role(last_player, your_seat, teams)
    except Exception:
        pass

    if len(hand) > 12:
        return jsonify({"suggest": None, "message": "Hand too large for AI suggest"})

    feasible = generate_feasible(hand, level, last_play, role)
    if not feasible:
        feasible = [[]]
    if len(feasible) > 100:
        feasible = feasible[:100]

    scored = _score_all(hand, feasible, level, last_play, role)
    top3 = []
    for sc, cards in scored[:3]:
        t, r = identify_hand_type(cards, level) if cards else ("pass", "")
        top3.append({"cards": cards, "score": round(sc, 1), "type": t, "rank": r})

    return jsonify({"suggest": top3})


@app.route("/api/logs")
def api_logs():
    """返回引擎日志（最近 500 条）。"""
    return jsonify(read_logs_tail(500))


@app.route("/api/logs_history")
def api_logs_history():
    """返回历史对局列表。"""
    setup_dirs()
    files = sorted(os.listdir(LOGS_DIR), reverse=True)[:20]
    result = []
    for fname in files:
        fpath = os.path.join(LOGS_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            result.append({
                "file": fname,
                "is_win": data.get("is_win", False),
                "timestamp": data.get("timestamp", ""),
                "game_id": data.get("game_id", ""),
                "moves": len(data.get("moves", [])),
            })
        except Exception:
            pass
    return jsonify(result)


@app.route("/api/clear_history", methods=["POST"])
def api_clear_history():
    import glob
    setup_dirs()
    try:
        for f in glob.glob(os.path.join(LOGS_DIR, "*.json")):
            os.remove(f)
        save_stats({"total": 0, "wins": 0, "losses": 0, "games": []})
        return jsonify({"ok": True, "message": "History cleared"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/delete_log/<fname>", methods=["POST"])
def api_delete_log(fname):
    fpath = os.path.join(LOGS_DIR, fname)
    try:
        if not os.path.exists(fpath):
            return jsonify({"ok": False, "message": f"File not found: {fname}"}), 404
        if fname.startswith("PENDING"):
            write_cmd({"cmd": "stop"})
            time.sleep(0.3)
        os.remove(fpath)
        return jsonify({"ok": True, "message": f"Deleted {fname}"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/replay/<fname>")
def api_replay(fname):
    fpath = os.path.join(LOGS_DIR, fname)
    if not os.path.exists(fpath):
        return jsonify({"error": "File not found"}), 404
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pending_log")
def api_pending_log():
    setup_dirs()
    s = read_engine_state()
    gid = s.get("game_id")
    if gid:
        # 找最新的 PENDING 文件
        import glob
        pattern = os.path.join(LOGS_DIR, f"PENDING_game{gid}_*.json")
        files = sorted(glob.glob(pattern), reverse=True)
        if files:
            try:
                with open(files[0], "r", encoding="utf-8") as f:
                    return jsonify(json.load(f))
            except Exception:
                pass
    return jsonify({"moves": [], "is_win": None})


def main():
    # ── 强制 UTF-8 编码，避免 Windows GBK 环境下的 UnicodeEncodeError ──
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

    # ── 进程锁 ──
    import atexit
    _lock_file_path = os.path.join(_BASE_DIR, ".server.lock")

    def _cleanup():
        """退出时的清理：杀引擎 + 删锁文件。"""
        stop_engine()
        try:
            if os.path.exists(_lock_file_path):
                os.unlink(_lock_file_path)
        except Exception:
            pass

    # ── 信号处理：捕获 Ctrl+C / kill  → 优雅退出 ──
    _shutdown_requested = False

    def _signal_handler(signum, frame):
        nonlocal _shutdown_requested
        if _shutdown_requested:
            # 二次 Ctrl+C → 强制退出
            print("\n[WEB] Force exit", flush=True)
            os._exit(1)
        _shutdown_requested = True
        print("\n[WEB] Shutting down... (Ctrl+C again to force)", flush=True)
        # 在信号 handler 中只做最小操作，实际清理在主线程 finally 中完成
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ── atexit 兜底清理 ──
    atexit.register(_cleanup)

    try:
        _lock_fd = open(_lock_file_path, "x")
        _lock_fd.write(str(os.getpid()))
        _lock_fd.close()
    except FileExistsError:
        try:
            with open(_lock_file_path) as f:
                old_pid = int(f.read().strip())
            r = subprocess.run(
                f'tasklist /FI "PID eq {old_pid}" /NH',
                capture_output=True, text=True, shell=True, timeout=5
            )
            if str(old_pid) in r.stdout:
                print(f"ERROR: Another server instance (PID {old_pid}) is already running!")
                print(f"       Run kill_server.ps1 first, or delete {_lock_file_path}")
                sys.exit(1)
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass
        try:
            os.unlink(_lock_file_path)
            _lock_fd = open(_lock_file_path, "x")
            _lock_fd.write(str(os.getpid()))
            _lock_fd.close()
        except:
            pass

    setup_dirs()
    config = parse_config(CONFIG_FILE)
    port = int(config.get("port", "8080"))

    # 启动引擎进程（但不自动开始游戏，由用户手动或打榜脚本触发）
    launch_engine()
    time.sleep(1.0)

    # 启动引擎健康监测后台线程
    monitor_thread = threading.Thread(target=_engine_monitor, daemon=True)
    monitor_thread.start()

    print(f"Starting web server at http://0.0.0.0:{port}")
    try:
        import waitress
        print("  (waitress WSGI server, multi-threaded)")
        waitress.serve(app, host="0.0.0.0", port=port, threads=8,
                       connection_limit=1000, channel_request_lookahead=0)
    except ImportError:
        print("  WARNING: waitress not installed, falling back to Flask dev server")
        app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        print("[WEB] Cleaning up...", flush=True)
        _cleanup()
        print("[WEB] Shutdown complete.", flush=True)


if __name__ == "__main__":
    main()

"""打榜监控脚本：自动循环对战。
运行：python _battle.py

架构说明：
- 本脚本作为外部监控进程，独立运行
- 每5秒轮询 /api/status，检测对局状态
- 对局结束后，自动调用 /api/start 开始下一局
- 不依赖服务端 cancel_game（该接口不存在）
- 服务端 join_game 总是返回同一个 game_id（用户持久会话）
"""
import requests, time, io, sys, json, traceback, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from datetime import datetime

# 从配置文件读取服务器地址
def _load_base_url():
    try:
        import re
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.cfg")
        pattern = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
        with open(cfg_path, "r", encoding="utf-8") as f:
            for line in f:
                m = pattern.match(line.strip())
                if m and m.group(1) == "port":
                    return f'http://localhost:{m.group(2)}'
    except Exception:
        pass
    return 'http://localhost:8080'

BASE = _load_base_url()

def log(msg):
    print(f'  [{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)

def api_post(path, data=None, timeout=10):
    try:
        r = requests.post(f'{BASE}{path}', json=data or {}, timeout=timeout)
        return r.json()
    except Exception as e:
        log(f'HTTP POST {path} failed: {e}')
        return None

def api_get(path, timeout=8):
    try:
        r = requests.get(f'{BASE}{path}', timeout=timeout)
        return r.json()
    except Exception as e:
        log(f'HTTP GET {path} failed: {e}')
        return None

def get_sleep_between_games():
    cfg = api_get('/api/config')
    if cfg:
        return int(cfg.get('sleep_between_games', 8))
    return 8

def start_new():
    """启动新对局（/api/start 会先停止旧线程再创建新线程）。"""
    result = api_post('/api/start', timeout=15)
    if result and result.get('ok'):
        log('NEW GAME started')
        return True
    else:
        log(f'Start failed: {result}')
        return False

def force_full_reset():
    """强制重置：尝试取消前端的状态。"""
    result = api_post('/api/cancel', timeout=5)
    if result is None:
        log('force_full_reset: /api/cancel 无响应（服务可能离线）')
    elif not result.get('ok'):
        log(f"force_full_reset: /api/cancel 返回失败: {result.get('message', result)}")
    time.sleep(2)

# ── 主循环 ──
print('=' * 60)
print('  GUANDAN 打榜监控 v2')
print('  每5秒轮询状态，对局结束后自动开下一局')
print('  无需服务端 cancel 支持 - 自动续接同一会话')
print('=' * 60, flush=True)

# 启动首局
start_new()

stats = {'games': 0, 'wins': 0, 'losses': 0, 'errors': 0, 'loops': 0}
consecutive_api_errors = 0
last_game_id = None
no_reply_count = 0  # 服务端长时间无响应计数

while True:
    stats['loops'] += 1
    time.sleep(5)

    # 每 100 轮显示统计摘要
    if stats['loops'] % 100 == 0:
        w, l = stats['wins'], stats['losses']
        pct = 0 if (w + l) == 0 else w / (w + l) * 100
        elapsed_h = stats['loops'] * 5 / 3600
        log(f'STATS: {stats["loops"]}轮 | {stats["games"]}局 W={w} L={l} ({pct:.1f}%) | {elapsed_h:.1f}h')

    # 读取状态
    s = api_get('/api/status')
    if s is None:
        consecutive_api_errors += 1
        stats['errors'] += 1
        if consecutive_api_errors >= 6:
            log(f'API连续{consecutive_api_errors}次无响应，等待10s后重试...')
            time.sleep(10)
        if consecutive_api_errors >= 15:
            log('API长时间无响应，执行本地重置...')
            time.sleep(30)
            start_new()
            consecutive_api_errors = 0
        continue

    consecutive_api_errors = 0

    running = s.get('running', False)
    status = s.get('status', '')
    gid = s.get('game_id', '') or ''
    moves = s.get('total_moves', 0)
    hand_len = len(s.get('your_hand', []))
    turn = s.get('is_your_turn', False)
    completed = s.get('completed', False)
    message = str(s.get('message', ''))[:60]

    # 检测 game_id 变化
    if gid and gid != last_game_id:
        if last_game_id:
            log(f'Game ID changed: {last_game_id} → {gid}')
        last_game_id = gid

    # 缩短的状态行（重点信息）
    w, l = stats['wins'], stats['losses']
    pct = 0 if (w + l) == 0 else w / (w + l) * 100
    short_msg = message[:40] if message else status
    print(f'  [#{stats["games"]}|{stats["loops"]}] {status:8s} g={gid or "-":4s} '
          f'mv={moves:3d} h={hand_len:2d} turn={str(turn):5s} | '
          f'{w}/{l} ({pct:.1f}%) | {short_msg}', flush=True)

    if running:
        # 对局进行中 → 无需操作
        no_reply_count = 0
        continue

    # ── 对局未运行 ──
    if completed or status in ('finished',):
        # 对局已结束 → 记录结果
        winner = s.get('winner_team')
        your_team = s.get('your_team')
        win = (winner is not None and winner == your_team)
        stats['games'] += 1
        if win:
            stats['wins'] += 1
        else:
            stats['losses'] += 1
        pct = 0 if (w + l) == 0 else w / (w + l) * 100
        log(f'GAME #{stats["games"]} END: {"WIN" if win else "LOSS"} | '
            f'W/L: {stats["wins"]}/{stats["losses"]} ({pct:.1f}%)')

        # 等待配置时间后开下一局
        sleep_sec = get_sleep_between_games()
        log(f'等待 {sleep_sec}s 后开始下一局...')
        time.sleep(sleep_sec)
        start_new()
        no_reply_count = 0

    elif status == 'idle':
        # 空闲状态 → 启动新对局
        log(f'IDLE - 启动新对局...')
        time.sleep(2)
        start_new()

    elif status == 'error':
        # 错误状态 → 尝试重启
        log(f'ERROR状态({message})，重置后重试...')
        force_full_reset()
        time.sleep(3)
        start_new()

    elif status in ('starting', 'joining'):
        # 正在启动中 → 等待
        no_reply_count += 1
        if no_reply_count > 24:  # 2分钟未进入playing状态
            log(f'启动超时({no_reply_count*5}s)，强制重启...')
            force_full_reset()
            time.sleep(3)
            start_new()
            no_reply_count = 0
        pass

    else:
        # 其他状态 → 重启
        log(f'未知状态({status})，尝试重启...')
        force_full_reset()
        time.sleep(3)
        start_new()

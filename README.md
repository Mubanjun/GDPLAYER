# GDPLAYER — 掼蛋 AI 自动对战系统  
**注意: 本项目依托于内蒙古大学的掼蛋大作战Python项目！**
轻量级掼蛋 AI 对战客户端，通过 HTTP 对接在线掼蛋服务器，自动完成加入对局、轮询状态、启发式评分出牌的完整流程。

## 语言与依赖

- **语言**：Python 3.8+
- **核心依赖**（3 个）：

| 依赖 | 用途 | 说明 |
|---|---|---|
| `flask` | Web 框架 | 提供对战面板 UI 和 API 网关 |
| `requests` | HTTP 客户端 | 调用游戏服务器接口 |
| `waitress` | 生产 WSGI 服务器 | 多线程处理请求，未安装时回退至 Flask 开发服务器 |

安装依赖：

```bash
pip install flask requests waitress
```

## 目录结构

```
GDPLAYER/
├── main.py            # Flask Web 主进程（API 网关 + 引擎管理）
├── engine.py          # 游戏引擎进程（HTTP 轮询 + AI 决策 + 出牌）
├── scorer.py          # 启发式评分引擎（20+ 维度加权）
├── feasible.py        # 可行牌生成器（枚举合法出牌组合）
├── utils.py           # 牌面工具库（识别 / 排序 / 比较 / RSA 加密）
├── _battle.py         # 打榜监控脚本（自动循环对战）
├── index.html         # 对战面板 Web UI
├── play_replica.html  # 历史回放 UI
├── config.cfg         # 配置文件（需手动创建，见下方说明）
└── temp/              # 运行时数据（自动创建）
```

## 配置文件

首次运行前需在当前目录创建 `config.cfg`：

```ini
# 服务器连接
address="http://183.175.14.145:8004"
user="你的用户名"
password="你的明文密码"

# RSA 加密参数（向服务器管理员索取）
rsa_e="65537"
rsa_n="13526182891679194670531..."

# Web 服务端口
port="8080"

# 出牌节奏（秒）
sleep_min="1"
sleep_max="5"
sleep_after_play="1"
sleep_between_games="5"
game_timeout_minutes="30"

# 启发式权重（以下为默认值，可按需调整）
W_CLEAR="10000.0"
W_CONTROL="20.0"
W_PLAY_BONUS="68.0"
W_PASS_PENALTY="80.0"
W_ROUND_PENALTY="12.0"
W_HAND_DECLINE_RATE="25.0"
# ... 完整权重列表见 scorer.py 中 _DEFAULT_WEIGHTS
```

> `config.cfg` 含明文凭据，已在 `.gitignore` 中排除，不会提交到版本库。

## 运行方式

### 方式一：Web 面板模式（推荐）

启动 Web 服务器，自动拉起引擎进程：

```bash
cd GDPLAYER
python main.py
```

浏览器访问 `http://localhost:8080`，在面板上点击「开始对局」即可上桌。

- Web 进程与引擎进程通过 `temp/` 目录下的文件通信（GIL 隔离）
- 引擎崩溃时 Web 会每 15 秒自动检测并重启
- 面板支持手动出牌模式、AI 建议、历史回放

### 方式二：自动打榜模式

先启动 Web 服务（方式一），再另开终端运行打榜脚本：

```bash
cd GDPLAYER
python _battle.py
```

打榜脚本每 5 秒轮询 `/api/status`，对局结束自动开始下一局，实现 7×24 小时无人值守对战。

## 如何上桌对战

完整流程分三步：

**1. 配置账号**

编辑 `config.cfg`，填入服务器地址、用户名、密码和 RSA 公钥参数。

**2. 启动服务**

```bash
python main.py
```

看到以下输出表示就绪：

```
[WEB] Engine process started, PID=12345
Starting web server at http://0.0.0.0:8080
```

**3. 开始对战**

- **手动**：浏览器打开 `http://localhost:8080`，点击「开始对局」
- **自动**：运行 `python _battle.py`，脚本会自动循环开局

引擎上桌后会自动完成：加入对局 → 轮询状态 → 我方回合时调用评分引擎选牌 → 提交出牌 → 等待对手 → 循环直至对局结束 → 记录日志 → 开启下一局。

## 架构简述

```
浏览器 ──HTTP──→ Flask (main.py) ──文件──→ 引擎 (engine.py) ──HTTP──→ 游戏服务器
                     │                      │
                     │                      ├── feasible.py  枚举合法出牌
                     │                      ├── scorer.py    启发式评分
                     │                      └── utils.py     牌面工具
                     │
                     └── temp/engine_state.json  状态共享
                         temp/engine_cmd.json    命令传递
                         temp/engine_logs.jsonl  日志流
```

双进程分离设计，Flask 与引擎通过文件共享实现 GIL 隔离，避免长耗时 AI 计算阻塞 Web 响应。

# okxquant

面向 BTC/USDT 的命令行量化交易工具，基于 OKX 官方 `python-okx` SDK。

拉行情 → 写策略 → 跑回测 → 模拟盘自动交易 → 风控熔断 → 终端复盘，全链路在终端内完成，**没有任何网页界面**。

> **仅用于模拟盘。** 默认使用 OKX 模拟交易环境（`OKX_FLAG=1`），不动用真实资金。
> 切换实盘需要同时修改配置 `risk.allow_live = true` 并追加 `--i-accept-real-money`，详见[安全边界](#安全边界)。

---

## 目录

- [特性](#特性)
- [环境要求](#环境要求)
- [安装](#安装)
- [配置](#配置)
- [命令速查](#命令速查)
- [写自己的策略](#写自己的策略)
- [风控规则](#风控规则)
- [数据与目录](#数据与目录)
- [安全边界](#安全边界)
- [远程部署与验证](#远程部署与验证)
- [已知限制](#已知限制)
- [风险声明](#风险声明)

---

## 特性

| 能力 | 说明 |
| --- | --- |
| 历史数据 | 分页拉取 BTC-USDT K 线落 SQLite，幂等写入，重复执行只补缺口 |
| 实时行情 | 公有 WebSocket 订阅 K 线与 ticker，断线指数退避重连 |
| 策略体系 | 内置网格、双均线；自定义策略是普通 `.py` 文件，改完自动热重载 |
| 回测 | 事件驱动，可配手续费/滑点/初始资金，输出收益、回撤、夏普、胜率、盈亏比与逐笔成交 |
| 模拟盘交易 | 走 OKX 模拟盘 REST 下单，私有 WS 回报成交，启动自动对账防重复下单 |
| 风控 | 单笔金额、持仓占比、日内亏损、最小下单额、下单冷却、单日次数；触发即熔断并落库 |
| 终端看板 | TTY 下原地刷新状态带 + 资产 + 挂单 + 事件流；非 TTY 降级为周期性快照 |
| 机器可读 | 关键命令支持 `--json`，便于接自己的脚本或后续界面 |

## 环境要求

- **Python 3.12**（`python-okx` 官方声明支持到 3.12；更高版本未验证）
- [uv](https://docs.astral.sh/uv/) 用于管理解释器与虚拟环境
- 能**直接**访问 `https://www.okx.com` 与 `wss://ws.okx.com`（模拟盘为 `wss://wspap.okx.com`）
- 本工具**不内置任何网络代理**。如果所在网络无法直连交易所，请把它部署到能直连的服务器上运行，
  见[远程部署与验证](#远程部署与验证)

## 安装

```bash
# 1. 安装 uv（若已安装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 2. 拉取 Python 3.12 并创建虚拟环境、安装依赖
cd okx
uv python install 3.12
uv sync

# 3. 配置密钥
cp .env.example .env
# 编辑 .env，填入 OKX 模拟盘 API Key / Secret / Passphrase

# 4. 验证
uv run okxquant info
```

用 `uv run okxquant ...` 运行。若想全局可用：

```bash
uv tool install .
okxquant info
```

## 配置

### 密钥：`.env`

```dotenv
OKX_API_KEY=你的模拟盘 key
OKX_API_SECRET=你的模拟盘 secret
OKX_PASSPHRASE=你的 passphrase
OKX_FLAG=1          # 1 = 模拟盘（默认），0 = 实盘
```

密钥只从环境变量读取，`.env` 已在 `.gitignore` 中。日志不会打印 secret 与签名。

模拟盘 API Key 需要在 OKX 官网切换到「模拟交易」环境后再创建，与实盘 Key 不通用。

### 全局参数：`config/default.toml`

| 段 | 关键项 | 作用 |
| --- | --- | --- |
| `[market]` | `inst_id`、`bar` | 交易对与 K 线周期（`1m`/`5m`/`15m`/`1H`/`4H`/`1D`…） |
| `[storage]` | `db_path` | SQLite 路径，默认 `data/quant.db` |
| `[execution]` | `fee_rate`、`slippage_bps`、`initial_cash` | 回测成本假设与初始资金 |
| `[risk]` | 见[风控规则](#风控规则) | 下单前置校验阈值 |
| `[render]` | `equity_sample_bars` | 权益曲线落库的采样间隔（每 N 根） |
| `[strategy]` | `name`、`params` | 默认策略与参数覆盖 |

### 策略参数：`config/strategies/<name>.toml`

内置策略的参数优先级：**策略默认值 < `config/strategies/<name>.toml` < `[strategy].params` < 命令行 `--param`**。

## 命令速查

### `info` — 环境与配置摘要

```bash
uv run okxquant info
uv run okxquant --json info
```

`info` 会明确标注当前是 `SIMULATED TRADING`、`LIVE TRADING` 还是 `OFFLINE`（无凭据）。

### `download` — 历史 K 线

```bash
# 下载最近 7 天
uv run okxquant download --days 7

# 指定区间（本地时区）
uv run okxquant download --start 2026-11-01 --end 2026-11-10

# 只检测缺口，不下载
uv run okxquant download --days 30 --check

# 离线演示：生成合成数据（不代表真实行情）
uv run okxquant download --synthetic --days 7
```

重复执行不会重复写入；第二次执行会提示「已是最新」。

### `backtest` — 回测

```bash
# 默认策略（config 里的 strategy.name）跑最近 7 天
uv run okxquant backtest --days 7

# 指定策略与参数
uv run okxquant backtest --strategy grid --days 3 \
  --param lower=55000 --param upper=65000 --param grids=20

# 用自定义策略文件
uv run okxquant backtest --strategy strategies/bollinger_reversion.py --days 7

# 调整成本假设并启用风控（默认关闭，只评估策略本身）
uv run okxquant backtest --days 7 --fee 0.001 --slippage 5 --risk

# 机器可读
uv run okxquant --json backtest --days 7
```

报告包含：参数摘要、收益/超额/回撤/夏普/胜率/盈亏比/手续费、字符净值曲线、回撤走势、逐笔成交。

回测撮合规则：

- 市价单：以当根收盘价 ± `slippage_bps` 成交
- 限价单：当根 K 线触及委托价才成交，否则本根不成交
- 买入按可用现金自动缩量，卖出按持仓自动缩量
- 回测不维护挂单队列，未成交的限价单不会在后续 K 线继续等待

### `report` — 查看历史

```bash
uv run okxquant report list
uv run okxquant report list --kind live
uv run okxquant report show 3
```

### `live` — 模拟盘自动交易

```bash
# 首次务必先 dry-run：只跑信号与记账，不向交易所下单
uv run okxquant live --dry-run

# 真实向模拟盘下单
uv run okxquant live --strategy strategies/bollinger_reversion.py

# 覆盖参数
uv run okxquant live -s grid -p lower=55000 -p upper=65000 -p qty_per_grid=0.001
```

运行中：

- `Ctrl-C` 优雅退出，打印本轮成交笔数、权益、手续费、重载次数
- `kill -HUP <pid>` 手动触发策略重载
- 修改 `.py` 策略文件后约 2 秒内自动重载，重载失败会保留旧版本继续运行

### `strategy` — 策略管理

```bash
uv run okxquant strategy list
uv run okxquant strategy check strategies/bollinger_reversion.py
```

---

## 写自己的策略

### 最小示例

```python
from okxquant.domain import Intent, Side
from okxquant.indicators import EMA
from okxquant.strategy import BarCtx, Strategy


class MyStrategy(Strategy):
    name = "my_strategy"
    default_params = {"fast": 10, "slow": 30}

    def __init__(self, params=None):
        super().__init__(params)
        self.fast = EMA(self.param_int("fast"))

    def on_bar(self, ctx: BarCtx) -> list[Intent]:
        fast = self.fast.update(ctx.price)
        if fast is None:
            return []
        if ctx.position == 0 and ctx.price > fast:
            return [Intent(Side.BUY, quote_qty=ctx.cash * 0.95, reason="站上均线")]
        if ctx.position > 0 and ctx.price < fast:
            return [Intent(Side.SELL, qty=ctx.position, reason="跌破均线")]
        return []


STRATEGY = MyStrategy   # 必须叫这个名字
```

把文件放在 `strategies/` 下（或任意路径），然后：

```bash
uv run okxquant backtest -s strategies/my_strategy.py --days 7
uv run okxquant live -s strategies/my_strategy.py --dry-run
```

### 接口约定

| 成员 | 说明 |
| --- | --- |
| `STRATEGY` | 模块级变量，指向继承 `Strategy` 的类，引擎据此实例化 |
| `name` | 展示用名称 |
| `description` | 一句话说明，会出现在 `strategy list` |
| `default_params` | 默认参数字典，与配置文件、`--param` 合并 |
| `on_start()` | 可选，启动时调用一次，可返回初始动作 |
| `on_bar(ctx)` | **必须实现**，每根已收盘 K 线调用一次，返回动作列表 |
| `on_stop()` | 可选，退出时调用 |
| `state_dict()` / `load_state()` | 可选，热重载时迁移内部状态 |

### `ctx` 提供什么

| 属性 | 类型 | 说明 |
| --- | --- | --- |
| `ctx.candle` | `Candle` | 当根 K 线（`ts/open/high/low/close/volume/confirm`） |
| `ctx.price` | `float` | 收盘价快捷方式 |
| `ctx.account` | `AccountSnapshot` | `cash/position_qty/avg_cost/equity/unrealized/realized/drawdown/last_price` |
| `ctx.position` | `float` | 持仓数量 |
| `ctx.cash` | `float` | 可用现金 |
| `ctx.equity` | `float` | 总权益 |
| `ctx.bar_index` | `int` | 已处理 K 线计数 |

### 下单动作

```python
Intent(Side.BUY, quote_qty=500.0)          # 按金额买入（按现价换算数量）
Intent(Side.BUY, qty=0.01)                 # 按数量买入（市价）
Intent(Side.SELL, qty=ctx.position)        # 全部卖出
Intent(Side.BUY, qty=0.01, px=59000)       # 限价买入
CancelAll(reason="撤掉所有挂单")            # 撤销该交易对全部挂单
```

`qty` 与 `quote_qty` 必须二选一；两者都为正数；`px` 为空表示市价。

### 可用指标

全部是 O(1) 增量实现，数据不足时返回 `None`：

```python
from okxquant.indicators import ATR, EMA, RSI, SMA, Bollinger, Donchian, Window

sma = SMA(20);          sma.update(price)              # -> float | None
ema = EMA(20);          ema.update(price)
rsi = RSI(14);          rsi.update(close)
atr = ATR(14);          atr.update(candle)             # 需要完整 K 线算真实波幅
bb  = Bollinger(20, 2); bb.update(close)               # -> (upper, mid, lower)
don = Donchian(20);     don.update(candle)             # -> (high, low)
win = Window(100);      win.push(price); win.values    # 定长滑窗
```

### 注意事项

- 策略是**同步函数**，不要在里面发网络请求或写数据库；需要外部数据请提前准备好
- 只用 `confirm == True` 的 K 线驱动，不要自己判断盘中价
- 抛出异常会中断当前 bar 的处理并记录日志，不会崩掉进程，但请自己做好防御
- 内置策略（`ma_cross` / `grid`）不支持热重载，改参数请用配置或 `--param`，改逻辑请复制成 `.py`

## 风控规则

`live` 强制启用；`backtest` 默认关闭（加 `--risk` 启用）。规则在 `[risk]` 段配置：

| 规则 | 配置项 | 默认 | 行为 |
| --- | --- | --- | --- |
| 单笔上限 | `max_order_notional` | 500 USDT | 买入超过即拒绝（卖出不受限） |
| 持仓占比 | `max_position_ratio` | 0.8 | 买入后占比超限即拒绝 |
| 日内亏损 | `max_daily_loss` | 300 USDT | 当日亏损超过即**熔断**，停止一切下单 |
| 最小下单额 | `min_order_notional` | 10 USDT | 低于即拒绝 |
| 下单冷却 | `order_cooldown_sec` | 5 秒 | 两次下单间隔不足即拒绝 |
| 单日次数 | `max_orders_per_day` | 200 | 超过即拒绝 |

熔断状态会写入 `risk_events` 表，**进程重启后不会自动恢复**，需人工确认后再启动。

## 数据与目录

```
config/            配置（default.toml、策略参数）
strategies/        自定义策略 .py（当前保留：bollinger_reversion.py）
data/quant.db      SQLite：K 线、runs、订单、成交、权益曲线、风控事件
logs/              按天轮转的日志
```

主要表：

| 表 | 内容 |
| --- | --- |
| `candles` | `(inst_id, bar, ts)` 主键的 K 线 |
| `runs` | 每次回测/实盘运行的元信息与绩效 JSON |
| `orders` | 订单（含状态流转） |
| `fills` | 成交明细 |
| `equity_points` | 权益曲线采样点 |
| `risk_events` | 风控熔断记录 |

## 安全边界

1. 默认 `OKX_FLAG=1`，走模拟交易环境。
2. 无凭据时进入 **OFFLINE** 模式：只能下载数据与回测；`live` 必须加 `--dry-run`。
3. 检测到 `OKX_FLAG=0` 时，必须**同时**满足：
   - `config/default.toml` 中 `risk.allow_live = true`
   - 命令行追加 `--i-accept-real-money`
   否则直接退出（退出码 2）。
4. 启动横幅明确打印当前环境；实盘会额外打印红色警告。
5. 密钥只从环境变量读取，日志对 key/passphrase/签名打码。

## 远程部署与验证

本地网络可能无法直连交易所。此时把项目部署到一台能直连的服务器上运行即可——**本工具不需要任何代理**。

两种落地方式：用 [Render 托管](#render-托管部署需常驻容器)（仓库里已有 `render.yaml`），或者用自己的服务器（下面的 SSH 脚本）。

### 前置条件

服务器需要：Python 3.12、能访问 `https://www.okx.com` 与 `wss://ws.okx.com`、能访问 PyPI。

### 本地准备

在 `.env` 中填写部署目标（**只被 `scripts/deploy_remote.sh` 读取**，程序运行时不读）：

```dotenv
DEPLOY_SSH_HOST=user@your-server
DEPLOY_SSH_PORT=22
DEPLOY_SSH_PASSWORD=你的登录密码
```

### 部署

```bash
bash scripts/deploy_remote.sh                  # 上传 + 建 venv + 装依赖 + 跑测试
bash scripts/deploy_remote.sh --only-tests     # 只跑远程 pytest 与 ruff
bash scripts/deploy_remote.sh --skip-install   # 只上传，不重装依赖
bash scripts/deploy_remote.sh --clean          # 删除远程 .venv 与运行数据
```

上传时排除 `.venv`、`.env`、`data`、`logs`、`.git` 与各类缓存，**本地密钥不会离开本机**。

### 在服务器上配置密钥

首次部署会在远程生成只含 `OKX_FLAG=1` 的 `.env`（权限 600）。填入模拟盘密钥：

```bash
ssh user@your-server
cd ~/okxquant
cat > .env <<'EOF'
OKX_API_KEY=...
OKX_API_SECRET=...
OKX_PASSPHRASE=...
OKX_FLAG=1
EOF
chmod 600 .env
```

重复执行部署脚本不会覆盖已存在的 `.env`。

### 验证清单

```bash
cd ~/okxquant
.venv/bin/pytest -q                                  # 单元测试
.venv/bin/ruff check .                               # 代码检查
.venv/bin/okxquant info                              # 应显示 SIMULATED TRADING
.venv/bin/okxquant download --days 1                 # 真实 K 线落库
.venv/bin/okxquant backtest --days 1                 # 真实数据回测
.venv/bin/python scripts/ws_probe.py --matrix         # WS 端点自检
.venv/bin/python scripts/smoke_order.py --quote 20   # 下单链路冒烟（最小金额）
.venv/bin/okxquant live --dry-run                    # 实时行情，Ctrl-C 退出
```

### 已验证 / 未验证

| 环节 | 状态 |
| --- | --- |
| 历史 K 线下载（真实数据） | ✅ 已验证 |
| 回测（真实数据） | ✅ 已验证 |
| 公有 WS 行情（K 线推送） | ✅ 已验证 |
| 私有 WS（订单 / 账户频道） | ✅ 已验证 |
| 模拟盘下单 / 查询 / 撤单 / 平仓 | ✅ 已验证（`scripts/smoke_order.py`） |
| `live` 自动交易（策略触发下单） | ⚠️ 未验证——需策略在真实行情下产生信号 |
| 实盘交易 | ❌ 不支持，需要额外改造 |

### 关于 WS 端点

OKX 已公告 `:8443` 端口退役（`notice code=64010`），本项目统一使用 443：

| 频道 | 端点 |
| --- | --- |
| K 线（`candle*`） | `wss://ws.okx.com/ws/v5/business`（模拟盘 `wspap.okx.com`） |
| 私有（`orders` / `account`） | `wss://ws.okx.com/ws/v5/private` |

`ticker` / `tickers` 频道在 public 与 business 端点均返回 `60018`，因此不再订阅：
面板最新价取自 K 线收盘价，24 小时涨跌幅在启动时通过 REST 获取一次。

### Render 托管部署（需常驻容器）

`render.yaml` 已经描述好整台服务：一个常驻 Web Service，`uvicorn okxquant.web:app`
起在 `$PORT`，交易引擎作为子进程由它托管，Web 与引擎共用同一个**外部 Postgres**（不挂持久盘）。

1. 仓库推到 GitHub，Render 控制台 → **New → Blueprint** → 选中该仓库。
2. Environment 里填：
   - `OKX_DB_PATH`：Postgres 连接串，形如 `postgresql://user:pass@host/db?sslmode=require`
   - `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_PASSPHRASE`（模拟盘密钥）
   - OKX 后台把该 Key 的 **IP 白名单留空**，否则 Render 的动态出口 IP 会被拒（`50110`）
3. 部署完成后打开服务域名就是看板首页，也可以直接验接口：

```bash
curl -s https://<服务域名>/health    # engine_running=true，且 engine.blocked 为空
curl -s https://<服务域名>/status    # 最新一次 live 运行的权益与指标
curl -s https://<服务域名>/logs      # 引擎日志尾部
```

### Web 看板

`GET /` 返回一个单文件看板（`src/okxquant/static/index.html`，无构建步骤、无外部依赖）：
引擎状态、最新运行指标、权益曲线、成交明细、订单列表、日志尾部，
每 5 秒轮询一次，可关闭自动刷新。

**看板是纯只读的**：页面上没有任何启停、下单、撤单入口，只有刷新；
所有操作类动作都留给服务端托管（`AUTOSTART_ENGINE` + 看护线程）和命令行。

背后的 JSON 接口：

| 接口 | 内容 |
| --- | --- |
| `GET /health` | 存活探针 + 引擎进程状态（pid / 重启次数 / 阻塞原因） |
| `GET /status` | 环境、交易对、引擎状态、最新 live 运行（运行中会用最新采样点推算权益与收益） |
| `GET /trades?limit=50` | 最近成交 |
| `GET /equity?limit=300` | 权益曲线采样点 |
| `GET /orders?limit=30` | 最近订单 |
| `GET /logs?limit=40` | 引擎日志尾部 |

> `POST /engine/start` · `POST /engine/stop` 这两个端点仍保留给运维用（看板不调用它们）。
> 它们没有鉴权，服务暴露在公网时建议直接删掉或用反向代理加访问控制。

部署形态的约束：

| 事项 | 说明 |
| --- | --- |
| 必须常驻 | 引擎是长连接子进程，Vercel / Cloudflare 这类 serverless 平台跑不了 |
| 存储 | 全部落在外部 Postgres（Neon / Supabase / Render Postgres 均可），容器重启不丢数据；首次连接自动建表 |
| 连接池 | 用 Neon 直连串即可；Supabase / pgBouncer 的事务池化模式也兼容（代码已关闭自动 prepared statement） |
| 断线 | 空闲连接被云数据库掐断后，下一次访问会自动重连（日志里会有「正在重连」） |
| 区域 | 默认 `singapore`。OKX 对部分云厂商 IP 有限制，引擎反复退出时先看 `/logs` |
| 引擎自愈 | 子进程异常退出按 5→60s 退避自动重拉，上限 `OKX_MAX_RESTARTS`（默认 10 次）；缺凭据时不会空转，原因写在 `/health` 的 `engine.blocked` |
| free plan | 15 分钟无请求会休眠，引擎跟着睡。必须用 UptimeRobot 之类每 5 分钟 ping `/health` |

## 已知限制

- **回测撮合是简化的**：不含订单簿深度、不含部分成交、不含挂单排队，限价单只在触价当根成交。
- **现货市价单必须显式指定 `tgtCcy`**：不传时 OKX 会把 `sz` 当作计价币金额，
  容易触发 `51020`（低于最小下单额）。代码已在 `OkxRest.place_order` 内部处理。
- **实盘账本以本地为准**：启动时用交易所余额校准 USDT/BTC 数量，但持仓成本用当前价近似（交易所不返回现货成本），因此盈亏统计在带仓启动时会有偏差。
- **私有 WS 成交回报的增量拆分**：部分成交时按累计均价拆增量，单价存在小额误差。
- **`python-okx` 私有 WS 订阅内部固定等待 5 秒**，启动时会有约 5 秒延迟才收到订单推送。
- 仅支持单交易对、单策略实例；多策略并行需要开多个进程（共用同一个数据库，靠 `run_id` 隔离）。
- 网络不可达时 `download` / `live` 会失败并提示，可用 `--synthetic` 生成离线演示数据先跑通回测。

## 风险声明

本项目是技术演示与个人研究工具，不构成投资建议。加密货币交易存在本金全额损失的风险。即使只在模拟盘使用，也请先用 `--dry-run` 与回测充分验证策略逻辑，并确认自己理解每一笔下单的来源。

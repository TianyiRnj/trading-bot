# Minimal Musashi x Polymarket Bot

最小可运行的自动交易 bot：

- 轮询 Musashi `feed`
- 用 `analyze-text` 重新打分
- 只挑 Polymarket 高置信度机会
- 自动解析对应 `token_id`
- 支持 `paper` 和 `live` 两种模式

## 重要提醒

这个 bot 不能保证盈利，更不能保证“最大利润”。

我已经把默认参数调成适合小资金起步的保守版本，目标是：

- 先跑起来
- 控制回撤
- 降低误下单和流动性坑位

如果你只有 10 美元，建议先 `paper` 跑一段时间，再切 `live`。

## 1. 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 2. 配置

先编辑 `.env`。

### 先纸上交易

保留：

```bash
BOT_MODE=paper
```

### 再切真实下单

把这些填好：

```bash
BOT_MODE=live
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_SIGNATURE_TYPE=2
POLYMARKET_FUNDER=0x...
```

`POLYMARKET_SIGNATURE_TYPE` 参考官方文档：

- `0`: EOA
- `1`: POLY_PROXY
- `2`: GNOSIS_SAFE

大多数 Polymarket 网页账户通常应使用代理钱包地址作为 `FUNDER`，且签名类型通常是 `2`。

如果你用的是普通 EOA / MetaMask 钱包，按照官方 `py-clob-client` 文档，真实交易前通常还需要先设置一次 token allowances，否则下单可能直接失败。

我没有在这个版本里自动化 VPN、浏览器登录或地区绕过逻辑；它只负责读取信号和提交订单。是否符合你所在地法律、平台条款和账户限制，需要你自己确认。

## 3. 运行

```bash
python bot/main.py
```

## 4. 输出文件

- `bot/logs/bot.log`
- `bot/data/positions.json`
- `bot/data/trades.jsonl`
- `bot/data/seen_event_ids.json`

## 5. 后台运行

```bash
nohup ./.venv/bin/python bot/main.py >> bot/logs/nohup.log 2>&1 &
```

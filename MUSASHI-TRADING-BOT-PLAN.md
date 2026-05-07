# Musashi Trading Bot Plan

## 目标

把当前仓库维护成一个可以持续运行、可以复盘、并且在 `paper` / `live` 两种模式下都具备明确行为边界的交易 bot。

当前目标不再是“从零补齐基础设施”，而是：

- 保持主策略以 `Postgres` 作为唯一真源
- 保持模式切换、保护流程、恢复流程可解释
- 保持 dashboard、日志、数据库事件之间的一致性
- 把剩余风险集中到少数明确的下一步任务中

## 当前已完成能力

以下能力已经在代码中落地，不应再继续作为“待做事项”保留在计划里：

- `requested_mode` / `effective_mode` 已实现
- `live -> paper` 自动降级已实现
- 当存在真实 `live` 暴露时，直接切换到 `paper` 会被阻止，并进入保护语义
- 主策略运行态已经写入 `Postgres`
- `account_state`、`positions`、`orders`、`trade_events`、`seen_events`、`mode_state`、`equity_snapshots` 已存在
- 主策略 dashboard 和 JSON API 已从 `Postgres` 读取
- 资金模型已经是 mark-to-market，而不是只看静态 `BANKROLL_USD`
- 启动恢复、pending 订单 reconcile、`SIGTERM` / `Ctrl+C` 清理路径已经具备基础实现

## 存储边界

主策略与套利 sidecar 的存储边界需要明确区分：

- 主策略以 `Postgres` 为唯一真源
- 主策略账户、持仓、订单、动作事件、模式状态、权益快照都以数据库为准
- 套利 sidecar 仍然是可选的模拟模块
- 套利 sidecar 目前继续使用 `bot/data/arbitrage_trades.jsonl`
- `bot/data/arbitrage_trades.jsonl` 不应被误写成“主策略旧状态文件”

## 本次 Review Findings

评审日期：`2026-05-05`

### Finding 1：启动阶段的数据库就绪性检查还不够严格

问题：

- 之前的启动检查只验证 `SELECT 1`
- 如果表结构缺失、启动写入失败、或启动恢复失败，bot 仍可能继续运行
- 这与“主策略以 `Postgres` 为唯一真源”的约束冲突

影响：

- 可能出现 bot 在没有可靠持久化的情况下继续跑
- dashboard、恢复逻辑、数据库事实会和运行内存脱节

### Finding 2：pending exit order 进入 `rejected` 后可能卡住退出流程

问题：

- `process_pending_orders()` 把 `rejected` 视为 terminal status
- 但旧逻辑没有把该订单从 `self.pending_orders` 中清掉
- 结果是后续平仓逻辑会误以为“仍有一个待完成的退出单”

影响：

- 持仓可能无法继续发起新的退出单
- 风控行为会被假性 pending 状态阻塞
- dashboard / 内存状态会出现偏差

### Finding 3：计划文档本身已经过期

之前的计划里仍然保留了多条已经完成、甚至与当前代码相反的描述，例如：

- “`paper mode` 在美国环境会直接退出”
- “dashboard 只展示套利模拟结果”
- “还没有 requested/effective mode”
- “还没有 `Postgres` 主存储”
- “资金池还是静态 `BANKROLL_USD`”

这些内容需要从计划中删除，避免后续误判仓库状态。

## 已完成修复记录

### 第一轮（Finding 1-3）

1. 启动阶段新增数据库 schema 就绪性检查。
2. 启动写入与启动恢复改为 fail fast。
3. `rejected` 的 pending exit order 会被正确清理。
4. README 已同步。

### 第二轮（2026-05-05）

5. **运行中关键写路径失败策略**（Next Step 1）
   - 新增 `Bot._db_write_critical(label, exc)` 辅助方法
   - `buy_filled`（订单成交、持仓已开仓但 DB 写入失败）：live 模式触发 `SafetyShutdown`；paper 模式记录 error 并继续
   - `close_position`（持仓已平仓但 DB 写入失败）：同上分级
   - 其余非关键写路径（entry_rejected、exit_rejected、cancel_reprice 等）维持 warning 记录后继续
   - 对应单元测试已覆盖 live / paper 两种模式

6. **测试覆盖补齐**（Next Step 2 部分）
   - 新增 `TestLiquidityGuard`：PaperTrader 恒返回 True；LiveTrader 在空 order book 时返回 False；异常时 fail open
   - 新增 `TestDbWriteCriticalPolicy`：live 模式抛 SafetyShutdown，paper 模式只记录 error
   - 现有 `test_process_pending_orders_clears_rejected_exit_orders` 已覆盖 rejected 清理路径

7. **套利 sidecar 决策**（Next Step 3）：保留为文件制独立模拟模块，不迁入 Postgres
   - `MIN_VOLUME_USD` 从 50000 降为 500（适配 Kalshi 政治类市场低成交量情况）

8. **order book 流动性守卫**（PR review fix）
   - `PaperTrader.check_entry_liquidity` / `LiveTrader.check_entry_liquidity` 新增
   - 在 `execute_trade()` 中 `place_market_buy` 前调用，空 order book 时跳过入场

## 当前剩余问题

### 1. 主策略查询还没有按 `run_label` / 多账户做更细粒度隔离

现状：

- 当前默认按单账户 `main` 工作
- dashboard 汇总查询更接近”单实例默认部署”

如果后续要支持：

- 多 bot 实例
- 多账户
- 多 run label 并行回放

则需要重新定义 dashboard 和 summary 的查询边界。

### 2. 缺少基于真实 Postgres 的集成测试

现状：

- 现有测试以纯单元测试为主（全部 mock DB 层）
- 启动恢复、live protection -> flat -> fallback 的端到端链路暂无集成测试覆盖

后续需要：

- 增加带依赖的测试环境说明（本地 `docker compose up -d postgres`）
- 增加基于本地 Postgres 的启动与恢复集成测试

## Railway 单服务合并部署方案（2026-05-06）

当前部署目标调整为：优先支持 `Railway`，不再把 `Vercel` 作为整个 trading bot 的主部署目标。

方案摘要：

- 使用 `1` 个 `Railway` Web Service 承载 dashboard 和 bot 运行入口
- 使用 `1` 个 `Railway PostgreSQL` 服务承载主策略唯一真源
- dashboard 对外提供 HTTP 访问
- bot 在同一服务内作为后台线程或同进程后台任务启动
- 不把“完全免费、24/7 常驻运行”作为该方案的成立前提

这样选的原因：

- 当前 bot 是常驻轮询 + WebSocket + 后台线程模型，更接近 `Railway` 的持久化 service，而不是 `Vercel` 的请求驱动函数模型
- 当前 dashboard 既读 `Postgres`，也会读本地 `bot/logs/bot.log` 与 `bot/data/arbitrage_trades.jsonl`
- 如果拆成两个应用服务，本地文件共享会立刻变成额外问题
- 单服务合并部署可以先把部署复杂度压到最低，再决定是否值得继续拆分

该方案的边界与代价：

- Web 服务重启、重新部署、崩溃时，会连带重启 bot
- 必须保证云上始终只有一个 bot 实例启动，不能因为 worker / replica 配置导致重复运行
- 主策略状态仍然必须只以 `Postgres` 为准，不能把本地文件重新变成真源
- `bot/logs/bot.log` 与 `bot/data/arbitrage_trades.jsonl` 在云环境里只能视为“尽力而为的本地副产物”，不能依赖其跨部署持久化
- 如果后续坚持“零费用长期在线”，则需要改走自托管路线；当前 `Railway` 方案不以此为目标

本地兼容约束：

- Railway 改造不能替换当前本地开发入口，只能新增云部署入口
- 本地 `docker compose up -d postgres` 路径必须继续可用
- 本地 `python3 bot/main.py` 直跑路径必须继续可用
- 本地 `python3 dashboard.py` 直跑路径必须继续可用
- 云端使用的 `HOST`、`PORT`、bot 自动启动开关必须通过环境变量控制，并保留适合本地开发的默认值

阶段性成功标准：

- 单个 `Railway` 服务启动后，dashboard 可以对外访问
- 同一服务启动后，bot 会自动开始扫描并持续写入 `Postgres`
- `SIGTERM` / 部署重启时，bot 仍沿用现有清理路径
- dashboard 在不依赖本地旧文件的前提下，仍能展示主策略核心视图

## Railway 下一步修改方案

### Step 1：增加统一云入口

- 新增单一入口文件，例如 `app.py`
- 该入口负责启动 Flask dashboard，并在后台启动 bot
- 该入口是新增入口，不替换现有 `bot/main.py` 和 `dashboard.py` 的本地直跑方式
- 保持当前 `bot/main.py` 的核心逻辑不直接揉进 dashboard 文件，避免把已有模块结构打散
- 明确 bot 只能启动一次，不能因为模块导入、副本扩容或 worker 机制被重复拉起

### Step 2：把 dashboard 改成可部署的生产入口

- 不再只监听 `127.0.0.1:5000`
- 改为监听 `0.0.0.0` 和 `PORT`
- 增加 `healthz` 类健康检查接口，供 `Railway` healthcheck 使用
- 关闭 `debug=True`
- 保持当前模板与 JSON API 行为不变，优先减少 UI 层改动

### Step 3：补齐单服务启动保护

- 增加显式环境变量开关，例如是否自动启动 bot
- 明确部署时只允许单实例 / 单 replica 运行 bot
- 为后续可能的多实例部署提前标记风险：当前 dashboard 查询、`account_key=main`、本地 sidecar 文件都默认单实例语义

### Step 4：收紧云环境下的存储边界

- 继续坚持主策略只以 `Postgres` 为唯一真源
- dashboard 对本地日志和 `arbitrage_trades.jsonl` 的读取改成可降级能力，而不是部署前提
- 如果本地文件缺失，主策略核心 dashboard 仍应正常工作
- 后续再决定是否把套利 sidecar 数据迁入 `Postgres`，当前不把它作为 Railway 首次上线阻塞项

### Step 5：补齐 Railway 部署资产与运行说明

- 增加 Railway 所需启动方式与部署说明
- 选择适合单进程单实例语义的 Python Web server 方案，避免多 worker 触发重复 bot
- 明确 `DATABASE_URL` 直接接 Railway Postgres 提供的连接串
- 在文档中补齐环境变量、healthcheck、启动命令、以及单实例约束

### Step 6：补齐部署相关测试与验证

- 增加最小化的启动验证，确保统一入口启动时 dashboard 与 bot 都能初始化
- 增加对 `PORT`、healthcheck、以及 bot 自动启动开关的测试
- 保留现有本地 `docker compose` + Postgres 验证路径，避免云部署改造破坏本地开发

建议的实际落地顺序：

1. 先改统一入口与 dashboard 监听方式。
2. 再补 bot 单实例启动保护。
3. 然后处理本地文件降级与 Railway 启动方式。
4. 最后更新 `README.md` 与补测试。

## 本文档维护规则

从现在开始，这份计划只保留三类内容：

- 当前真实状态
- 新发现的问题
- 下一步可执行任务

以下内容不再保留：

- 已经完成的历史性待办
- 与代码现状冲突的旧结论
- 把主策略和套利 sidecar 混写在一起的描述

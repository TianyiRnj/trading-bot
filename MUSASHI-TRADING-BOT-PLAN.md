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

## 本文档维护规则

从现在开始，这份计划只保留三类内容：

- 当前真实状态
- 新发现的问题
- 下一步可执行任务

以下内容不再保留：

- 已经完成的历史性待办
- 与代码现状冲突的旧结论
- 把主策略和套利 sidecar 混写在一起的描述

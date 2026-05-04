# Musashi Trading Bot Plan

## 目标

把当前仓库从“可运行的自动交易原型”推进到“可以长期运行、可复盘成绩、具备上线前检查能力的交易 bot”。

目标场景：

- 用户可以给 bot 一个明确初始资金，例如 `10 USD` 或 `100 USD`
- 主存储使用 `Postgres`，作为账户状态、持仓、订单、交易事件的唯一真源
- bot 可以持续运行并自动处理开仓、持仓、平仓
- 用户在运行期间和一段时间后都可以实时查看：
  - 总收益
  - 已实现收益
  - 未实现收益
  - 当前持仓
  - 交易次数
  - 胜率
  - 资金曲线
  - 所有交易动作明细
- 交易动作需要单独列出并可实时查看，至少包括：
  - 买进提交
  - 买进成交
  - 卖出提交
  - 卖出成交
  - pending
  - 部分成交
  - 取消
  - 改价
  - 改金额
  - 拒单
  - 完整平仓
- 当用户请求 `live mode` 但出现以下任一情况时，bot 需要自动降级到 `paper mode` 并继续运行：
  - 缺少 Polymarket API 凭证
  - API 凭证无效
  - 账户余额不足或无法下单
  - 地区被 ban / geoblock
  - live 初始化失败或被风控拒绝
- bot 停止前和重启后需要保证状态连续：
  - 可捕获退出时先缓存
  - 下次启动时恢复上次持仓、挂单、资金状态、交易动作
- bot 在 `paper mode` 和 `live mode` 下都具备清晰、可验证的行为

## 当前状态结论

当前项目已经具备以下基础能力：

- 轮询 Musashi feed 并分析交易信号
- 基于置信度、edge、流动性、价格区间筛选 Polymarket 市场
- 自动开仓
- 基于止盈、止损、最大持仓时间、信号反转自动平仓
- 持久化本地持仓、待处理订单、已见事件
- 在 `live mode` 下接入 Polymarket 下单接口

当前项目尚未完全达到目标，主要原因如下：

1. `paper mode` 目前也会触发地区限制检查，在美国环境下直接退出，无法长期稳定运行。
2. 当前 dashboard 只展示套利模拟结果，不展示主交易策略的真实运行结果。
3. `BANKROLL_USD` 更像静态风控上限，不是会随收益变化自动更新的真实资金池。
4. 当前缺少一个面向主策略的完整实时业绩视图，无法方便回答“现在表现如何、刚刚发生了什么交易动作”。
5. `live mode` 虽然具备下单路径，但还缺少更完整的上线前检查、运行监控与失败恢复说明。
6. 当前计划还没有把“live 不可用时自动降级为 paper”定义成明确运行规则。
7. 当前计划还没有把“退出前缓存、重启后恢复、SIGKILL 边界”定义完整。
8. 当前运行态仍以本地 `JSON / JSONL` 文件为主，还没有 `Postgres` 作为统一主存储。
9. 目标方案不需要迁移旧文件数据；旧文件应保持为空或停止使用，直接由 `Postgres` 承接主运行态。

## 核心问题

### 问题 1：`paper mode` 无法稳定长期运行

现状：

- 启动时会执行地区与 geoblock 检查
- 当前限制列表包含 `US`
- 即使是 `paper mode` 也会在启动时直接停止

影响：

- 无法先做低风险长期模拟
- 无法积累主策略交易记录
- 无法验证“挂着跑一段时间后再看成绩”的核心体验

### 问题 1.5：`live mode` 缺少自动降级到 `paper mode` 的运行策略

现状：

- 当前模式切换主要依赖固定 `BOT_MODE`
- 没有把 “requested mode” 和 “effective mode” 区分开
- 遇到 API 缺失、API 无效、余额不足、地区被 ban 等情况时，缺少统一降级策略

影响：

- 用户请求 live 时可能直接失败退出，而不是继续以 paper 运行
- 无法保证 bot 尽量持续工作
- dashboard 和日志也无法明确说明“为什么现在是 paper”

### 问题 2：主策略缺少成绩看板

现状：

- 主策略会写入 `bot/data/trades.jsonl`
- 但现有 Flask dashboard 只读取 `arbitrage_trades.jsonl`
- 主策略没有自己的实时收益汇总页或 API
- 当前没有把订单生命周期动作拆成独立可视事件

影响：

- 用户无法直观看到主策略是否赚钱
- 用户无法实时看到刚刚发生的买入、卖出、挂单、取消、改价、改量
- 无法快速复盘最近交易
- 无法区分主策略与套利模拟策略的结果

### 问题 2.5：交易动作缺少统一事件模型

现状：

- 当前交易记录更偏向结果记录，不是完整事件流
- 同一笔交易从提交到完成的中间状态还没有统一建模
- `pending`、取消、改价、改量等动作没有独立视图

影响：

- 用户无法实时判断 bot 当前到底在“已下单、等待成交、部分成交、取消后重挂、还是已完成”
- dashboard 很难准确展示订单生命周期
- 后续统计、复盘、告警会缺少统一数据基础

### 问题 2.6：退出持久化与启动恢复边界没有定义完整

现状：

- 当前有本地状态文件，但没有把所有退出路径下的保存策略写成明确方案
- 当前没有明确区分“可捕获退出”和“不可捕获退出”的处理边界
- 当前没有把下次启动时的恢复范围列完整

影响：

- 用户不清楚 `Ctrl+C`、正常退出、`kill <PID>`、关机时应该期待什么恢复结果
- 如果运行中断，可能不清楚哪些状态一定会保留
- 后续实现容易对 `SIGKILL` 这类不可捕获退出做出不现实承诺

### 问题 2.7：缺少 `Postgres` 作为统一主存储

现状：

- 当前状态主要散落在 `positions.json`、`pending_orders.json`、`seen_event_ids.json`、`trades.jsonl`
- 当前 dashboard 也主要围绕文件读取
- 当前没有事务型数据库来统一保存账户、订单、事件和恢复状态

影响：

- 查询订单生命周期、实时统计、恢复状态都需要拼接多个文件
- 故障恢复和一致性校验成本更高
- 后续实时 dashboard、策略统计、模式切换记录不够稳固
- 如果继续保留旧文件作为兼容真源，会让实现复杂度和错配风险显著上升

### 问题 3：资金池没有随着盈亏动态变化

现状：

- 当前剩余资金主要通过“初始资金 - 当前持仓敞口”估算
- 已实现盈利没有自动回流到可用资金
- 亏损也不会明确压缩后续可交易资金

影响：

- `10 USD` 或 `100 USD` 的资金设定不够真实
- 长时间运行后的仓位管理与真实资金增长不一致
- 收益率、回撤等指标难以准确计算

### 问题 4：`live mode` 的可操作性还不够完整

现状：

- 已有真实下单接口
- 已有挂单状态跟踪和重定价逻辑
- 但缺少更清晰的 live readiness 流程与检查项

影响：

- 容易出现“代码能下单，但不够放心上线”的情况
- 实盘运行问题定位成本高
- 用户难以判断什么时候可以从 `paper mode` 切换到 `live mode`

## 分阶段实施计划

## Phase 1：让主策略的模式管理真正可用

目标：

- 让 bot 可以在允许的本地开发/测试场景下稳定进入主循环
- 支持持续运行并生成主策略交易记录
- 让 `live` 请求在不可用时自动降级为 `paper`

计划改动：

1. 引入 `requested_mode` 与 `effective_mode`
   - 用户配置的模式作为 `requested_mode`
   - 实际运行的模式作为 `effective_mode`
   - dashboard、日志、状态文件都要显示这两个值

2. 定义 `live -> paper` 自动降级规则
   - 缺少 API 凭证时自动降级
   - API 凭证无效时自动降级
   - 余额不足或无法下单时自动降级
   - 地区被 ban / geoblock 时自动降级
   - live 初始化失败、鉴权失败、合规拒绝时自动降级

3. 调整运行安全策略
   - 让 `paper mode` 与 `live mode` 区分处理
   - 保留 `live mode` 的严格地区限制检查
   - 为 `paper mode` 提供更合理的安全策略

4. 明确 `paper mode` 的启动行为
   - 如果外部 geolocation 服务失败，不应直接让模拟模式彻底不可用
   - 允许模拟模式继续运行，或提供可配置的严格/宽松策略

5. 增强日志可读性
   - 明确记录 `requested_mode`
   - 明确记录 `effective_mode`
   - 明确记录降级原因
   - 明确记录某次跳过安全检查的原因
   - 明确记录 bot 已成功进入主循环

6. 明确旧文件处理规则
   - `bot/data/` 下旧 JSON / JSONL 文件不参与新方案主流程
   - 不做旧文件导入
   - 旧文件应保持为空、停止写入，或仅作为人工调试时的临时导出目标

交付结果：

- `paper mode` 在开发环境可以连续运行
- 主交易策略可以开始积累真实模拟记录
- live 不可用时，bot 会自动切到 paper 继续运行

验收标准：

- 在 `BOT_MODE=paper` 下，bot 不因美国地区检测而立即退出
- 在用户请求 `BOT_MODE=live` 且 live 条件不满足时，bot 自动降级到 `paper`
- 日志和 dashboard 能明确显示降级原因
- bot 成功进入轮询主循环
- 产生主策略交易记录文件

## Phase 2：引入 `Postgres` 主存储、实时业绩统计与交易事件模型

目标：

- 用 `Postgres` 作为 bot 的唯一主存储
- 让用户可以实时看到主策略成绩
- 让用户可以实时看到每个订单和交易动作的生命周期

计划改动：

1. 定义 `Postgres` 连接与运行方式
   - 使用 `DATABASE_URL`
   - 定义开发环境与生产环境连接方式
   - 明确连接池、超时、重连策略
   - 明确 schema migration 方式

2. 设计 `Postgres` 表结构
   - `account_state`
   - `positions`
   - `orders`
   - `trade_events`
   - `seen_events`
   - `mode_state`
   - 视需要增加 `price_snapshots` 或 `equity_snapshots`
   - 为 `order_id`、`market_id`、`status`、`created_at`、`action_type` 建索引

3. 定义存储分工
   - `Postgres` 作为唯一真源
   - `bot.log` 继续保留为文本日志
   - 现有 `JSON / JSONL` 文件不再作为迁移来源或兼容真源
   - `bot/data/` 目标状态应为空或不再参与主流程

4. 定义初始化与启动引导
   - 首次引入 `Postgres` 时不导入现有 `JSON / JSONL`
   - 以空库初始化作为标准启动路径
   - 明确 schema 初始化流程
   - 明确旧文件保持为空或停止使用

5. 定义主策略业绩模型
   - 初始资金
   - 当前可用资金
   - 当前持仓市值
   - 已实现收益
   - 未实现收益
   - 总资产
   - 总收益率
   - 交易次数
   - 胜率
   - 平均单笔收益
   - 最大回撤
   - 实时更新时间
   - 当前 open orders 数量
   - 当前 pending orders 数量

6. 定义两种执行模式
   - `direct_execution`：按当前可成交价格直接交易
   - `quoted_execution`：先给出报价或挂限价单，等待价格涨上来或降下去后成交
   - `quoted_execution` 在未完成时允许：
     - 保持 pending
     - 撤单
     - 改价
     - 改金额
     - 重新挂单

7. 统一交易记录结构
   - 把“交易结果记录”升级为“交易事件流”
   - 为每个动作定义统一字段，例如：
     - `event_id`
     - `timestamp`
     - `strategy`
     - `mode`
     - `market_id`
     - `token_id`
     - `order_id`
     - `parent_order_id`
     - `action_type`
     - `status`
     - `requested_shares`
     - `filled_shares`
     - `remaining_shares`
     - `requested_price`
     - `executed_price`
     - `requested_value_usd`
     - `executed_value_usd`
     - `execution_mode`
     - `reason`
     - `metadata`
   - 明确以下 action_type：
     - `quote_submitted`
     - `buy_submitted`
     - `buy_filled`
     - `sell_submitted`
     - `sell_filled`
     - `pending`
     - `partial_fill`
     - `cancel_requested`
     - `canceled`
     - `reprice_requested`
     - `repriced`
     - `amount_modified`
     - `quote_expired`
     - `rejected`
     - `position_closed`
   - 使后续统计逻辑、dashboard 和告警都依赖统一事件模型而不是临时推断

8. 新增主策略统计函数
   - 基于 `Postgres` 表计算汇总指标
   - 计算当前未实现盈亏
   - 汇总每个市场、每笔交易、每个时间段的表现
   - 汇总每类 action 的次数与最近状态
   - 允许按 `order_id` 回放订单生命周期
   - 允许区分 `direct_execution` 与 `quoted_execution` 的表现

9. 输出主策略统计 API
   - 新增主策略 `metrics` endpoint
   - 新增主策略 `positions` endpoint
   - 新增主策略 `orders` endpoint
   - 新增主策略 `actions` endpoint
   - 新增主策略 `recent-events` endpoint
   - 视需要增加轮询或流式接口，例如 SSE / WebSocket，用于实时刷新

10. 定义性能与写入策略
   - 关键交易状态写入使用小事务，保证一致性
   - 查询走索引，避免 dashboard 扫全表
   - 需要时增加汇总表或快照表
   - `Postgres` 的写入延迟预计远低于外部 API 延迟，不应成为主要交易瓶颈

11. 定义实时查看的最小刷新模型
   - 概览指标按固定间隔刷新
   - 当前持仓按固定间隔刷新
   - 订单动作列表按更短间隔刷新
   - 如果实现流式能力，动作时间线应在事件产生后立即更新

交付结果：

- 项目具备主策略成绩统计能力
- 项目具备统一交易事件流
- 项目具备 `Postgres` 主存储
- 用户可以实时看到“现在赚了多少、有哪些持仓、刚刚发生了什么动作、哪些订单还在 pending”

验收标准：

- 能从 `Postgres` 正确生成主策略统计结果
- 收益指标可以区分已实现与未实现部分
- 持仓与交易记录能够相互对上
- 每笔订单都能看到从提交到结束的动作链路
- `pending`、取消、改价、改金额等动作可以被单独查询和展示
- `quoted_execution` 的未成交订单可以被单独追踪、撤单、改价、改金额并记录结果
- 查询性能在单实例 bot 场景下不成为主要瓶颈

## Postgres 数据结构（V1）

参考初始化文件：

- `docker-compose.yml`
- `db/init/001_init.sql`

### 1. `mode_state`

用途：

- 记录每次 bot 启动时的模式状态
- 区分 `requested_mode` 和 `effective_mode`
- 记录自动降级原因、运行状态、退出原因

关键字段：

- `id`
- `run_label`
- `requested_mode`
- `effective_mode`
- `fallback_reason`
- `status`
- `started_at`
- `stopped_at`
- `last_heartbeat_at`
- `last_shutdown_reason`

### 2. `account_state`

用途：

- 保存账户当前真值状态
- 作为 dashboard 概览和风控 sizing 的主要来源

关键字段：

- `account_key`
- `requested_mode`
- `effective_mode`
- `cash_balance`
- `positions_value`
- `realized_pnl`
- `unrealized_pnl`
- `equity`
- `max_equity`
- `drawdown`
- `updated_at`

### 3. `positions`

用途：

- 保存当前和历史持仓
- 跟踪持仓从开仓到平仓的生命周期

关键字段：

- `position_id`
- `market_id`
- `condition_id`
- `token_id`
- `title`
- `side`
- `status`
- `requested_mode`
- `effective_mode`
- `entry_order_id`
- `entry_price`
- `entry_value_usd`
- `shares`
- `realized_pnl`
- `unrealized_pnl`
- `opened_at`
- `closed_at`
- `updated_at`
- `metadata`

### 4. `orders`

用途：

- 保存所有下单请求和真实订单状态
- 支持直接成交和报价挂单两种执行模式

关键字段：

- `order_id`
- `parent_order_id`
- `position_id`
- `market_id`
- `condition_id`
- `token_id`
- `side`
- `execution_mode`
- `status`
- `requested_mode`
- `effective_mode`
- `requested_price`
- `executed_price`
- `requested_value_usd`
- `executed_value_usd`
- `requested_shares`
- `filled_shares`
- `remaining_shares`
- `fallback_reason`
- `created_at`
- `updated_at`
- `closed_at`
- `metadata`

### 5. `trade_events`

用途：

- 保存所有交易动作事件
- 作为实时动作时间线、订单复盘、告警和统计的基础

关键字段：

- `event_id`
- `order_id`
- `position_id`
- `market_id`
- `condition_id`
- `token_id`
- `action_type`
- `status`
- `requested_mode`
- `effective_mode`
- `execution_mode`
- `requested_price`
- `executed_price`
- `requested_value_usd`
- `executed_value_usd`
- `requested_shares`
- `filled_shares`
- `remaining_shares`
- `reason`
- `metadata`
- `created_at`

关键 `action_type`：

- `quote_submitted`
- `buy_submitted`
- `buy_filled`
- `sell_submitted`
- `sell_filled`
- `pending`
- `partial_fill`
- `cancel_requested`
- `canceled`
- `reprice_requested`
- `repriced`
- `amount_modified`
- `quote_expired`
- `rejected`
- `position_closed`

### 6. `seen_events`

用途：

- 保存已经处理过的 feed 事件
- 防止重复处理和重复下单

关键字段：

- `source_event_id`
- `source_type`
- `signal_type`
- `payload`
- `seen_at`

### 7. `equity_snapshots`

用途：

- 保存账户净值快照
- 为资金曲线和历史回撤提供数据

关键字段：

- `account_key`
- `cash_balance`
- `positions_value`
- `realized_pnl`
- `unrealized_pnl`
- `equity`
- `captured_at`

## 核心流程（V1）

### 1. 启动流程

1. 读取 `.env`
2. 初始化日志
3. 连接 `Postgres`
4. 读取 `requested_mode`
5. 执行 `live` 可用性检查
6. 得出 `effective_mode`
7. 把模式状态写入 `mode_state`
8. 从 `Postgres` 恢复 `account_state`、`positions`、`orders`、`trade_events`
9. 对恢复出的 open / pending 订单执行 reconcile
10. 启动主循环

### 2. 信号处理流程

1. 拉取 Musashi feed
2. 过滤已处理的 `seen_events`
3. 对新文本调用 `analyze_text`
4. 基于置信度、edge、流动性、价格范围做筛选
5. 生成交易决策
6. 把 feed 事件写入 `seen_events`

### 3. `direct_execution` 流程

1. 创建订单记录
2. 写入 `buy_submitted` 或 `sell_submitted`
3. 直接按当前可成交价格发单
4. 如果成交：
   - 更新 `orders`
   - 更新 `positions`
   - 更新 `account_state`
   - 写入 `buy_filled` / `sell_filled`
5. 如果失败：
   - 更新 `orders`
   - 写入 `rejected`
   - 如果是 `live` 且触发降级条件，切换为 `paper`

### 4. `quoted_execution` 流程

1. 创建报价订单记录
2. 写入 `quote_submitted`
3. 进入等待状态并写入 `pending`
4. 定时检查当前价格与目标价格
5. 如果价格满足：
   - 成交
   - 更新 `orders`
   - 更新 `positions`
   - 更新 `account_state`
   - 写入 `buy_filled` / `sell_filled`
6. 如果长时间未完成：
   - 撤单时写入 `cancel_requested` / `canceled`
   - 改价时写入 `reprice_requested` / `repriced`
   - 改金额时写入 `amount_modified`
   - 重新挂单后重新进入 `pending`

### 5. 持仓监控流程

1. 读取当前持仓
2. 计算实时价格
3. 判断止盈、止损、最大持仓时间、信号反转
4. 如需退出，创建卖出订单
5. 更新 `positions`、`orders`、`trade_events`
6. 更新 `account_state`
7. 写入 `equity_snapshots`

### 6. 退出与恢复流程

1. 运行中每次关键状态变化立即写入 `Postgres`
2. 周期性写入 `equity_snapshots`
3. 收到 `Ctrl+C`、正常退出或 `SIGTERM` 时执行最终 flush
4. 下次启动时先从 `Postgres` 恢复状态
5. 对未完成订单继续追踪、撤单、改价或完成
6. `SIGKILL` 无法执行最终 flush，只能依赖之前已经写入的数据

## 实施步骤（执行版）

### Step 1：固定存储边界

- 明确 `Postgres` 是唯一真源
- 明确 `bot/data/` 旧文件不做迁移、不做回填、不做双写
- 明确 `bot.log` 继续作为文本日志保留

### Step 2：完成模式控制

- 实现 `requested_mode`
- 实现 `effective_mode`
- 实现 `live -> paper` 自动降级
- 记录降级原因

### Step 3：接入 Postgres 基础设施

- 配置连接层
- 建立 schema / migration
- 建立核心 repository
- 建立启动时数据库可用性检查

### Step 4：切换主运行态到 Postgres

- 持仓改写入 `positions`
- 订单改写入 `orders`
- 动作改写入 `trade_events`
- 账户状态改写入 `account_state`
- feed 去重改写入 `seen_events`

### Step 5：补齐恢复与退出

- 启动时从 `Postgres` 恢复状态
- 对 open / pending 订单做 reconcile
- 支持 `Ctrl+C` / `SIGTERM` 最终 flush
- 用即时写库兜底 `SIGKILL`

### Step 6：补齐交易执行生命周期

- 实现 `direct_execution`
- 实现 `quoted_execution`
- 支持 pending、撤单、改价、改金额、重新挂单
- 保证每一步都写入 `trade_events`

### Step 7：补齐实时查看

- 输出 metrics / positions / orders / actions API
- dashboard 改从 `Postgres` 查询
- 展示资金曲线、持仓、订单、动作时间线

### Step 8：补齐风控、测试与文档

- 重构动态资金模型
- 增强 live readiness
- 补测试
- 收口 README 和配置说明

## Phase 3：新增主策略实时 dashboard

目标：

- 让用户不用手翻数据库或 JSON 文件就能实时查看 bot 表现和订单动作

计划改动：

1. 改造现有 Flask dashboard
   - 不再只服务套利模拟
   - 增加主策略视图
   - 视情况保留套利视图作为单独模块
   - 支持实时刷新
   - dashboard 主查询源改为 `Postgres`

2. 设计主策略页面
   - 账户概览
   - 当前持仓列表
   - open orders / pending orders 列表
   - 最近交易
   - 实时交易动作时间线
   - 收益统计
   - 最近日志

3. 增加关键可视化
   - 总资产变化
   - 已实现收益曲线
   - 未实现收益变化
   - 胜负分布
   - 按市场分类的收益统计
   - 按 action_type 分类的动作统计

4. 标注模式与数据来源
   - 明确区分 `paper mode` 和 `live mode`
   - 明确页面当前看的是什么策略数据

5. 设计交易动作面板
   - 单独显示已完成动作
   - 单独显示买进动作
   - 单独显示卖出动作
   - 单独显示 pending 动作
   - 单独显示取消动作
   - 单独显示改价动作
   - 单独显示改金额动作
   - 支持按市场、订单、状态、动作类型筛选
   - 支持点开单笔订单查看完整动作链路

6. 设计执行模式与订单详情面板
   - 单独标记 `direct_execution` 与 `quoted_execution`
   - 对报价单展示目标价格、当前价格、等待方向、剩余数量
   - 对未完成报价单展示下一步动作，例如继续等待、撤单、改价、改金额

交付结果：

- 用户可以通过本地 dashboard 实时查看主策略成绩
- 用户可以实时查看每一笔交易和订单动作

验收标准：

- 打开 dashboard 后能直接看到主策略关键指标
- 最近交易与 `Postgres trade_events` 一致
- 页面明确显示当前模式和数据来源
- 动作面板能够单独列出 `买进 / 卖出 / pending / 取消 / 改价 / 改金额 / 已完成`
- 新事件产生后页面能在可接受延迟内刷新出来
- 报价单的等待中状态、修改动作、取消动作都能单独看到

## Phase 4：补齐退出持久化与启动恢复

目标：

- 让 bot 在退出和重启之间保持状态连续
- 让用户明确知道哪些退出路径可以保证最终 flush，哪些只能依赖即时落盘

计划改动：

1. 定义持久化策略
   - 每次关键状态变化立即写入 `Postgres`
   - 增加周期性快照
   - 持久化范围至少包括：
     - 账户状态
     - 当前持仓
     - open / pending orders
     - 交易事件流
     - 最近模式与降级原因

2. 定义可捕获退出的 flush 行为
   - `Ctrl+C` / `SIGINT`
   - 正常程序退出
   - `kill <PID>` 对应的 `SIGTERM`
   - 系统关机时若进程收到可捕获终止信号，也应执行最终 flush

3. 明确不可捕获退出边界
   - `kill -9` / `SIGKILL` 无法执行最终 flush
   - 对这类退出不承诺“最后一刻再保存”
   - 通过“关键状态即时写入 `Postgres` + 周期快照”尽量减少丢失

4. 定义启动恢复范围
   - 从 `Postgres` 读取账户状态
   - 从 `Postgres` 读取当前持仓
   - 从 `Postgres` 读取 pending / open orders
   - 从 `Postgres` 读取交易事件流
   - 从 `Postgres` 读取最近 `requested_mode` / `effective_mode`

5. 定义恢复后的 reconcile 流程
   - 启动时先从 `Postgres` 恢复状态
   - 然后与外部交易状态或 paper 状态做 reconcile
   - 恢复后继续追踪上次未完成的报价单和 pending 订单

交付结果：

- 可捕获退出路径下，bot 能在退出前保存最终状态
- 下次启动时，bot 能恢复上次的交易上下文并继续运行

验收标准：

- `Ctrl+C` 后重启，能看到上次持仓和 pending 订单
- `kill <PID>` 后重启，能看到上次持仓和 pending 订单
- 未完成的报价单会在重启后继续被追踪或 reconcile
- 文档明确说明 `SIGKILL` 只能依赖即时落盘兜底，不能承诺最终 flush

## Phase 5：把资金池从静态风控值升级为真实账户模型

目标：

- 让“给 bot 10 美元或 100 美元”的设定更真实

计划改动：

1. 定义账户状态结构
   - `initial_bankroll`
   - `cash_balance`
   - `realized_pnl`
   - `unrealized_pnl`
   - `equity`
   - `max_equity`
   - `drawdown`

2. 重构仓位 sizing 逻辑
   - 下单不再只看静态 `BANKROLL_USD`
   - 基于当前 `cash_balance` 和 `equity` 计算可下单额度
   - 收益后允许在风控范围内扩大可用资金
   - 亏损后自动收缩

3. 持久化账户状态
   - 写入 `account_state` 表
   - 启动时读取并恢复
   - 平仓后更新

4. 支持收益率指标
   - 绝对收益
   - 收益率
   - 日收益
   - 回撤

交付结果：

- 资金规模与 bot 长期表现绑定
- 用户给 `10 USD` 和 `100 USD` 时，运行结果会真实反映资金变化

验收标准：

- 平仓后账户现金会正确变化
- 当前权益 = 现金 + 持仓浮动价值
- 后续下单额度会随盈亏变化

## Phase 6：提高 `live mode` 上线准备度

目标：

- 把“代码支持 live”提升为“实际更适合上线 live”

计划改动：

1. 补充 live readiness 检查
   - 环境变量完整性检查
   - Polymarket 凭证检查
   - API 可连通性检查
   - 市场解析与 token 解析检查
   - WebSocket 可用性检查

2. 明确 live 风险开关
   - 强制用户显式确认 `BOT_MODE=live`
   - 增加更严格的 dry-run / confirm 配置
   - 限制首次上线时的最大仓位

3. 增强故障恢复
   - 启动时恢复持仓与挂单状态
   - 避免重复提交订单
   - 更清楚地区分“未成交、部分成交、成交失败、被拒绝”

4. 增强运行监控
   - 更清楚的错误日志
   - 关键事件日志
   - 可选告警能力

交付结果：

- `live mode` 的启动风险更低
- 出问题后更容易定位

验收标准：

- 缺少关键配置时能在启动前明确失败
- live 启动流程可以输出完整 readiness 检查结果
- 出现订单异常时可追踪原因

## Phase 7：测试、验证与文档收口

目标：

- 确保改动可维护、可复用、可交接

计划改动：

1. 补充测试
   - 主策略统计测试
   - `Postgres` schema / migration 测试
   - 交易事件流测试
   - 模式自动降级测试
   - 退出持久化与启动恢复测试
   - 报价单 pending / cancel / reprice / resize 测试
   - 账户状态计算测试
   - `paper mode` 安全策略测试
   - dashboard 数据接口测试
   - 实时 actions / orders API 测试

2. 更新 README
   - 增加主策略运行说明
   - 增加主策略 dashboard 说明
   - 增加账户模型说明
   - 增加 `paper` 与 `live` 模式区别
   - 增加 `Postgres` 配置、启动和迁移说明

3. 更新 `.env.example`
   - 新增 `Postgres` 必要配置项
   - 增加注释说明

4. 更新 `.gitignore`
   - 如果新增本地状态文件或导出文件，需要明确忽略策略

交付结果：

- 文档和代码行为一致
- 新用户可以从 README 理解如何运行与查看结果

验收标准：

- README 足以指导从零启动
- 新增测试覆盖关键统计与资金逻辑
- 配置样例与实际代码一致

## 推荐实施顺序

建议按下面顺序推进：

1. 修复模式管理和 `live -> paper` 自动降级
2. 引入 `Postgres` 主存储
3. 补退出持久化与启动恢复
4. 补主策略统计能力与交易事件模型
5. 补主策略 dashboard
6. 重构动态账户模型
7. 增强 `live mode` readiness
8. 最后统一补测试与文档

原因：

- 第 1 步先保证 bot 在 live 不可用时也不会直接废掉
- 第 2 步先把状态真源统一到 `Postgres`
- 第 3 步再保证中断后不会丢掉主要状态
- 第 4 步和第 5 步能尽快回答“这 bot 现在在做什么、到底赚没赚钱”
- 第 6 步才能让 `10 USD` / `100 USD` 的资金概念变得真实
- 第 7 步适合在前面都稳定后再推进，否则 live 风险太高

## 最小可交付版本

如果优先做一个最快能用的版本，建议先完成下面这组：

1. 用户请求 `live` 但 live 条件不满足时，自动降级为 `paper`
2. `paper mode` 不再因美国地区检查直接退出
3. 引入 `Postgres` 并完成核心表初始化
4. 主策略直接写入 `Postgres`
5. 旧 `bot/data/` 文件保持为空或停止使用
6. 对 `Ctrl+C`、正常退出、`kill <PID>` 做退出前 flush，并在下次启动时恢复
7. 支持 `direct_execution` 和 `quoted_execution`
8. 对未完成报价单支持 pending、撤单、改价、改金额
9. dashboard 展示主策略：
   - 总收益
   - 已实现收益
   - 未实现收益
   - 当前持仓
   - 最近交易
   - open orders / pending orders
   - 交易动作时间线
   - 当前 `requested_mode` / `effective_mode`
   - 当前模式
10. 账户资金至少支持：
   - 初始资金
   - 可用现金
   - 已实现收益
   - 当前总权益

完成这一组后，项目就能基本接近你最初的目标：

- 给 bot 一笔初始资金
- 挂着持续运行
- 随时实时查看清晰成绩
- 随时实时查看订单和交易动作
- live 不可用时自动切成 paper 继续跑
- 退出后重启还能接着上次状态继续

## 风险与注意事项

- `live mode` 涉及 Polymarket 地区限制与平台规则，必须继续保留严格防护。
- `paper mode` 放宽检查时，不能误伤 `live mode` 的安全逻辑。
- `live -> paper` 自动降级必须清楚记录原因，避免用户误以为自己还在实盘。
- `Postgres` 不应成为主要性能瓶颈，但需要通过索引、短事务和合理连接池避免把 dashboard 查询拖进交易路径。
- 旧文件既然不迁移也不参与主流程，就要避免任何双写或“数据库失败再退回 JSON”的隐式兼容路径。
- 交易记录结构一旦定下来，后续统计逻辑会依赖它，最好尽早稳定格式。
- 如果 dashboard 同时展示主策略和套利策略，必须明确区分数据来源，避免用户误读。
- 账户模型重构会影响 position sizing、统计口径和 README，需要一起收口。
- 如果要做实时刷新，需要尽早确定是走短轮询、SSE 还是 WebSocket，避免前后端接口反复调整。
- `SIGKILL` 无法执行最终 flush，计划里必须明确通过即时落盘和周期快照兜底，而不是承诺无法实现的“退出前一定保存”。
- 报价单支持改价和改金额后，需要避免重复订单、幽灵挂单和事件流错序。
- 如果 `Postgres` 是远程实例，必须考虑短时网络抖动时的降级策略和重试策略。

## 建议的第一轮实施范围

第一轮最值得做的范围：

1. 做好 `live -> paper` 自动降级
2. 对 `paper mode` 放宽启动安全检查
3. 引入 `Postgres` 主存储与 schema
4. 让主运行态直接写入 `Postgres`
5. 做好退出持久化与启动恢复
6. 新增主策略实时统计逻辑和事件模型
7. 改造 dashboard 以实时展示主策略结果和动作时间线
8. 更新 README 与 `.env.example`

这轮完成后，就能先验证主策略的长期运行表现，再决定是否继续推进动态资金模型和实盘 readiness。

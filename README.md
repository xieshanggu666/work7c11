# 卡牌闯关（Card Run）

基于 **React + Phaser 3 + FastAPI + SQLite** 的浏览器卡牌闯关游戏。

## 功能
- 选择路线（种子地图，3 条节点路径，最终挑战首领）
- **卡牌锻造**：路线上的锻造节点花金币为指定卡牌选择强化分支（锋锐/强效/精炼）；同名卡为独立实例、各自保存成长状态
- **旅途商店**：路线上的商人节点可购买卡牌/遗物，或付费移除指定卡牌实例；库存由种子确定性生成，
  统一事务处理扣款/售罄（409）/失败整体回退（400 零副作用），交易结果带入后续战斗，续局与回放一致
- 构筑牌组、挑战精英/首领，奖励选择影响后续遭遇（遗物加伤、首领血量提升等）
- 卡牌效果统一经 **结算队列** 处理，支持连锁触发、状态叠加、死亡打断
- **战斗演出**：Phaser 场景按服务端结算顺序逐条播放（待机/攻击/受击/死亡动画、护盾与状态实时刷新），
  播放期间操作锁定，播完再应用权威快照同步血量/护盾/手牌，战斗结束衔接领奖
- 服务端权威校验行动，防作弊；失败解锁新卡
- 中途可续局（`POST /api/runs` → 刷新 → 回到同一节点/战斗）；种子回放一致
- **可交互整局回放**：动作日志升级为可 ▶播放 / ⏸暂停 / ◀单步 / 拖拽跳转 / 0.5–4× 倍速 的时间轴，
  逐步重建路线、战斗（Phaser 按结算顺序重演）、锻造与交易状态；记录规则版本与逐步校验点（可检出日志损坏/规则漂移），
  兼容无版本号的旧日志（裸卡牌 id）；回放全程只读隔离——不写存档、战败不发解锁
- 服务端对无限连锁触发设上限防止死循环；领奖/锻造防重复（重复领取返回 409，不重复扣款）
- 旧存档自动兼容：裸 id 牌组在首次载入（续局/行动）时迁移为卡牌实例结构，含战斗中存档

## 目录结构
```
backend/   FastAPI + SQLite（引擎、结算队列、会话、路由、测试）
frontend/  Vite + React + Phaser + zustand
```

## 启动

### 后端（端口 8001）
```bash
cd backend
py -m pip install -r requirements.txt
py run.py
```

### 前端（端口 5173，代理 /api → 8001）
```bash
cd frontend
npm install
npm run dev
```
浏览器打开 http://localhost:5173

## 测试（后端）
```bash
cd backend
$env:PYTHONPATH="."; $env:PYTHONDONTWRITEBYTECODE="1"
py -m pytest -q tests
```
测试覆盖：结算队列（连锁/叠加/死亡打断）、无限连锁封顶、防重复领奖/锻造（含重复扣款）、
失败解锁、种子+动作日志确定性回放、**可交互回放逐帧重建（帧视口/规则版本/校验点/损坏检出/旧日志兼容/只读隔离与解锁隔离）**、
锻造同名卡独立成长、锻造贯通战斗/奖励/续局/回放、旧档迁移、
商店库存确定性/续局一致、购买卡牌/遗物（扣款/售罄/失败回退）、移除指定实例（递增价/牌组下限）、
交易贯通后续战斗与回放。

## API 摘要
- `POST /api/runs {seed?}` 建局
- `GET  /api/runs/{id}/resume` 续局
- `POST /api/runs/{id}/act {action,...}` 行动（choose_node / play / end_turn / claim_reward / forge / shop_buy / shop_remove）
- `GET  /api/runs/{id}/replay` 整局可交互回放：除原 `actions`（兼容）外，返回
  `rules_version` / `recorded_versions` / `legacy` / `initial.checkpoint` / `steps[]` /
  `final_view` / `verification` / `isolated`；每个 `step` 含动作、类型/标题/摘要、
  结算事件 `events`、战斗结果，以及该步完成后的完整只读 `view`（与 `/resume` 同构）
- `GET  /api/cards`、`/api/enemies`、`/api/map-preview?seed=` 元数据

锻造行动：`{action:"forge", card:<卡牌实例 uid>, branch:"sharpen"|"empower"|"refine"}`，
花费 25 金币（`forge_cost` 随视口返回）；同一锻造节点仅可锻造一次，重复请求返回 409 且不扣款，
金币不足/非法卡牌或分支返回 400（校验先于扣款，无副作用）。

商店行动（进入商店节点后视口携带 `shop_available:true` 与 `shop` 库存）：
- 购买：`{action:"shop_buy", kind:"card"|"relic", sku:<货架项 id，如 "card:cleave">}`，
  按货架价格扣款；重复购买/已持有遗物返回 409（售罄，不扣款），金币不足/非法货架返回 400。
- 移除：`{action:"shop_remove", card:<卡牌实例 uid>}`，永久删除该实例（同名卡其余副本不受影响），
  基础价 35 金币，同店每移除一次下一次 +15（`shop.remove.cost`/`next_cost` 随视口返回）；
  牌组仅剩 1 张、金币不足或 uid 非法返回 400。
- 两类交易都走统一事务：先校验后扣款，过程中任何异常把 run 整体回退到交易前快照；
  交易记录保存在 `shop.tx`（含类型/sku 或 uid/价格/余额），离开节点库存清空。
  购入的卡牌/遗物直接进入后续战斗（遗物在战斗初始状态换算，新卡随下次洗牌进三堆）。

## 设计要点
- 所有战斗逻辑在服务端（唯一权威），客户端仅播放服务器返回的结算事件 → 续局/回放天然一致。
- 行动日志为有序序列：结算事件（`action/target/value/source/extra`）+ 控制项（`{"snapshot"}` 权威校正点、
  `{"result":"won|lost|run_won"}` 战斗结束、`enemy_turn` 敌方回合标记含技能名）；`end_turn` 的敌方结算
  事件同样按序入日志，敌人技能的全部效果都会结算（如血裔"吸取"= 伤害 + 自身回血）。
- 前端播放：Phaser 场景维护展示态（血量/护盾/状态随事件逐条推算），日志末尾的权威快照负责校正；
  播放期间到达的快照（刷新/续局）暂存至队列播完后应用，空闲时立即生效。
- 结算队列：事件按插入序稳定解析，效果 → 连锁子事件；目标死亡后定向事件被剔除（死亡打断）。
- 确定性：洗牌与敌人意图均由 `random.Random(seed)` 派生，持久化在 run 状态，回放可复现。
- 可交互回放：在线行动与回放共用纯推演函数 `_apply_action(run, action, map, grant_unlocks)`，
  “玩”和“放”永远是同一套规则。回放从建局初始状态按日志逐步推演，为每步产出一帧只读视口；
  动作事件携带 `ver`（规则版本）与 `ckpt`（权威状态 SHA-256 校验点），每步重演后比对哈希，
  不一致标记 `mismatch`（日志损坏/规则漂移）、无版本号事件标记 `legacy`（旧日志兼容、不校验）。
  回放路径 `grant_unlocks=False` 且不调用任何写库接口：runs/battle_events/profile 均不变，战败不解锁。
  前端 `ReplayPlayer` 提供播放/暂停/单步/跳转/倍速/类型过滤时间轴，战斗帧经同一 Phaser 事件队列重演。
- 卡牌实例：`run.card_instances` 为 `uid -> {id, forges:[分支...]}`，牌组/牌堆/手牌只存 uid；
  引擎在打牌时用 `forging.effective_card(base, forges)` 即时换算生效卡牌（费用/数值），
  因此锻造/商店购卡/商店移除自动贯通战斗结算、续局与回放（初始牌组 7 张，商店可增减）。
- 旧档兼容：缺少 `card_instances` 的存档在首次载入时迁移；战斗中存档按三堆出现序把裸 id
  稳定映射到 uid，洗牌布局与确定性保持不变。
- SQLite：`runs`（状态，含商店库存/交易记录）、`battle_events`（动作日志，含 forge/shop 行）、`profile`（解锁卡）。

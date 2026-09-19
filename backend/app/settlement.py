from __future__ import annotations

"""统一结算队列。

所有卡牌/敌人效果都折叠成 EffectEvent 压入队列，逐个解析。
- 连锁触发：解析一个事件会产出 0..N 个子事件再压入队列（e.g. 回响攻击 -> 再次攻击）。
- 状态叠加：apply_status 按 add/replace/set 规则堆叠；倍率在结算时与加值分开计算。
- 死亡打断：目标死亡后，所有仍指向它的待处理事件被剔除，并触发其 on_death。
- 终止：全局触发计数超过 LIMIT 时中断队列并标记 truncated，防止无限连锁卡死。
"""

# 每场战斗的触发执行上限（防止死循环）
LIMIT = 500


class EffectEvent:
    __slots__ = ("action", "target", "value", "source", "tags", "extra")

    def __init__(self, action, target=None, value=0, source=None, tags=(), extra=None):
        self.action = action
        self.target = target          # 实体 key：'player' | 'enemy'
        self.value = value
        self.source = source          # 触发源 key
        self.tags = list(tags)
        self.extra = extra or {}

    def to_log(self):
        return {
            "action": self.action, "target": self.target, "value": self.value,
            "source": self.source, "tags": self.tags, "extra": self.extra,
        }

    def __repr__(self):
        return f"<EffectEvent {self.action} tgt={self.target} v={self.value}>"


class SettlementQueue:
    def __init__(self, engine, limit=LIMIT):
        self.engine = engine
        self.limit = limit
        self.pending: list[EffectEvent] = []
        self.counter = 0
        self.log: list[dict] = []
        self.truncated = False

    def push(self, *events):
        self.pending.extend(events)

    def run(self):
        """消费 pending 直至清空、目标全部死亡或触发计数达上限。返回过程日志。"""
        while self.pending and self.counter < self.limit:
            ev = self.pending.pop(0)
            if self.engine.is_dead(ev.target):
                continue  # 目标已死，跳过（死亡打断的关键：后续连锁不再作用死目标）
            self.counter += 1
            children = self.engine.resolve(ev)
            self.log.append(ev.to_log())
            if children:
                self.push(*children)
            self.engine.collect_deaths(self)
        if self.counter >= self.limit and self.pending:
            self.truncated = True
            self.pending.clear()
            self.log.append({"action": "truncated", "target": None,
                             "value": LIMIT, "source": None, "tags": ["system"], "extra": {}})
        return self.log
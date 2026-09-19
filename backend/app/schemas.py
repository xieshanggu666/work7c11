from pydantic import BaseModel
from typing import Optional


class CreateRunRequest(BaseModel):
    seed: Optional[int] = None


class ActRequest(BaseModel):
    action: str
    node: Optional[str] = None
    card: Optional[str] = None
    target: Optional[str] = "enemy"
    option: Optional[int] = None
    branch: Optional[str] = None  # forge 动作用：强化分支 id
    kind: Optional[str] = None    # shop_buy 动作用：货架类别 card/relic
    sku: Optional[str] = None     # shop_buy 动作用：货架项 id（如 card:strike）
    req_id: Optional[str] = None  # 幂等令牌：同值重试返回首次结果，不重复生效
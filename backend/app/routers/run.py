from fastapi import APIRouter, HTTPException

from .. import service
from ..schemas import ActRequest, CreateRunRequest

router = APIRouter(prefix="/api/runs", tags=["run"])


@router.get("")
def list_runs():
    return {"message": "not implemented", "runs": []}


@router.post("")
def create_run(body: CreateRunRequest):
    return service.create_run(seed=body.seed)


@router.get("/{run_id}")
def get_run(run_id: str):
    try:
        return service.resume(run_id)
    except service.InvalidAction as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{run_id}/act")
def act(run_id: str, body: ActRequest):
    try:
        return service.act(run_id, body.model_dump())
    except service.DuplicateReward as e:
        # 重复领奖/重复锻造：业务幂等键拦截（含并发情况下后到的请求）
        raise HTTPException(status_code=409, detail=str(e))
    except service.ShopSoldOut as e:
        # 货架售罄/遗物已持有：不扣款
        raise HTTPException(status_code=409, detail=str(e))
    except service.StaleState as e:
        # 状态冲突：客户端基于过期视口提交（或并发请求已推进存档），请刷新重试
        raise HTTPException(status_code=409, detail=str(e))
    except service.InvalidAction as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{run_id}/resume")
def resume(run_id: str):
    try:
        return service.resume(run_id)
    except service.InvalidAction as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{run_id}/replay")
def replay(run_id: str):
    try:
        return service.replay(run_id)
    except service.InvalidAction as e:
        raise HTTPException(status_code=400, detail=str(e))

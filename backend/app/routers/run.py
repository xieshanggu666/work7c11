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
        raise HTTPException(status_code=409, detail=str(e))
    except service.ShopSoldOut as e:
        raise HTTPException(status_code=409, detail=str(e))
    except service.InvalidAction as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{run_id}/resume")
def resume(run_id: str):
    return service.resume(run_id)


@router.get("/{run_id}/replay")
def replay(run_id: str):
    try:
        return service.replay(run_id)
    except service.InvalidAction as e:
        raise HTTPException(status_code=400, detail=str(e))
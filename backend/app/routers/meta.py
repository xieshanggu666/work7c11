from fastapi import APIRouter, HTTPException
from ..cards import all_cards, get_card
from ..enemies import all_enemies
from .. import mapgen

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/cards")
def cards():
    return all_cards()


@router.get("/enemies")
def enemies():
    return all_enemies()


@router.get("/map-preview")
def map_preview(seed: int = 12345):
    return mapgen.generate_map(seed)
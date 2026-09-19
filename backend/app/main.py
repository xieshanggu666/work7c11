from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import db
from .routers import meta, run

app = FastAPI(title="卡牌闯关 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    db.init_db()


app.include_router(meta.router)
app.include_router(run.router)


@app.get("/api/health")
def health():
    return {"ok": True}
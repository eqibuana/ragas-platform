from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import models  # noqa: F401 - registers ORM tables on Base before create_all()
from .config import settings
from .database import Base, engine
from .routers import audit, auth, datasets, runs

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Scenario DB Service")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(datasets.router)
app.include_router(auth.router)
app.include_router(runs.router)
app.include_router(audit.router)


@app.get("/health")
def health():
    return {"status": "ok"}

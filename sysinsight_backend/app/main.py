from fastapi import FastAPI
from app.api.config import router as config_router
from app.api.tune_run import router as tune_run_router

app = FastAPI(
    title="DBTuner API",
    version="1.0.0"
)

# 注册路由
app.include_router(config_router)
app.include_router(tune_run_router)

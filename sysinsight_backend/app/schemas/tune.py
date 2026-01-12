# schemas/tune.py
from pydantic import BaseModel
from typing import Optional, List

class LoadConfig(BaseModel):
    loadType: Optional[str] = None
    duration: Optional[int] = None
    threadNum: Optional[int] = None
    workloadType: Optional[str] = None

class TuneConfig(BaseModel):
    algorithm: Optional[str] = None
    rounds: Optional[int] = None
    metrics: Optional[List[str]] = None
    tuneName: Optional[str] = None

class SetConfigRequest(BaseModel):
    load: Optional[LoadConfig] = None
    tune: Optional[TuneConfig] = None

class StartTuneRequest(BaseModel):
    taskId: str
    taskName: str


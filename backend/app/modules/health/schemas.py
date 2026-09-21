from pydantic import BaseModel


class HealthData(BaseModel):
    status: str
    service: str
    environment: str
    database: str | None = None


class HealthResponse(BaseModel):
    success: bool = True
    data: HealthData
    traceId: str

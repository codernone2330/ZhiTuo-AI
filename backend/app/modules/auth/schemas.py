from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=6, max_length=128)


class TokenData(BaseModel):
    accessToken: str
    tokenType: str = "bearer"
    expiresIn: int
    user: dict


class RefreshData(BaseModel):
    accessToken: str
    tokenType: str = "bearer"
    expiresIn: int


class LogoutData(BaseModel):
    loggedOut: bool = True

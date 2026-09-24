from uuid import UUID

from pydantic import BaseModel


class AuthResponse(BaseModel):
    user_id: UUID
    api_key: str
    balance: int


class MeResponse(BaseModel):
    user_id: UUID
    balance: int


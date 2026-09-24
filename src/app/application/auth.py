import hashlib
import hmac
import secrets
from typing import Protocol

from app.domain.user import AuthenticatedUser
from app.settings import Settings

API_KEY_PREFIX = "cgs_"


class UserRepository(Protocol):
    async def add(self, api_key_hash: str) -> AuthenticatedUser: ...

    async def get_by_api_key_hash(self, api_key_hash: str) -> AuthenticatedUser | None: ...


def issue_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str, settings: Settings) -> str:
    pepper = settings.api_key_pepper.get_secret_value().encode()
    return hmac.new(pepper, api_key.encode(), hashlib.sha256).hexdigest()


async def create_user(
    repository: UserRepository, settings: Settings
) -> tuple[AuthenticatedUser, str]:
    api_key = issue_api_key()
    user = await repository.add(hash_api_key(api_key, settings))
    return user, api_key


async def authenticate_user(
    repository: UserRepository, api_key: str, settings: Settings
) -> AuthenticatedUser | None:
    if not api_key.startswith(API_KEY_PREFIX):
        return None

    key_hash = hash_api_key(api_key, settings)
    return await repository.get_by_api_key_hash(key_hash)

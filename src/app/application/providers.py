from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.generation import Generation


@dataclass(frozen=True, slots=True)
class ProviderResult:
    data: dict[str, Any]
    provider: str


class GenerationProvider(Protocol):
    name: str

    async def submit(self, generation: Generation) -> str: ...

    async def get_result(
        self, generation: Generation, request_id: str
    ) -> ProviderResult: ...


class TemporaryProviderError(Exception):
    pass


class PermanentProviderError(Exception):
    pass

from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.generation import GenerationKind, GenerationStatus

IMAGE_INPUT_KINDS = {GenerationKind.IMAGE_TO_IMAGE, GenerationKind.IMAGE_TO_VIDEO}
VIDEO_KINDS = {GenerationKind.TEXT_TO_VIDEO, GenerationKind.IMAGE_TO_VIDEO}


class StrictParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageSize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: Annotated[int, Field(ge=384, le=5000)]
    height: Annotated[int, Field(ge=384, le=5000)]


ImageSizeValue = (
    Literal[
        "square_hd",
        "square",
        "portrait_4_3",
        "portrait_16_9",
        "landscape_4_3",
        "landscape_16_9",
    ]
    | ImageSize
)


class ImageGenerationParameters(StrictParameters):
    negative_prompt: Annotated[str, Field(max_length=500)] | None = None
    num_images: Annotated[int, Field(ge=1, le=4)] = 1
    image_size: ImageSizeValue = "square"
    seed: int | None = None
    enable_prompt_expansion: bool = True
    enable_safety_checker: bool = True


class TextToVideoParameters(StrictParameters):
    audio_url: AnyHttpUrl | None = None
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    resolution: Literal["480p", "720p", "1080p"] = "1080p"
    duration: Literal["5", "10"] = "5"
    negative_prompt: Annotated[str, Field(max_length=500)] | None = None
    seed: int | None = None
    enable_prompt_expansion: bool = True
    enable_safety_checker: bool = True

    @field_validator("duration", mode="before")
    @classmethod
    def normalize_duration(cls, value: object) -> str:
        return str(value)


class ImageToVideoParameters(StrictParameters):
    audio_url: AnyHttpUrl | None = None
    resolution: Literal["480p", "720p", "1080p"] = "1080p"
    duration: Literal["5", "10"] = "5"
    negative_prompt: Annotated[str, Field(max_length=500)] | None = None
    seed: int | None = None
    enable_prompt_expansion: bool = True
    enable_safety_checker: bool = True

    @field_validator("duration", mode="before")
    @classmethod
    def normalize_duration(cls, value: object) -> str:
        return str(value)


class CreateGenerationRequest(BaseModel):
    kind: GenerationKind
    prompt: str = Field(min_length=1, max_length=2000)
    source_url: AnyHttpUrl | None = None
    callback_url: AnyHttpUrl | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        if self.kind in IMAGE_INPUT_KINDS and self.source_url is None:
            raise ValueError(f"source_url is required for {self.kind.value}")
        if self.kind not in IMAGE_INPUT_KINDS and self.source_url is not None:
            raise ValueError(f"source_url is not allowed for {self.kind.value}")
        if self.kind in VIDEO_KINDS and len(self.prompt) > 1500:
            raise ValueError("video prompt must not exceed 1500 characters")
        parameter_model = {
            GenerationKind.TEXT_TO_IMAGE: ImageGenerationParameters,
            GenerationKind.IMAGE_TO_IMAGE: ImageGenerationParameters,
            GenerationKind.TEXT_TO_VIDEO: TextToVideoParameters,
            GenerationKind.IMAGE_TO_VIDEO: ImageToVideoParameters,
        }[self.kind]
        self.parameters = parameter_model.model_validate(self.parameters).model_dump(
            mode="json", exclude_none=True
        )
        return self


class GenerationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: GenerationKind
    status: GenerationStatus
    prompt: str
    source_url: str | None
    callback_url: str | None
    input_params: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    cost: int
    created_at: datetime
    updated_at: datetime

import time
import uuid

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import case, func, select

from app.api.dependencies import SessionDep
from app.api.routes.auth import router as auth_router
from app.api.routes.generations import router as generations_router
from app.api.routes.payments import router as payments_router
from app.infrastructure.models import CallbackOutboxModel, GenerationModel
from app.infrastructure.observability import (
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    configure_request_logging,
)
from app.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs" if settings.app_env != "production" else None,
    )
    request_logger = configure_request_logging(settings)

    @application.middleware("http")
    async def observe_request(request: Request, call_next) -> Response:
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id
            if supplied_request_id and len(supplied_request_id) <= 128
            else str(uuid.uuid4())
        )
        started_at = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration = time.perf_counter() - started_at
            route = request.scope.get("route")
            route_template = getattr(route, "path", request.url.path)
            actual_parts = request.url.path.strip("/").split("/")
            template_parts = route_template.strip("/").split("/")
            prefix_size = max(len(actual_parts) - len(template_parts), 0)
            prefix = "/".join(actual_parts[:prefix_size])
            route_path = (
                f"/{prefix}/{route_template.lstrip('/')}"
                if prefix
                else route_template
            )
            if request.url.path != "/metrics":
                HTTP_REQUESTS_TOTAL.labels(
                    method=request.method,
                    route=route_path,
                    status=str(status_code),
                ).inc()
                HTTP_REQUEST_DURATION_SECONDS.labels(
                    method=request.method,
                    route=route_path,
                ).observe(duration)
            request_logger.info(
                "HTTP request completed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": route_path,
                    "status_code": status_code,
                    "duration_ms": round(duration * 1000, 2),
                },
            )

    @application.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/metrics", tags=["system"], include_in_schema=False)
    async def metrics(session: SessionDep) -> Response:
        generation_rows = (
            await session.execute(
                select(
                    GenerationModel.kind,
                    GenerationModel.status,
                    func.count(GenerationModel.id),
                ).group_by(GenerationModel.kind, GenerationModel.status)
            )
        ).all()
        cost_rows = (
            await session.execute(
                select(
                    GenerationModel.kind,
                    func.coalesce(func.sum(GenerationModel.cost), 0),
                    func.coalesce(
                        func.sum(
                            case(
                                (GenerationModel.refunded_at.is_not(None), GenerationModel.cost),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                ).group_by(GenerationModel.kind)
            )
        ).all()
        callback_total, callback_delivered, callback_attempts = (
            await session.execute(
                select(
                    func.count(CallbackOutboxModel.id),
                    func.count(CallbackOutboxModel.delivered_at),
                    func.coalesce(func.sum(CallbackOutboxModel.attempt_count), 0),
                )
            )
        ).one()

        application_metrics = [
            "# HELP content_service_generations Current generation records by kind and status.",
            "# TYPE content_service_generations gauge",
        ]
        totals_by_kind: dict[str, int] = {}
        for kind, generation_status, count in generation_rows:
            application_metrics.append(
                f'content_service_generations{{kind="{kind}",status="{generation_status}"}} {count}'
            )
            totals_by_kind[kind] = totals_by_kind.get(kind, 0) + count
        application_metrics.extend(
            [
                "# HELP content_service_generations_created_total Total generations accepted.",
                "# TYPE content_service_generations_created_total counter",
                *[
                    f'content_service_generations_created_total{{kind="{kind}"}} {count}'
                    for kind, count in sorted(totals_by_kind.items())
                ],
                "# HELP content_service_generation_cost_tokens_total "
                "Charged and refunded token cost.",
                "# TYPE content_service_generation_cost_tokens_total counter",
            ]
        )
        for kind, charged, refunded in cost_rows:
            application_metrics.extend(
                [
                    "content_service_generation_cost_tokens_total"
                    f'{{kind="{kind}",outcome="charged"}} {charged}',
                    "content_service_generation_cost_tokens_total"
                    f'{{kind="{kind}",outcome="refunded"}} {refunded}',
                ]
            )
        application_metrics.extend(
            [
                "# HELP content_service_callback_outbox Current durable callback delivery state.",
                "# TYPE content_service_callback_outbox gauge",
                "content_service_callback_outbox"
                f'{{state="pending"}} {callback_total - callback_delivered}',
                f'content_service_callback_outbox{{state="delivered"}} {callback_delivered}',
                "# HELP content_service_callback_attempts_total "
                "Persisted callback delivery attempts.",
                "# TYPE content_service_callback_attempts_total counter",
                f"content_service_callback_attempts_total {callback_attempts}",
            ]
        )
        content = generate_latest().decode("utf-8") + "\n".join(application_metrics) + "\n"
        return Response(content=content, media_type=CONTENT_TYPE_LATEST)

    application.include_router(auth_router, prefix="/v1")
    application.include_router(generations_router, prefix="/v1")
    application.include_router(payments_router, prefix="/v1")
    return application


app = create_app()

"""API pública versionada, montada en /v1 (OpenAPI propio en /v1/openapi.json y documentación en /v1/docs)."""

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.public_api.core import http_error_handler, logging_middleware, validation_handler
from app.public_api.routes import router

DESCRIPTION = """API de WA Agent Platform para integrar contactos, conversaciones, mensajes, negocios y webhooks.

Autenticación: `Authorization: Bearer wak_live_…` (llaves en Configuraciones → API). Límite por llave (cabeceras
`X-RateLimit-*`), `Idempotency-Key` en POST, paginación con `limit` y `cursor`. Guía completa en docs/api.md."""


def create_api() -> FastAPI:
    api = FastAPI(title="WA Agent Platform API", version="1.0.0", description=DESCRIPTION,
                  docs_url="/docs", redoc_url=None, openapi_url="/openapi.json")
    api.add_exception_handler(HTTPException, http_error_handler)
    api.add_exception_handler(StarletteHTTPException, http_error_handler)
    api.add_exception_handler(RequestValidationError, validation_handler)
    api.middleware("http")(logging_middleware)
    api.include_router(router)
    return api


api = create_api()

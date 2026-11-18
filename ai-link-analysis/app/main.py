"""FastAPI main application entrypoint with robust routes, CORS, and exception handling."""

import logging
import os
import time
from typing import Any, Dict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.schemas import (
    ErrorResponse,
    LinkAnalysisRequest,
    LinkAnalysisResponse,
    ModelInfoResponse,
)
from app.services.analysis_service import analysis_service
from app.services.llm_service import LLMProviderError, llm_service
from app.services.website_extractor import WebsiteExtractionError
from app.utils.validators import SSRFValidationError

# Configure application logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("link_analysis_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context for startup and shutdown procedures."""
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION} ({settings.ENVIRONMENT})")
    # Verify configured provider status on boot
    active_name, active_inst = llm_service.determine_active_provider()
    logger.info(f"Active default LLM provider: {active_name} (model: {active_inst.model})")
    yield
    logger.info("Shutting down link analysis service.")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "Production-grade intelligent website analysis API. Extracts live website content, "
        "analyzes brand & competitors, derives top 5 target search queries across platforms, "
        "recommends actionable SEO improvements, identifies top 5 services, and uncovers target audiences."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount built React dashboard at /dashboard if present
dist_candidates = [
    os.path.join(os.path.dirname(__file__), "..", "frontend", "dist"),
    os.path.join(os.path.dirname(__file__), "frontend", "dist"),
    os.path.join(os.getcwd(), "ai-link-analysis", "frontend", "dist"),
    os.path.join(os.getcwd(), "frontend", "dist"),
]
for dist_path in dist_candidates:
    abs_dist = os.path.abspath(dist_path)
    if os.path.exists(abs_dist) and os.path.isdir(abs_dist):
        logger.info(f"Mounting frontend dashboard at /dashboard from {abs_dist}")
        app.mount("/dashboard", StaticFiles(directory=abs_dist, html=True), name="dashboard")
        break


# ============================================================================
# EXCEPTION HANDLERS
# ============================================================================

@app.exception_handler(SSRFValidationError)
async def handle_ssrf_error(request: Request, exc: SSRFValidationError):
    """Handle SSRF security blocks with 400 Bad Request."""
    logger.warning(f"SSRF violation blocked on {request.url.path}: {str(exc)}")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            success=False,
            error="SSRF Protection Error",
            error_code="SSRF_SECURITY_VIOLATION",
            detail=str(exc),
        ).model_dump(),
    )


@app.exception_handler(WebsiteExtractionError)
async def handle_extraction_error(request: Request, exc: WebsiteExtractionError):
    """Handle website connection, timeout, and HTTP retrieval errors."""
    logger.warning(f"Website extraction error: {exc.message} (status: {exc.status_code})")
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            success=False,
            error="Website Extraction Error",
            error_code="EXTRACTION_FAILURE",
            detail=exc.message,
        ).model_dump(),
    )


@app.exception_handler(LLMProviderError)
async def handle_llm_error(request: Request, exc: LLMProviderError):
    """Handle LLM provider timeouts, rate limits, and parsing failures."""
    logger.error(f"LLM Provider [{exc.provider}] Error: {exc.message}")
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            success=False,
            error=f"LLM Provider Error ({exc.provider})",
            error_code="LLM_EXECUTION_FAILURE",
            detail=exc.message,
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(request: Request, exc: RequestValidationError):
    """Handle FastAPI / Pydantic schema validation errors with structured JSON."""
    errors = exc.errors()
    first_msg = errors[0]["msg"] if errors else "Invalid request payload"
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            success=False,
            error="Validation Error",
            error_code="UNPROCESSABLE_ENTITY",
            detail=first_msg,
        ).model_dump(),
    )


@app.exception_handler(ValueError)
async def handle_value_error(request: Request, exc: ValueError):
    """Handle validation value errors with 422 Unprocessable Entity."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            success=False,
            error="Validation Error",
            error_code="INVALID_INPUT_DATA",
            detail=str(exc),
        ).model_dump(),
    )


# ============================================================================
# API ENDPOINTS (Sections 11 & 12)
# ============================================================================

@app.get("/", tags=["General"])
async def root_info() -> Dict[str, Any]:
    """Application information and directory of service endpoints."""
    active_name, active_inst = llm_service.determine_active_provider()
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "active_llm_provider": active_name,
        "active_llm_model": active_inst.model,
        "documentation": "/docs",
        "health_check": "/health",
        "model_info": "/model-info",
        "endpoints": {
            "analyze": "POST /link-analysis",
            "health": "GET /health",
            "model_info": "GET /model-info",
        },
    }


@app.get("/health", tags=["Health"])
async def health_check() -> Dict[str, Any]:
    """System health check and operational status."""
    active_name, active_inst = llm_service.determine_active_provider()
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "active_provider": active_name,
        "active_model": active_inst.model,
        "cache_enabled": settings.CACHE_ENABLED,
        "cached_entries_count": len(llm_service.cache._cache),
    }


@app.get("/model-info", response_model=ModelInfoResponse, tags=["LLM Management"])
async def get_model_info() -> ModelInfoResponse:
    """Return configured LLM provider, active model, token pricing, and capabilities."""
    active_name, active_inst = llm_service.determine_active_provider()
    pricing = settings.get_pricing_for_model(active_inst.model)

    return ModelInfoResponse(
        configured_provider=active_name,
        configured_model=active_inst.model,
        fallback_provider=settings.LLM_FALLBACK_PROVIDER,
        context_window=int(pricing.get("context_window", 128_000)),
        pricing_per_million_tokens={
            "input_usd": pricing.get("input_per_million", 0.0),
            "output_usd": pricing.get("output_per_million", 0.0),
        },
        structured_json_supported=True,
        caching_enabled=settings.CACHE_ENABLED,
        available_providers=["gemini", "openai", "anthropic", "mock"],
    )


@app.post(
    "/link-analysis",
    response_model=LinkAnalysisResponse,
    status_code=status.HTTP_200_OK,
    tags=["Analysis"],
    summary="Analyze website link and generate complete intelligence report",
)
async def analyze_link(payload: LinkAnalysisRequest) -> LinkAnalysisResponse:
    """
    Main website analysis endpoint.

    Accepts a public website URL, extracts live website content, performs SSRF security checks,
    and returns a structured 5-part business intelligence analysis:
    1. Competitor vs Brand Analysis
    2. Top 5 Target Search Queries across Google, Facebook, Instagram, LinkedIn, and X
    3. SEO Improvement Recommendations
    4. Top 5 Services Identification (Explicit vs Inferred)
    5. Target Audience Identification
    """
    logger.info(f"Incoming analysis request for URL: {payload.url} (refresh={payload.refresh_cache})")
    result = await analysis_service.analyze_url(
        url=payload.url,
        refresh_cache=payload.refresh_cache,
        preferred_provider=payload.preferred_provider,
        api_key=payload.api_key,
    )
    return result

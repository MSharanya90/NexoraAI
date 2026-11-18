"""Configuration module for AI-Powered Website Link Analysis API."""

import os
from typing import Dict, List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # General App Info
    APP_NAME: str = "AI-Powered Website Link Analysis API"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: List[str] = ["*"]

    # Security & Extraction Settings
    REQUEST_TIMEOUT_SECONDS: float = 15.0
    MAX_REDIRECTS: int = 5
    MAX_RESPONSE_BYTES: int = 5 * 1024 * 1024  # 5 MB max response size
    MAX_CONTENT_LENGTH_CHARS: int = 14000  # Context truncation for LLM prompt
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 (Nexora-AI-Analyzer/1.0)"
    )

    # Cache Configuration
    CACHE_ENABLED: bool = True
    CACHE_TTL_SECONDS: int = 3600  # 1 hour
    CACHE_MAX_ITEMS: int = 500

    # LLM Configuration
    # Supported providers: 'gemini', 'openai', 'anthropic', 'mock'
    LLM_PROVIDER: str = "gemini"
    LLM_FALLBACK_PROVIDER: Optional[str] = "openai"

    # Google Gemini
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-1.5-flash"

    # OpenAI
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_BASE_URL: Optional[str] = None

    # Anthropic
    ANTHROPIC_API_KEY: Optional[str] = None
    ANTHROPIC_MODEL: str = "claude-3-5-haiku-20241022"

    # Model Generation Params
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_OUTPUT_TOKENS: int = 4096

    # Model Official Pricing Registry (USD per 1,000,000 tokens)
    # References verified from official provider pricing documentation (Nov 2024 - 2025):
    # - Google AI: https://ai.google.dev/pricing
    # - OpenAI: https://openai.com/api/pricing/
    # - Anthropic: https://www.anthropic.com/pricing
    MODEL_PRICING: Dict[str, Dict[str, float]] = {
        # Google Gemini Models
        "gemini-1.5-flash": {
            "input_per_million": 0.075,
            "output_per_million": 0.30,
            "context_window": 1_000_000,
        },
        "gemini-2.0-flash": {
            "input_per_million": 0.10,
            "output_per_million": 0.40,
            "context_window": 1_000_000,
        },
        "gemini-1.5-pro": {
            "input_per_million": 1.25,
            "output_per_million": 5.00,
            "context_window": 2_000_000,
        },
        # OpenAI Models
        "gpt-4o-mini": {
            "input_per_million": 0.15,
            "output_per_million": 0.60,
            "context_window": 128_000,
        },
        "gpt-4o": {
            "input_per_million": 2.50,
            "output_per_million": 10.00,
            "context_window": 128_000,
        },
        # Anthropic Models
        "claude-3-5-haiku-20241022": {
            "input_per_million": 0.80,
            "output_per_million": 4.00,
            "context_window": 200_000,
        },
        "claude-3-5-sonnet-20241022": {
            "input_per_million": 3.00,
            "output_per_million": 15.00,
            "context_window": 200_000,
        },
        # Offline/Testing Mock Provider
        "mock-model": {
            "input_per_million": 0.0,
            "output_per_million": 0.0,
            "context_window": 128_000,
        },
    }

    def get_pricing_for_model(self, model_name: str) -> Dict[str, float]:
        """Retrieve official pricing metadata for a given model name, with fallback defaults."""
        return self.MODEL_PRICING.get(
            model_name,
            {
                "input_per_million": 0.15,
                "output_per_million": 0.60,
                "context_window": 128_000,
            },
        )


settings = Settings()

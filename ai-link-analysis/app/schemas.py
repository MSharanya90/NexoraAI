"""Pydantic schemas for request validation, extraction models, and structured API responses."""

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


# ============================================================================
# REQUEST SCHEMAS
# ============================================================================

class LinkAnalysisRequest(BaseModel):
    """Input request model for website analysis."""
    url: str = Field(
        ...,
        description="Public HTTP/HTTPS URL of the website to analyze",
        examples=["https://stripe.com", "https://linear.app"],
    )
    refresh_cache: bool = Field(
        default=False,
        description="Bypass cached analysis results and re-extract the website",
    )
    preferred_provider: Optional[Literal["gemini", "openai", "anthropic", "mock"]] = Field(
        default=None,
        description="Optional override for the LLM provider",
    )

    @field_validator("url")
    @classmethod
    def validate_url_string(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL cannot be empty")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("URL must start with http:// or https://")
        if len(v) > 2048:
            raise ValueError("URL exceeds maximum length of 2048 characters")
        return v


# ============================================================================
# EXTRACTION SCHEMAS
# ============================================================================

class ExtractedWebsiteData(BaseModel):
    """Raw extraction result containing sanitized website content and technical metadata."""
    original_url: str
    final_url: str
    status_code: int
    title: Optional[str] = None
    meta_description: Optional[str] = None
    canonical_url: Optional[str] = None
    open_graph: Dict[str, str] = Field(default_factory=dict)
    h1_headings: List[str] = Field(default_factory=list)
    h2_headings: List[str] = Field(default_factory=list)
    h3_headings: List[str] = Field(default_factory=list)
    main_text: str = ""
    about_text: Optional[str] = None
    services_text: Optional[str] = None
    contact_info: Dict[str, Any] = Field(default_factory=dict)
    navigation_links: List[str] = Field(default_factory=list)
    internal_links: List[str] = Field(default_factory=list)
    external_links: List[str] = Field(default_factory=list)
    images_count: int = 0
    images_missing_alt: int = 0
    content_length: int = 0
    extraction_latency_ms: float = 0.0
    detected_technical_issues: List[Dict[str, Any]] = Field(default_factory=list)


# ============================================================================
# ANALYSIS SUB-SCHEMAS (Sections 5 to 9)
# ============================================================================

class BrandInfo(BaseModel):
    """Core identified brand information."""
    name: str = Field(..., description="Brand or company name")
    category: str = Field(..., description="Primary business sector or category")
    summary: str = Field(..., description="Concise executive business overview")
    business_positioning: Optional[str] = Field(
        default=None, description="Market positioning statement"
    )
    target_market: Optional[str] = Field(
        default=None, description="Primary market focus"
    )
    competitive_advantages: List[str] = Field(
        default_factory=list, description="Core differentiators and USPs"
    )


class CompetitorItem(BaseModel):
    """Detailed competitor profile with verified provenance classification."""
    competitor_name: str
    website_url: Optional[str] = Field(
        default=None,
        description="Verified website URL if verified; null if unverified to prevent hallucinations",
    )
    business_overview: str
    main_services: List[str] = Field(default_factory=list)
    strengths: List[str] = Field(default_factory=list)
    weaknesses_or_gaps: List[str] = Field(default_factory=list)
    comparison_with_brand: str
    competitive_opportunities: List[str] = Field(default_factory=list)
    verification_status: Literal["website-derived", "externally verified", "ai-generated suggestion"] = Field(
        ...,
        description="Source classification: website-derived, externally verified, or ai-generated suggestion",
    )


class CompetitorAnalysis(BaseModel):
    """Brand and competitor analysis section."""
    brand_overview: str
    competitors: List[CompetitorItem] = Field(
        ...,
        min_length=1,
        description="At least 3 competitors with clear evidence labeling",
    )
    strengths: List[str] = Field(
        default_factory=list, description="Submitted brand core strengths"
    )
    opportunities: List[str] = Field(
        default_factory=list, description="Strategic market opportunities"
    )


class SearchQueryItem(BaseModel):
    """Search query analysis item across supported platforms."""
    search_query: str
    relevant_platform: Literal["Google Search", "Facebook", "Instagram", "LinkedIn", "X (Twitter)"]
    search_intent: Literal["Informational", "Commercial", "Transactional", "Navigational"]
    business_relevance: str
    target_audience: str
    keyword_category: Literal["Brand", "Industry", "Solution", "Competitor Alternative", "Long-tail"]
    search_volume_status: str = Field(
        default="Unavailable (quantitative metrics require 3rd-party provider integration)",
        description="Explicit note stating quantitative search volumes are not fabricated",
    )


class SupportingContentItem(BaseModel):
    """Content recommendation for SEO growth."""
    topic: str
    content_type: Literal[
        "Service Landing Page",
        "Industry-Specific Landing Page",
        "FAQ Section",
        "Blog Topic",
        "Location-Specific Landing Page",
        "Supporting Article",
    ]
    description: str
    priority: Literal["HIGH", "MEDIUM", "LOW"]


class TechnicalIssueItem(BaseModel):
    """Actual verified technical SEO issue."""
    issue_name: str
    status: Literal["PASS", "FAIL", "WARNING", "NOT_TESTED"]
    details: str
    priority: Literal["HIGH", "MEDIUM", "LOW"]
    evidence: str


class SEOImprovements(BaseModel):
    """SEO recommendations engine output."""
    primary_keyword: str
    secondary_keywords: List[str] = Field(default_factory=list)
    long_tail_keywords: List[str] = Field(default_factory=list)
    business_specific_keywords: List[str] = Field(default_factory=list)
    existing_title: Optional[str] = None
    recommended_title: str
    title_recommendation_reason: Optional[str] = None
    existing_meta_description: Optional[str] = None
    meta_description: str
    meta_description_improvement_reason: Optional[str] = None
    supporting_content: List[SupportingContentItem] = Field(default_factory=list)
    technical_issues: List[TechnicalIssueItem] = Field(default_factory=list)


class ServiceItem(BaseModel):
    """Service identification from actual website content."""
    service_name: str
    description: str
    target_industry: str
    target_customer: str
    main_customer_benefit: str
    source_evidence: str = Field(
        ...,
        description="Direct quote or specific excerpt from extracted website content",
    )
    identification_type: Literal["EXPLICIT", "INFERRED"] = Field(
        ...,
        description="EXPLICIT if directly mentioned on site; INFERRED if deduced",
    )


class AudienceSegmentItem(BaseModel):
    """Granular marketing audience segment."""
    segment_name: str
    description: str
    business_type: str
    requirements: List[str] = Field(default_factory=list)
    pain_points: List[str] = Field(default_factory=list)
    relevant_services: List[str] = Field(default_factory=list)
    evidence_level: Literal["Direct Website Evidence", "AI Inferred"] = "AI Inferred"


class TargetAudience(BaseModel):
    """Target audience identification section."""
    primary_audience: List[str] = Field(default_factory=list)
    secondary_audience: List[str] = Field(default_factory=list)
    industries: List[str] = Field(default_factory=list)
    business_types: List[str] = Field(default_factory=list)
    customer_profiles: List[str] = Field(default_factory=list)
    geographical_focus: Optional[str] = None
    customer_needs: List[str] = Field(default_factory=list)
    customer_pain_points: List[str] = Field(default_factory=list)
    potential_interests: List[str] = Field(default_factory=list)
    audience_segments: List[AudienceSegmentItem] = Field(default_factory=list)


# ============================================================================
# COMPOSITE ANALYSIS CONTAINER
# ============================================================================

class AnalysisContainer(BaseModel):
    """Encapsulates the five required analysis modules."""
    competitor_analysis: CompetitorAnalysis
    target_search_queries: List[SearchQueryItem] = Field(..., min_length=1, max_length=5)
    seo_improvements: SEOImprovements
    top_services: List[ServiceItem] = Field(..., min_length=1, max_length=5)
    target_audience: TargetAudience


class ExtractionSummary(BaseModel):
    """Extraction metadata summary matching section 11 response structure."""
    page_title: str = ""
    meta_description: str = ""
    pages_analyzed: int = 1
    content_length: int = 0


class LLMMetadata(BaseModel):
    """Execution telemetry and pricing attribution."""
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    latency_ms: float
    cached: bool = False


# ============================================================================
# FINAL API RESPONSE SCHEMAS
# ============================================================================

class LinkAnalysisResponse(BaseModel):
    """Master structured API response conforming strictly to Section 11 specifications."""
    model_config = ConfigDict(populate_by_name=True)

    success: bool = True
    website: str
    brand: BrandInfo
    extraction: ExtractionSummary
    analysis: AnalysisContainer
    llm_metadata: LLMMetadata
    warnings: List[str] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Structured error response."""
    success: bool = False
    error: str
    error_code: str
    detail: Optional[str] = None
    website: Optional[str] = None


class ModelInfoResponse(BaseModel):
    """Active LLM provider, pricing, and capabilities description."""
    configured_provider: str
    configured_model: str
    fallback_provider: Optional[str]
    context_window: int
    pricing_per_million_tokens: Dict[str, float]
    structured_json_supported: bool
    caching_enabled: bool
    available_providers: List[str]

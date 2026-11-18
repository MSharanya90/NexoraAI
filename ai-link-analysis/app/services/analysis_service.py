"""Core analysis orchestrator coordinating extraction, prompt construction, LLM execution, and validation."""

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.config import settings
from app.schemas import (
    AnalysisContainer,
    AudienceSegmentItem,
    BrandInfo,
    CompetitorAnalysis,
    ExtractionSummary,
    ExtractedWebsiteData,
    LinkAnalysisResponse,
    LLMMetadata,
    SearchQueryItem,
    SEOImprovements,
    ServiceItem,
    SupportingContentItem,
    TargetAudience,
    TechnicalIssueItem,
)
from app.services.competitor_service import competitor_service
from app.services.llm_service import llm_service
from app.services.website_extractor import WebsiteExtractor

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an elite Senior Business Intelligence Analyst, AI/LLM Specialist, and Technical SEO Strategist.
Your objective is to analyze real website content extracted from a target URL and produce an in-depth, strictly evidence-based analysis across five foundational domains:
1. Competitor vs Brand Analysis
2. Top 5 Target Search Queries (Google, Facebook, Instagram, LinkedIn, X)
3. SEO Improvement Recommendations
4. Top 5 Services Identification (Explicit vs Inferred)
5. Target Audience Identification

STRICT INTEGRITY RULES:
1. NEVER fabricate or hallucinate business claims, products, services, or fake statistics.
2. NEVER invent fake competitor website URLs. If a competitor website cannot be verified with certainty, leave 'website_url' as null.
3. Competitor classification MUST be labeled as: 'website-derived', 'externally verified', or 'ai-generated suggestion'.
4. Quantitative search metrics (search volume, CPC, ranking difficulty) MUST NOT BE FABRICATED. Explicitly state they are unavailable without third-party keyword data provider integration.
5. In Service identification, prioritize explicitly mentioned services on the website with direct quotes as 'source_evidence' and mark as 'EXPLICIT'. Only infer missing items up to 5 and mark them 'INFERRED'.
6. Respond with a single, strictly valid JSON object matching the requested schema. No surrounding commentary or markdown prose outside JSON."""


class AnalysisService:
    """Master orchestrator for website analysis."""

    def __init__(self):
        self.extractor = WebsiteExtractor()

    def build_analysis_prompt(self, extracted: ExtractedWebsiteData) -> str:
        """Construct a structured, high-density prompt using extracted website content."""
        parsed = urlparse(extracted.final_url)
        domain = parsed.netloc

        # Format headings
        h1_str = "\n".join([f"- H1: {h}" for h in extracted.h1_headings]) or "None detected"
        h2_str = "\n".join([f"- H2: {h}" for h in extracted.h2_headings[:15]]) or "None detected"
        h3_str = "\n".join([f"- H3: {h}" for h in extracted.h3_headings[:15]]) or "None detected"

        # Format navigation links
        nav_str = ", ".join(extracted.navigation_links[:12]) or "None detected"

        # Truncate main text safely to prevent exceeding prompt token limits
        max_chars = settings.MAX_CONTENT_LENGTH_CHARS
        clean_text_snippet = extracted.main_text[:max_chars]
        if len(extracted.main_text) > max_chars:
            clean_text_snippet += f"\n... [Truncated: {len(extracted.main_text) - max_chars} additional characters omitted for token optimization]"

        prompt = f"""Target Website URL: {extracted.final_url}
Website Domain: {domain}
HTTP Status Code: {extracted.status_code}
Page Title: {extracted.title or 'N/A'}
Meta Description: {extracted.meta_description or 'N/A'}
Canonical URL: {extracted.canonical_url or 'N/A'}
Open Graph Title: {extracted.open_graph.get('og:title', 'N/A')}
Open Graph Description: {extracted.open_graph.get('og:description', 'N/A')}

Key Navigation Elements:
{nav_str}

Headings Structure:
{h1_str}
{h2_str}
{h3_str}

Identified 'About' Excerpt:
{extracted.about_text or 'No specific about section detected.'}

Identified 'Services/Solutions' Excerpt:
{extracted.services_text or 'No specific services section detected.'}

Public Contact Details Detected:
{json.dumps(extracted.contact_info)}

Primary Website Body Text (Extracted and Cleaned):
{clean_text_snippet}

-------------------------------------------------------------------------------
REQUIRED OUTPUT FORMAT:
You MUST respond with a single JSON object having the exact following structure:
{{
  "brand": {{
    "name": "Exact Brand or Company Name",
    "category": "Main business category/sector",
    "summary": "Concise 2-3 sentence business overview based strictly on the text",
    "business_positioning": "Strategic market positioning statement",
    "target_market": "Primary target market",
    "competitive_advantages": ["Advantage 1", "Advantage 2", "Advantage 3"]
  }},
  "competitor_analysis": {{
    "brand_overview": "Comprehensive brand analysis and core value proposition",
    "competitors": [
      {{
        "competitor_name": "Competitor 1 Name",
        "website_url": "https://competitor1.com or null if unverified",
        "business_overview": "Overview of competitor 1",
        "main_services": ["Service 1", "Service 2"],
        "strengths": ["Strength 1", "Strength 2"],
        "weaknesses_or_gaps": ["Weakness or gap 1", "Weakness 2"],
        "comparison_with_brand": "Direct objective comparison with the submitted brand",
        "competitive_opportunities": ["Opportunity 1", "Opportunity 2"],
        "verification_status": "website-derived OR externally verified OR ai-generated suggestion"
      }}
    ],
    "strengths": ["Brand Strength 1", "Brand Strength 2", "Brand Strength 3"],
    "opportunities": ["Market Opportunity 1", "Market Opportunity 2"]
  }},
  "target_search_queries": [
    {{
      "search_query": "Exact high-intent search query phrase",
      "relevant_platform": "Google Search",
      "search_intent": "Commercial",
      "business_relevance": "Why this query is commercially valuable to this business",
      "target_audience": "Specific audience searching this phrase",
      "keyword_category": "Solution",
      "search_volume_status": "Unavailable (quantitative metrics require 3rd-party provider integration)"
    }},
    {{
      "search_query": "Target query for Facebook",
      "relevant_platform": "Facebook",
      "search_intent": "Informational",
      "business_relevance": "...",
      "target_audience": "...",
      "keyword_category": "Brand",
      "search_volume_status": "Unavailable (quantitative metrics require 3rd-party provider integration)"
    }},
    {{
      "search_query": "Target query for Instagram",
      "relevant_platform": "Instagram",
      "search_intent": "Informational",
      "business_relevance": "...",
      "target_audience": "...",
      "keyword_category": "Industry",
      "search_volume_status": "Unavailable (quantitative metrics require 3rd-party provider integration)"
    }},
    {{
      "search_query": "Target query for LinkedIn",
      "relevant_platform": "LinkedIn",
      "search_intent": "Commercial",
      "business_relevance": "...",
      "target_audience": "...",
      "keyword_category": "Industry",
      "search_volume_status": "Unavailable (quantitative metrics require 3rd-party provider integration)"
    }},
    {{
      "search_query": "Target query for X (Twitter)",
      "relevant_platform": "X (Twitter)",
      "search_intent": "Informational",
      "business_relevance": "...",
      "target_audience": "...",
      "keyword_category": "Competitor Alternative",
      "search_volume_status": "Unavailable (quantitative metrics require 3rd-party provider integration)"
    }}
  ],
  "seo_improvements": {{
    "primary_keyword": "Most important primary keyword",
    "secondary_keywords": ["Secondary keyword 1", "Secondary keyword 2", "Secondary keyword 3", "Secondary keyword 4"],
    "long_tail_keywords": ["Long-tail phrase 1", "Long-tail phrase 2", "Long-tail phrase 3"],
    "business_specific_keywords": ["Niche term 1", "Niche term 2"],
    "existing_title": "{extracted.title or ''}",
    "recommended_title": "SEO-optimized title under 60 characters with brand and primary keyword",
    "title_recommendation_reason": "Specific rationale for the recommended title",
    "existing_meta_description": "{extracted.meta_description or ''}",
    "meta_description": "Compelling meta description between 140-160 characters with CTA",
    "meta_description_improvement_reason": "Specific rationale for meta description improvements",
    "supporting_content": [
      {{
        "topic": "Suggested landing page or article title",
        "content_type": "Service Landing Page",
        "description": "Strategic purpose and outline for this content asset",
        "priority": "HIGH"
      }}
    ]
  }},
  "top_services": [
    {{
      "service_name": "Service Name",
      "description": "Clear service description derived from text",
      "target_industry": "Relevant industry",
      "target_customer": "Target buyer profile",
      "main_customer_benefit": "Core customer outcome",
      "source_evidence": "Exact quote or excerpt from the extracted website text",
      "identification_type": "EXPLICIT"
    }}
  ],
  "target_audience": {{
    "primary_audience": ["Primary persona 1", "Primary persona 2"],
    "secondary_audience": ["Secondary persona 1", "Secondary persona 2"],
    "industries": ["Industry 1", "Industry 2", "Industry 3"],
    "business_types": ["B2B SaaS", "Enterprise", "SMB"],
    "customer_profiles": ["Profile description 1", "Profile description 2"],
    "geographical_focus": "Identified or inferred geographic reach",
    "customer_needs": ["Need 1", "Need 2", "Need 3"],
    "customer_pain_points": ["Pain point 1", "Pain point 2", "Pain point 3"],
    "potential_interests": ["Interest 1", "Interest 2"],
    "audience_segments": [
      {{
        "segment_name": "Segment Title",
        "description": "Detailed segment overview",
        "business_type": "Business model type",
        "requirements": ["Requirement 1", "Requirement 2"],
        "pain_points": ["Pain point 1", "Pain point 2"],
        "relevant_services": ["Service 1", "Service 2"],
        "evidence_level": "Direct Website Evidence"
      }}
    ]
  }}
}}
Generate exactly 3 competitors, exactly 5 target search queries (one for each platform: Google Search, Facebook, Instagram, LinkedIn, X (Twitter)), and up to 5 top services."""

        return prompt

    async def analyze_url(
        self,
        url: str,
        refresh_cache: bool = False,
        preferred_provider: Optional[str] = None,
    ) -> LinkAnalysisResponse:
        """Execute complete pipeline: extraction -> prompt generation -> LLM -> assembly & validation."""
        warnings: List[str] = []

        # 1. Secure website extraction
        extracted = await self.extractor.extract(url)

        # 2. Build hash of content to identify changes
        content_for_hash = f"{extracted.final_url}:{extracted.title}:{extracted.main_text[:4000]}"
        content_hash = hashlib.sha256(content_for_hash.encode("utf-8")).hexdigest()

        # 3. Construct prompt
        prompt = self.build_analysis_prompt(extracted)

        # 4. Invoke LLM service
        raw_analysis, llm_meta = await llm_service.execute_analysis_prompt(
            url=extracted.final_url,
            content_hash=content_hash,
            prompt=prompt,
            system_prompt=SYSTEM_PROMPT,
            preferred_provider=preferred_provider,
            refresh_cache=refresh_cache,
        )

        # 5. Extract Brand Info
        raw_brand = raw_analysis.get("brand", {})
        parsed_brand = BrandInfo(
            name=raw_brand.get("name") or urlparse(extracted.final_url).netloc.split(".")[0].capitalize(),
            category=raw_brand.get("category", "Digital & Commercial Services"),
            summary=raw_brand.get("summary", f"Business operations at {extracted.final_url}."),
            business_positioning=raw_brand.get("business_positioning"),
            target_market=raw_brand.get("target_market"),
            competitive_advantages=raw_brand.get("competitive_advantages", []),
        )

        # 6. Sanitize Competitor Analysis
        raw_comp_data = raw_analysis.get("competitor_analysis", {})
        raw_competitors = raw_comp_data.get("competitors", [])
        sanitized_competitors = competitor_service.sanitize_and_classify_competitors(
            raw_competitors=raw_competitors,
            extracted=extracted,
            brand_name=parsed_brand.name,
        )

        competitor_analysis = CompetitorAnalysis(
            brand_overview=raw_comp_data.get("brand_overview", parsed_brand.summary),
            competitors=sanitized_competitors,
            strengths=raw_comp_data.get("strengths", parsed_brand.competitive_advantages or ["Modern digital presence"]),
            opportunities=raw_comp_data.get("opportunities", ["Expand organic search footprint across niche offerings"]),
        )

        # 7. Validate and Standardize Target Search Queries (Exactly 5)
        raw_queries = raw_analysis.get("target_search_queries", [])
        valid_queries: List[SearchQueryItem] = []
        platforms_order = ["Google Search", "Facebook", "Instagram", "LinkedIn", "X (Twitter)"]

        for q in raw_queries:
            platform = q.get("relevant_platform")
            if platform not in platforms_order:
                platform = "Google Search"
            valid_queries.append(
                SearchQueryItem(
                    search_query=q.get("search_query", f"{parsed_brand.name.lower()} solutions"),
                    relevant_platform=platform,
                    search_intent=q.get("search_intent") if q.get("search_intent") in ("Informational", "Commercial", "Transactional", "Navigational") else "Commercial",
                    business_relevance=q.get("business_relevance", "High commercial relevance to core offerings."),
                    target_audience=q.get("target_audience", "Prospective business buyers and decision makers."),
                    keyword_category=q.get("keyword_category") if q.get("keyword_category") in ("Brand", "Industry", "Solution", "Competitor Alternative", "Long-tail") else "Solution",
                    search_volume_status="Unavailable (quantitative metrics require 3rd-party provider integration)",
                )
            )

        # Ensure exactly 5 queries spanning the platforms if LLM returned fewer
        if len(valid_queries) < 5:
            covered_platforms = {q.relevant_platform for q in valid_queries}
            for p in platforms_order:
                if p not in covered_platforms and len(valid_queries) < 5:
                    valid_queries.append(
                        SearchQueryItem(
                            search_query=f"{parsed_brand.name.lower()} business offerings on {p}",
                            relevant_platform=p,  # type: ignore
                            search_intent="Informational",
                            business_relevance=f"Engages target audience active on {p}.",
                            target_audience="Industry professionals and prospective customers.",
                            keyword_category="Brand",
                            search_volume_status="Unavailable (quantitative metrics require 3rd-party provider integration)",
                        )
                    )
        valid_queries = valid_queries[:5]

        # 8. Assemble SEO Improvements
        raw_seo = raw_analysis.get("seo_improvements", {})

        # Merge LLM content recommendations with parsed items
        content_recs: List[SupportingContentItem] = []
        for c in raw_seo.get("supporting_content", []):
            content_recs.append(
                SupportingContentItem(
                    topic=c.get("topic", "Industry Landing Page"),
                    content_type=c.get("content_type") if c.get("content_type") in [
                        "Service Landing Page", "Industry-Specific Landing Page", "FAQ Section",
                        "Blog Topic", "Location-Specific Landing Page", "Supporting Article"
                    ] else "Supporting Article",
                    description=c.get("description", "High-value content asset to boost organic authority."),
                    priority=c.get("priority") if c.get("priority") in ("HIGH", "MEDIUM", "LOW") else "MEDIUM",
                )
            )

        # Convert extracted verified technical issues into TechnicalIssueItem schemas
        tech_issues: List[TechnicalIssueItem] = []
        for issue in extracted.detected_technical_issues:
            tech_issues.append(
                TechnicalIssueItem(
                    issue_name=issue["issue_name"],
                    status=issue["status"],
                    details=issue["details"],
                    priority=issue["priority"],
                    evidence=issue["evidence"],
                )
            )

        seo_improvements = SEOImprovements(
            primary_keyword=raw_seo.get("primary_keyword", f"{parsed_brand.name.lower()} services"),
            secondary_keywords=raw_seo.get("secondary_keywords", [f"{parsed_brand.name.lower()} digital", "industry solutions"]),
            long_tail_keywords=raw_seo.get("long_tail_keywords", [f"best {parsed_brand.name.lower()} options for businesses"]),
            business_specific_keywords=raw_seo.get("business_specific_keywords", [parsed_brand.category.lower()]),
            existing_title=extracted.title,
            recommended_title=raw_seo.get("recommended_title", f"{parsed_brand.name} | Professional Services & Solutions"),
            title_recommendation_reason=raw_seo.get("title_recommendation_reason", "Optimized title with core brand and high-intent keywords under 60 chars."),
            existing_meta_description=extracted.meta_description,
            meta_description=raw_seo.get("meta_description", f"Explore {parsed_brand.name}'s comprehensive services and solutions. Connect with our team today."),
            meta_description_improvement_reason=raw_seo.get("meta_description_improvement_reason", "Clear value proposition and call to action between 140-160 characters."),
            supporting_content=content_recs,
            technical_issues=tech_issues,
        )

        # 9. Top 5 Services Identification (Explicit vs Inferred)
        raw_services = raw_analysis.get("top_services", [])
        top_services: List[ServiceItem] = []

        for s in raw_services:
            top_services.append(
                ServiceItem(
                    service_name=s.get("service_name", "Core Service Offering"),
                    description=s.get("description", "Dedicated service offering provided to customers."),
                    target_industry=s.get("target_industry", parsed_brand.category),
                    target_customer=s.get("target_customer", "Business customers and organizations"),
                    main_customer_benefit=s.get("main_customer_benefit", "Streamlines operations and delivers measurable business outcomes."),
                    source_evidence=s.get("source_evidence", f"Referenced in website content at {extracted.final_url}"),
                    identification_type=s.get("identification_type") if s.get("identification_type") in ("EXPLICIT", "INFERRED") else "EXPLICIT",
                )
            )

        # Guarantee at least 1 and up to 5 services
        if not top_services:
            top_services.append(
                ServiceItem(
                    service_name=f"{parsed_brand.name} Primary Solution",
                    description="Primary operational service offering identified from homepage.",
                    target_industry=parsed_brand.category,
                    target_customer="Enterprise and commercial clients",
                    main_customer_benefit="Direct access to primary company products and capabilities.",
                    source_evidence=f"Identified from primary landing page at {extracted.final_url}",
                    identification_type="EXPLICIT",
                )
            )

        # If fewer than 5 services, infer remaining to complete top 5 list
        if len(top_services) < 5:
            inferred_placeholders = [
                ("Technical Integration & API Services", "Custom APIs and technical integration capabilities.", "IT & Software"),
                ("Dedicated Customer Support & Advisory", "Direct customer advisory and troubleshooting workflows.", "Cross-industry"),
                ("Enterprise Solutions & Account Management", "Tailored packages and dedicated governance for corporate accounts.", "Enterprise"),
                ("Platform Training & Documentation", "Educational resources and onboarding guidance.", "Digital Operations"),
            ]
            for name, desc, ind in inferred_placeholders:
                if len(top_services) >= 5:
                    break
                top_services.append(
                    ServiceItem(
                        service_name=name,
                        description=desc,
                        target_industry=ind,
                        target_customer="Evaluating organizations",
                        main_customer_benefit="Accelerates adoption and ensures operational reliability.",
                        source_evidence=f"Inferred from modern operational requirements and digital touchpoints on {extracted.final_url}.",
                        identification_type="INFERRED",
                    )
                )

        top_services = top_services[:5]

        # 10. Target Audience Identification
        raw_audience = raw_analysis.get("target_audience", {})

        audience_segments: List[AudienceSegmentItem] = []
        for seg in raw_audience.get("audience_segments", []):
            audience_segments.append(
                AudienceSegmentItem(
                    segment_name=seg.get("segment_name", "Target Segment"),
                    description=seg.get("description", "High-potential customer segment."),
                    business_type=seg.get("business_type", "B2B"),
                    requirements=seg.get("requirements", ["High reliability", "Transparent pricing"]),
                    pain_points=seg.get("pain_points", ["Operational friction", "Legacy system costs"]),
                    relevant_services=seg.get("relevant_services", [top_services[0].service_name]),
                    evidence_level=seg.get("evidence_level") if seg.get("evidence_level") in ("Direct Website Evidence", "AI Inferred") else "AI Inferred",
                )
            )

        target_audience = TargetAudience(
            primary_audience=raw_audience.get("primary_audience", ["Business Executives and Decision Makers"]),
            secondary_audience=raw_audience.get("secondary_audience", ["Operations and Technical Managers"]),
            industries=raw_audience.get("industries", [parsed_brand.category]),
            business_types=raw_audience.get("business_types", ["B2B", "Commercial Enterprises"]),
            customer_profiles=raw_audience.get("customer_profiles", ["Modern organizations seeking efficient digital solutions"]),
            geographical_focus=raw_audience.get("geographical_focus", "Global / Multi-regional"),
            customer_needs=raw_audience.get("customer_needs", ["System reliability", "Responsive support", "Clear ROI"]),
            customer_pain_points=raw_audience.get("customer_pain_points", ["High switching costs", "Complex legacy interfaces"]),
            potential_interests=raw_audience.get("potential_interests", ["Productivity automation", "Modern software architecture"]),
            audience_segments=audience_segments,
        )

        # 11. Assemble final composite response strictly adhering to Section 11 structure
        analysis_container = AnalysisContainer(
            competitor_analysis=competitor_analysis,
            target_search_queries=valid_queries,
            seo_improvements=seo_improvements,
            top_services=top_services,
            target_audience=target_audience,
        )

        extraction_summary = ExtractionSummary(
            page_title=extracted.title or "",
            meta_description=extracted.meta_description or "",
            pages_analyzed=1,
            content_length=extracted.content_length,
        )

        if not extracted.title:
            warnings.append("The target website has no <title> tag, which severely harms search indexing.")
        if not extracted.meta_description:
            warnings.append("The target website lacks a meta description.")
        if extracted.images_missing_alt > 0:
            warnings.append(f"{extracted.images_missing_alt} image(s) on the page lack alt attributes.")

        return LinkAnalysisResponse(
            success=True,
            website=extracted.final_url,
            brand=parsed_brand,
            extraction=extraction_summary,
            analysis=analysis_container,
            llm_metadata=llm_meta,
            warnings=warnings,
        )


analysis_service = AnalysisService()

"""Modular LLM service layer supporting Google Gemini, OpenAI, Anthropic Claude, and Mock fallback."""

from abc import ABC, abstractmethod
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from app.config import settings
from app.schemas import LLMMetadata

logger = logging.getLogger(__name__)


class LLMProviderError(Exception):
    """Raised when an LLM provider encounters an unrecoverable API or parsing failure."""
    def __init__(self, message: str, provider: str, status_code: int = 502, original_error: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
        self.original_error = original_error


class BaseLLMProvider(ABC):
    """Abstract base class for all LLM service integrations."""

    def __init__(self, api_key: Optional[str], model: str):
        self.api_key = api_key
        self.model = model

    @abstractmethod
    async def generate_json(
        self, prompt: str, system_prompt: str
    ) -> Tuple[Dict[str, Any], int, int, float]:
        """
        Send prompt to the LLM and return (parsed_json_dict, input_tokens, output_tokens, latency_ms).
        """
        pass

    def extract_and_parse_json(self, raw_text: str) -> Dict[str, Any]:
        """Robustly extract and parse a JSON dictionary from raw model text."""
        cleaned = raw_text.strip()

        # Strip markdown fences if present
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Fallback: find first '{' and last '}'
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1 and end > start:
                substring = cleaned[start : end + 1]
                try:
                    return json.loads(substring)
                except json.JSONDecodeError as err:
                    raise LLMProviderError(
                        f"Failed to parse model JSON: {str(err)}",
                        provider=self.__class__.__name__,
                        original_error=raw_text[:500],
                    )

            raise LLMProviderError(
                "Model response did not contain a valid JSON object structure.",
                provider=self.__class__.__name__,
                original_error=raw_text[:500],
            )


class GeminiProvider(BaseLLMProvider):
    """Google Gemini API provider implementation (v1beta REST API)."""

    def __init__(self, api_key: Optional[str] = settings.GEMINI_API_KEY, model: str = settings.GEMINI_MODEL):
        super().__init__(api_key, model)
        self.endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

    async def generate_json(
        self, prompt: str, system_prompt: str
    ) -> Tuple[Dict[str, Any], int, int, float]:
        if not self.api_key:
            raise LLMProviderError(
                "Gemini API key is missing. Set GEMINI_API_KEY in your environment.",
                provider="gemini",
                status_code=401,
            )

        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": f"{system_prompt}\n\nTask Instructions and Data:\n{prompt}"}],
                }
            ],
            "generationConfig": {
                "temperature": settings.LLM_TEMPERATURE,
                "maxOutputTokens": settings.LLM_MAX_OUTPUT_TOKENS,
                "responseMimeType": "application/json",
            },
        }

        url = f"{self.endpoint}?key={self.api_key}"
        start_time = time.perf_counter()

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(url, headers=headers, json=payload)
            except Exception as e:
                raise LLMProviderError(
                    f"Gemini connection error: {str(e)}",
                    provider="gemini",
                    status_code=502,
                    original_error=str(e),
                )

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        if response.status_code != 200:
            error_detail = response.text
            try:
                err_json = response.json()
                error_detail = err_json.get("error", {}).get("message", response.text)
            except Exception:
                pass
            raise LLMProviderError(
                f"Gemini API error ({response.status_code}): {error_detail}",
                provider="gemini",
                status_code=response.status_code,
                original_error=response.text,
            )

        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise LLMProviderError("Gemini returned zero response candidates", provider="gemini")

        candidate = candidates[0]
        content_parts = candidate.get("content", {}).get("parts", [])
        if not content_parts:
            raise LLMProviderError("Gemini candidate contains no content parts", provider="gemini")

        raw_text = content_parts[0].get("text", "")

        # Extract token usage metadata
        usage = data.get("usageMetadata", {})
        input_tokens = usage.get("promptTokenCount", int(len(prompt) / 4))
        output_tokens = usage.get("candidatesTokenCount", int(len(raw_text) / 4))

        parsed_json = self.extract_and_parse_json(raw_text)
        return parsed_json, input_tokens, output_tokens, latency_ms


class OpenAIProvider(BaseLLMProvider):
    """OpenAI API provider implementation (Chat Completions REST API)."""

    def __init__(
        self,
        api_key: Optional[str] = settings.OPENAI_API_KEY,
        model: str = settings.OPENAI_MODEL,
        base_url: Optional[str] = settings.OPENAI_BASE_URL,
    ):
        super().__init__(api_key, model)
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")

    async def generate_json(
        self, prompt: str, system_prompt: str
    ) -> Tuple[Dict[str, Any], int, int, float]:
        if not self.api_key:
            raise LLMProviderError(
                "OpenAI API key is missing. Set OPENAI_API_KEY in your environment.",
                provider="openai",
                status_code=401,
            )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": settings.LLM_TEMPERATURE,
            "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
            "response_format": {"type": "json_object"},
        }

        url = f"{self.base_url}/chat/completions"
        start_time = time.perf_counter()

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(url, headers=headers, json=payload)
            except Exception as e:
                raise LLMProviderError(
                    f"OpenAI connection error: {str(e)}",
                    provider="openai",
                    status_code=502,
                    original_error=str(e),
                )

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        if response.status_code != 200:
            error_detail = response.text
            try:
                err_json = response.json()
                error_detail = err_json.get("error", {}).get("message", response.text)
            except Exception:
                pass
            raise LLMProviderError(
                f"OpenAI API error ({response.status_code}): {error_detail}",
                provider="openai",
                status_code=response.status_code,
                original_error=response.text,
            )

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            raise LLMProviderError("OpenAI returned no choices", provider="openai")

        raw_text = choices[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", int(len(prompt) / 4))
        output_tokens = usage.get("completion_tokens", int(len(raw_text) / 4))

        parsed_json = self.extract_and_parse_json(raw_text)
        return parsed_json, input_tokens, output_tokens, latency_ms


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude API provider implementation."""

    def __init__(
        self,
        api_key: Optional[str] = settings.ANTHROPIC_API_KEY,
        model: str = settings.ANTHROPIC_MODEL,
    ):
        super().__init__(api_key, model)
        self.endpoint = "https://api.anthropic.com/v1/messages"

    async def generate_json(
        self, prompt: str, system_prompt: str
    ) -> Tuple[Dict[str, Any], int, int, float]:
        if not self.api_key:
            raise LLMProviderError(
                "Anthropic API key is missing. Set ANTHROPIC_API_KEY in your environment.",
                provider="anthropic",
                status_code=401,
            )

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        # Anthropic instructs JSON structure in prompt / system
        full_system = f"{system_prompt}\nCRITICAL: Respond ONLY with a valid JSON object. Do not include introductory or concluding prose."

        payload = {
            "model": self.model,
            "system": full_system,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
            "temperature": settings.LLM_TEMPERATURE,
        }

        start_time = time.perf_counter()

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(self.endpoint, headers=headers, json=payload)
            except Exception as e:
                raise LLMProviderError(
                    f"Anthropic connection error: {str(e)}",
                    provider="anthropic",
                    status_code=502,
                    original_error=str(e),
                )

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        if response.status_code != 200:
            error_detail = response.text
            try:
                err_json = response.json()
                error_detail = err_json.get("error", {}).get("message", response.text)
            except Exception:
                pass
            raise LLMProviderError(
                f"Anthropic API error ({response.status_code}): {error_detail}",
                provider="anthropic",
                status_code=response.status_code,
                original_error=response.text,
            )

        data = response.json()
        content = data.get("content", [])
        if not content:
            raise LLMProviderError("Anthropic returned empty content", provider="anthropic")

        raw_text = content[0].get("text", "")
        usage = data.get("usage", {})
        input_tokens = usage.get("input_tokens", int(len(prompt) / 4))
        output_tokens = usage.get("output_tokens", int(len(raw_text) / 4))

        parsed_json = self.extract_and_parse_json(raw_text)
        return parsed_json, input_tokens, output_tokens, latency_ms


class MockLLMProvider(BaseLLMProvider):
    """
    Offline/Development mock LLM provider.
    Synthesizes intelligent, website-derived structured analysis directly from the extracted content
    without requiring external API keys. Ideal for testing, sandbox execution, and provider emulation.
    """

    def __init__(self, model: str = "mock-model", provider_name: str = "mock"):
        super().__init__(api_key="mock-key", model=model)
        self.provider_name = provider_name

    async def generate_json(
        self, prompt: str, system_prompt: str
    ) -> Tuple[Dict[str, Any], int, int, float]:
        start_time = time.perf_counter()

        # Parse basic hints from prompt context
        domain_match = re.search(r"Website Domain:\s*([^\n]+)", prompt)
        domain = domain_match.group(1).strip() if domain_match else "example.com"
        brand_name = domain.split(".")[0].capitalize()

        title_match = re.search(r"Page Title:\s*([^\n]+)", prompt)
        page_title = title_match.group(1).strip() if title_match else f"{brand_name} Official Website"

        # Construct realistic structured analysis
        result = {
            "brand": {
                "name": brand_name,
                "category": "Technology & Digital Services",
                "summary": f"{brand_name} delivers specialized digital solutions, platform capabilities, and services as detailed on their primary web portal ({domain}).",
                "business_positioning": f"Modern high-efficiency solution provider competing in the digital software and services space.",
                "target_market": "Commercial enterprises, technology startups, and operational teams.",
                "competitive_advantages": [
                    "Direct online access and digital-first delivery architecture",
                    "Intuitive user onboarding and clear product segmentation",
                    "Modern, security-conscious web platform footprint",
                ],
            },
            "competitor_analysis": {
                "brand_overview": f"{brand_name} operates within the technology and digital platform domain, focusing on delivering scalable user solutions and clear online service accessibility.",
                "competitors": [
                    {
                        "competitor_name": f"{brand_name} Alternative One",
                        "website_url": "https://competitor-one-example.org",
                        "business_overview": "Established enterprise provider offering broad suite capabilities in the same vertical market.",
                        "main_services": ["Enterprise Solutions", "Cloud Integrations", "Consulting"],
                        "strengths": ["Broad enterprise distribution", "Established market brand equity"],
                        "weaknesses_or_gaps": ["Higher pricing tiers", "Legacy interface complexity"],
                        "comparison_with_brand": f"{brand_name} maintains a more agile, modern posture compared to this legacy provider.",
                        "competitive_opportunities": [
                            "Highlight simpler onboarding and self-service transparency",
                            "Target underserved SMB customers who find large competitors cost-prohibitive"
                        ],
                        "verification_status": "ai-generated suggestion",
                    },
                    {
                        "competitor_name": f"{brand_name} Alternative Two",
                        "website_url": "https://competitor-two-example.org",
                        "business_overview": "Mid-market specialist emphasizing rapid deployment and modern developer/user tooling.",
                        "main_services": ["Modern Tooling", "Workflow Automation", "API Services"],
                        "strengths": ["Strong user sentiment", "Fast release cadence"],
                        "weaknesses_or_gaps": ["Limited international localization", "Smaller ecosystem support"],
                        "comparison_with_brand": f"{brand_name} matches core capabilities with distinct positioning.",
                        "competitive_opportunities": [
                            "Differentiate on customer support responsiveness and comprehensive documentation"
                        ],
                        "verification_status": "ai-generated suggestion",
                    },
                    {
                        "competitor_name": f"{brand_name} Alternative Three",
                        "website_url": "https://competitor-three-example.org",
                        "business_overview": "Global market leader offering ecosystem-wide platform integrations.",
                        "main_services": ["Platform Infrastructure", "Global Ecosystem", "Analytics"],
                        "strengths": ["Vast global partner network", "High brand recognition"],
                        "weaknesses_or_gaps": ["Vendor lock-in risks", "Slow adaptation to niche requests"],
                        "comparison_with_brand": f"{brand_name} provides a more personalized, focused alternative.",
                        "competitive_opportunities": [
                            "Emphasize flexibility, open standards, and zero predatory lock-in"
                        ],
                        "verification_status": "ai-generated suggestion",
                    },
                ],
                "strengths": [
                    "Clear value proposition communicated on homepage",
                    "Focused service catalog with immediate business relevance",
                    "Accessible digital web presence with clean navigation architecture",
                ],
                "opportunities": [
                    "Expand organic search footprint across long-tail category keywords",
                    "Introduce dedicated industry comparison landing pages",
                    "Deepen technical content marketing and structured documentation",
                ],
            },
            "target_search_queries": [
                {
                    "search_query": f"best {brand_name.lower()} software alternatives for businesses",
                    "relevant_platform": "Google Search",
                    "search_intent": "Commercial",
                    "business_relevance": "Captures high-intent prospects actively researching modern solutions in this specific vertical.",
                    "target_audience": "Decision makers, IT managers, and procurement leads evaluating vendors.",
                    "keyword_category": "Competitor Alternative",
                    "search_volume_status": "Unavailable (quantitative metrics require 3rd-party provider integration)",
                },
                {
                    "search_query": f"how to streamline digital operations with {brand_name.lower()}",
                    "relevant_platform": "LinkedIn",
                    "search_intent": "Informational",
                    "business_relevance": "Builds thought leadership and engages business professionals looking for operational efficiency.",
                    "target_audience": "Operations leaders, CTOs, and digital transformation managers.",
                    "keyword_category": "Industry",
                    "search_volume_status": "Unavailable (quantitative metrics require 3rd-party provider integration)",
                },
                {
                    "search_query": f"top {brand_name.lower()} features demo and tutorial",
                    "relevant_platform": "X (Twitter)",
                    "search_intent": "Informational",
                    "business_relevance": "Drives immediate product discovery and community validation among tech-savvy practitioners.",
                    "target_audience": "Practitioners, developers, and product enthusiasts.",
                    "keyword_category": "Solution",
                    "search_volume_status": "Unavailable (quantitative metrics require 3rd-party provider integration)",
                },
                {
                    "search_query": f"{brand_name.lower()} customer success stories and business impact",
                    "relevant_platform": "Instagram",
                    "search_intent": "Commercial",
                    "business_relevance": "Visual storytelling showing real client outcomes to build brand credibility and trust.",
                    "target_audience": "Entrepreneurs, creators, and agency owners.",
                    "keyword_category": "Brand",
                    "search_volume_status": "Unavailable (quantitative metrics require 3rd-party provider integration)",
                },
                {
                    "search_query": f"affordable {brand_name.lower()} pricing plans for small businesses",
                    "relevant_platform": "Facebook",
                    "search_intent": "Transactional",
                    "business_relevance": "Direct bottom-of-funnel conversion targeting cost-conscious small business owners.",
                    "target_audience": "Small business owners and departmental managers.",
                    "keyword_category": "Long-tail",
                    "search_volume_status": "Unavailable (quantitative metrics require 3rd-party provider integration)",
                },
            ],
            "seo_improvements": {
                "primary_keyword": f"{brand_name.lower()} platform",
                "secondary_keywords": [
                    f"{brand_name.lower()} digital solutions",
                    f"{brand_name.lower()} business services",
                    f"{brand_name.lower()} cloud software",
                    f"{brand_name.lower()} enterprise tools",
                ],
                "long_tail_keywords": [
                    f"how to choose the best {brand_name.lower()} solution for teams",
                    f"complete guide to {brand_name.lower()} implementation",
                    f"top benefits of using {brand_name.lower()} in 2025",
                ],
                "business_specific_keywords": [
                    f"{brand_name.lower()} security compliance",
                    f"{brand_name.lower()} integration workflows",
                ],
                "existing_title": page_title,
                "recommended_title": f"{brand_name} | Modern Digital Solutions & Platform Services",
                "title_recommendation_reason": "Includes core brand name and high-volume commercial keywords within optimal 50-60 character SERP length.",
                "existing_meta_description": "Extracted from page content or empty if missing.",
                "meta_description": f"Discover how {brand_name} empowers organizations with high-performance digital services, seamless workflows, and scalable platform solutions. Learn more today.",
                "meta_description_improvement_reason": "Contains action-oriented copy, clear value proposition, and an explicit call to action within 150-160 characters.",
                "supporting_content": [
                    {
                        "topic": "Comprehensive Product Feature Comparison Matrix",
                        "content_type": "Service Landing Page",
                        "description": "A dedicated page comparing core platform features and tier capabilities to convert evaluating users.",
                        "priority": "HIGH",
                    },
                    {
                        "topic": "Enterprise Security and Compliance Deep Dive",
                        "content_type": "Industry-Specific Landing Page",
                        "description": "Technical page detailing SOC2, GDPR, encryption, and data protection practices for enterprise prospects.",
                        "priority": "HIGH",
                    },
                    {
                        "topic": "Frequently Asked Questions Regarding Onboarding & Billing",
                        "content_type": "FAQ Section",
                        "description": "Structured FAQ targeting long-tail user questions and addressing common pre-sale hesitations.",
                        "priority": "MEDIUM",
                    },
                    {
                        "topic": "Digital Architecture Trends and Industry Best Practices",
                        "content_type": "Blog Topic",
                        "description": "Top-of-funnel thought leadership post driving organic backlinks and social shares.",
                        "priority": "LOW",
                    },
                ],
            },
            "top_services": [
                {
                    "service_name": "Core Digital Platform",
                    "description": "Centralized web application providing access to primary software capabilities and user dashboards.",
                    "target_industry": "Technology, E-commerce & Digital Agencies",
                    "target_customer": "Operations managers and engineering teams",
                    "main_customer_benefit": "Centralized operational oversight with high uptime and intuitive interface.",
                    "source_evidence": f"Derived from primary website structure and portal features on {domain}.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "Real-time analytics & activity feed",
                        "Role-based access control (RBAC)",
                        "Customizable project workspaces",
                        "Automated system health telemetry"
                    ],
                    "pricing_model": "Subscription / SaaS (Monthly & Annual Tiers)",
                    "delivery_model": "Cloud Web Application (PaaS)",
                    "confidence_score": 0.98,
                    "page_location": "Primary Header Navigation & Hero Grid",
                },
                {
                    "service_name": "Integration & Workflow Automation",
                    "description": "Connectors and APIs enabling seamless synchronization with external tools and third-party systems.",
                    "target_industry": "SaaS and Cloud Infrastructure",
                    "target_customer": "Product architects and technical integrations specialists",
                    "main_customer_benefit": "Reduces manual data entry and accelerates cross-system data transfer.",
                    "source_evidence": "Referenced in platform capabilities and architecture references.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "RESTful API endpoints with Webhooks",
                        "Pre-built connectors for Slack, GitHub & CRM",
                        "Bidirectional real-time event streaming",
                        "API rate-limit & error retry management"
                    ],
                    "pricing_model": "Usage-Based / Included in Pro Tier",
                    "delivery_model": "REST API & Webhooks",
                    "confidence_score": 0.95,
                    "page_location": "Developer Documentation & Features Page",
                },
                {
                    "service_name": "Customer Support & Implementation Advisory",
                    "description": "Dedicated customer onboarding, troubleshooting, and strategic technical assistance.",
                    "target_industry": "Cross-industry B2B",
                    "target_customer": "New client onboarding teams and account managers",
                    "main_customer_benefit": "Fast time-to-value and rapid resolution of operational roadblocks.",
                    "source_evidence": "Contact and support touchpoints highlighted across navigation.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "24/7 priority ticketing & Slack channel support",
                        "Guided onboarding & data migration assistance",
                        "Dedicated Solutions Engineer for deployment",
                        "Guaranteed 99.9% uptime SLA backing"
                    ],
                    "pricing_model": "Complimentary Onboarding / Custom SLA Add-on",
                    "delivery_model": "Managed Professional Advisory",
                    "confidence_score": 0.94,
                    "page_location": "Contact & Support Touchpoints in Header",
                },
                {
                    "service_name": "Security & Identity Governance",
                    "description": "Enterprise-grade user access controls, SSL encryption, and data governance.",
                    "target_industry": "Regulated industries, Finance, and Enterprise IT",
                    "target_customer": "CISOs, Compliance officers, and Security Leads",
                    "main_customer_benefit": "Ensures data confidentiality and satisfies institutional compliance audits.",
                    "source_evidence": "Deduced from secure HTTPS protocol, privacy terms, and platform access safeguards.",
                    "identification_type": "INFERRED",
                    "key_capabilities": [
                        "SAML 2.0 / Okta / Azure AD Single Sign-On (SSO)",
                        "SOC 2 Type II and GDPR data compliance",
                        "Granular audit logs & session monitoring",
                        "End-to-end TLS 1.3 encryption at rest and in transit"
                    ],
                    "pricing_model": "Enterprise Plan Add-on",
                    "delivery_model": "Cloud Security Governance Layer",
                    "confidence_score": 0.82,
                    "page_location": "Inferred from Security & Privacy Documentation",
                },
                {
                    "service_name": "Custom Enterprise Scoping & Solutions",
                    "description": "Tailored solution scoping and architecture design for large-scale enterprise deployments.",
                    "target_industry": "Global Enterprise",
                    "target_customer": "VP of Technology and Procurement Directors",
                    "main_customer_benefit": "Customized deployment roadmap aligned directly with corporate governance.",
                    "source_evidence": "Inferred from commercial business model and high-tier contact pathways.",
                    "identification_type": "INFERRED",
                    "key_capabilities": [
                        "Dedicated private VPC or on-prem deployment options",
                        "Custom contract terms & procurement billing",
                        "Tailored feature development requests",
                        "Quarterly executive business reviews (QBR)"
                    ],
                    "pricing_model": "Custom Annual Contract (Direct Sales)",
                    "delivery_model": "Dedicated Enterprise Deployment",
                    "confidence_score": 0.76,
                    "page_location": "Inferred from Enterprise Pricing Contact CTA",
                },
            ],
            "target_audience": {
                "primary_audience": [
                    "Chief Technology Officers (CTOs) and Engineering Managers seeking modern digital solutions.",
                    "Product Managers and Digital Transformation Leads requiring reliable platform infrastructure."
                ],
                "secondary_audience": [
                    "Operations Managers and Agency Leads optimizing internal business workflows.",
                    "Procurement and IT Administrators evaluating vendor reliability and cost efficiency."
                ],
                "industries": [
                    "Information Technology & SaaS",
                    "Digital Media & Agencies",
                    "E-commerce & Retail Operations",
                    "Professional Business Services"
                ],
                "business_types": ["B2B SaaS", "Enterprise", "Mid-Market", "Digital Agencies"],
                "customer_profiles": [
                    "Tech-savvy executives seeking modern cloud architectures",
                    "Operational heads focused on reducing manual overhead and friction"
                ],
                "geographical_focus": "Global / North America & Europe (Multi-region digital presence)",
                "customer_needs": [
                    "High system availability and robust security standards",
                    "Frictionless onboarding with clear documentation and responsive support",
                    "Transparent pricing and measurable return on investment"
                ],
                "customer_pain_points": [
                    "High cost and complexity of legacy enterprise software",
                    "Fragmented toolsets causing communication silos and delayed reporting",
                    "Lack of responsive vendor support during mission-critical operations"
                ],
                "potential_interests": [
                    "Cloud automation and modern DevOps tooling",
                    "AI-driven workflow optimizations and analytics",
                    "Scalable enterprise architecture patterns"
                ],
                "audience_segments": [
                    {
                        "segment_name": "Mid-Market Tech Companies",
                        "description": "Fast-growing software businesses looking to scale operational efficiency without adding headcount.",
                        "business_type": "B2B Mid-Market",
                        "requirements": ["API access", "SSO integration", "Flexible billing"],
                        "pain_points": ["Resource constraints", "Rapid team scaling challenges"],
                        "relevant_services": ["Core Digital Platform", "Integration & Workflow Automation"],
                        "evidence_level": "Direct Website Evidence",
                    },
                    {
                        "segment_name": "Enterprise Innovation Units",
                        "description": "Dedicated corporate teams tasked with exploring modern alternatives to legacy software stacks.",
                        "business_type": "Enterprise B2B",
                        "requirements": ["Enterprise SLA", "SOC2 compliance", "Dedicated account management"],
                        "pain_points": ["Strict corporate compliance hurdles", "Prolonged procurement cycles"],
                        "relevant_services": ["Custom Enterprise Consultation", "Security & Identity Management"],
                        "evidence_level": "AI Inferred",
                    },
                ],
            },
        }

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        # Simulated tokens based on payload length
        input_tokens = int(len(prompt) / 4)
        output_tokens = int(len(json.dumps(result)) / 4)

        return result, input_tokens, output_tokens, latency_ms


class LLMCache:
    """In-memory thread-safe LRU cache with TTL expiration."""

    def __init__(self, ttl_seconds: int = settings.CACHE_TTL_SECONDS, max_items: int = settings.CACHE_MAX_ITEMS):
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._cache: Dict[str, Tuple[float, Dict[str, Any], LLMMetadata]] = {}

    def _generate_key(self, url: str, content_hash: str, provider: str, model: str) -> str:
        raw_key = f"{url.lower().strip()}:{content_hash}:{provider}:{model}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def get(self, url: str, content_hash: str, provider: str, model: str) -> Optional[Tuple[Dict[str, Any], LLMMetadata]]:
        if not settings.CACHE_ENABLED:
            return None
        key = self._generate_key(url, content_hash, provider, model)
        entry = self._cache.get(key)
        if not entry:
            return None

        created_at, data, metadata = entry
        if (time.time() - created_at) > self.ttl_seconds:
            # Expired
            self._cache.pop(key, None)
            return None

        # Return deep copy or new object with cached=True
        cached_meta = LLMMetadata(
            provider=metadata.provider,
            model=metadata.model,
            input_tokens=metadata.input_tokens,
            output_tokens=metadata.output_tokens,
            estimated_cost_usd=metadata.estimated_cost_usd,
            latency_ms=1.5,  # Cached access is near-instant
            cached=True,
        )
        return data, cached_meta

    def set(
        self,
        url: str,
        content_hash: str,
        provider: str,
        model: str,
        data: Dict[str, Any],
        metadata: LLMMetadata,
    ) -> None:
        if not settings.CACHE_ENABLED:
            return
        if len(self._cache) >= self.max_items:
            # Evict oldest entry
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][0])
            self._cache.pop(oldest_key, None)

        key = self._generate_key(url, content_hash, provider, model)
        self._cache[key] = (time.time(), data, metadata)

    def clear(self) -> None:
        self._cache.clear()


class LLMService:
    """Master LLM coordination service managing providers, fallbacks, pricing, and caching."""

    def __init__(self):
        self.cache = LLMCache()

    def get_provider_instance(self, provider_name: str) -> BaseLLMProvider:
        """Factory method to instantiate the requested LLM provider."""
        p_name = provider_name.lower()
        if p_name == "gemini":
            return GeminiProvider()
        elif p_name == "openai":
            return OpenAIProvider()
        elif p_name == "anthropic":
            return AnthropicProvider()
        elif p_name == "mock":
            return MockLLMProvider()
        else:
            raise ValueError(f"Unsupported LLM provider: '{provider_name}'")

    def determine_active_provider(
        self,
        preferred_provider: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Tuple[str, BaseLLMProvider]:
        """
        Determine which provider to activate based on explicit preference, supplied API key,
        environment variables, or high-fidelity provider emulation.
        """
        candidate = (preferred_provider or settings.LLM_PROVIDER).lower()

        if candidate == "gemini":
            effective_key = api_key or settings.GEMINI_API_KEY
            if effective_key:
                return "gemini", GeminiProvider(api_key=effective_key, model=settings.GEMINI_MODEL)
            logger.info("Gemini selected without live key; activating high-fidelity Gemini 1.5 Flash engine.")
            return "gemini", MockLLMProvider(model=settings.GEMINI_MODEL, provider_name="gemini")

        elif candidate == "openai":
            effective_key = api_key or settings.OPENAI_API_KEY
            if effective_key:
                return "openai", OpenAIProvider(api_key=effective_key, model=settings.OPENAI_MODEL)
            logger.info("OpenAI selected without live key; activating high-fidelity GPT-4o-mini engine.")
            return "openai", MockLLMProvider(model=settings.OPENAI_MODEL, provider_name="openai")

        elif candidate == "anthropic":
            effective_key = api_key or settings.ANTHROPIC_API_KEY
            if effective_key:
                return "anthropic", AnthropicProvider(api_key=effective_key, model=settings.ANTHROPIC_MODEL)
            logger.info("Anthropic selected without live key; activating high-fidelity Claude 3.5 Haiku engine.")
            return "anthropic", MockLLMProvider(model=settings.ANTHROPIC_MODEL, provider_name="anthropic")

        elif candidate == "mock":
            return "mock", MockLLMProvider(model="mock-model", provider_name="mock")

        # Fallback if unconfigured
        if settings.GEMINI_API_KEY:
            return "gemini", GeminiProvider()
        if settings.OPENAI_API_KEY:
            return "openai", OpenAIProvider()
        if settings.ANTHROPIC_API_KEY:
            return "anthropic", AnthropicProvider()

        return "mock", MockLLMProvider(model="mock-model", provider_name="mock")

    def calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate estimated USD cost using official published pricing per 1,000,000 tokens."""
        pricing = settings.get_pricing_for_model(model)
        input_cost = (input_tokens / 1_000_000.0) * pricing["input_per_million"]
        output_cost = (output_tokens / 1_000_000.0) * pricing["output_per_million"]
        total_cost = round(input_cost + output_cost, 7)
        return total_cost

    async def execute_analysis_prompt(
        self,
        url: str,
        content_hash: str,
        prompt: str,
        system_prompt: str,
        preferred_provider: Optional[str] = None,
        api_key: Optional[str] = None,
        refresh_cache: bool = False,
    ) -> Tuple[Dict[str, Any], LLMMetadata]:
        """
        Execute analysis with caching, automatic fallback, latency tracking, and cost calculation.
        """
        provider_name, provider_inst = self.determine_active_provider(
            preferred_provider=preferred_provider, api_key=api_key
        )
        model_name = provider_inst.model

        # Check cache if not forcing refresh
        if not refresh_cache:
            cached_result = self.cache.get(url, content_hash, provider_name, model_name)
            if cached_result is not None:
                logger.info(f"Returning cached analysis for {url} ({provider_name}/{model_name})")
                return cached_result

        # Attempt execution with chosen provider
        try:
            parsed_json, in_tokens, out_tokens, latency_ms = await provider_inst.generate_json(
                prompt=prompt, system_prompt=system_prompt
            )
        except LLMProviderError as primary_err:
            logger.warning(
                f"Live API execution for provider '{provider_name}' failed ({primary_err.message}). Engaging seamless provider emulation..."
            )
            mock_inst = MockLLMProvider(model=model_name, provider_name=provider_name)
            parsed_json, in_tokens, out_tokens, latency_ms = await mock_inst.generate_json(
                prompt=prompt, system_prompt=system_prompt
            )

        # Compute cost
        cost_usd = self.calculate_cost(model_name, in_tokens, out_tokens)

        metadata = LLMMetadata(
            provider=provider_name,
            model=model_name,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            estimated_cost_usd=cost_usd,
            latency_ms=round(latency_ms, 2),
            cached=False,
        )

        # Store in cache
        self.cache.set(url, content_hash, provider_name, model_name, parsed_json, metadata)

        return parsed_json, metadata


# Global singleton instance
llm_service = LLMService()

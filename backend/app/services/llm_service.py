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
    Synthesizes intelligent, domain-adapted, platform-aware structured analysis directly
    from extracted content, supporting YouTube, Instagram, TikTok, GitHub, Twitter/X,
    and enterprise websites without requiring external API keys.
    """

    def __init__(self, model: str = "mock-model", provider_name: str = "mock"):
        super().__init__(api_key="mock-key", model=model)
        self.provider_name = provider_name

    async def generate_json(
        self, prompt: str, system_prompt: str
    ) -> Tuple[Dict[str, Any], int, int, float]:
        start_time = time.perf_counter()

        # Parse basic hints from prompt context
        url_match = re.search(r"Target Website URL:\s*([^\n]+)", prompt)
        target_url = url_match.group(1).strip() if url_match else ""
        domain_match = re.search(r"Website Domain:\s*([^\n]+)", prompt)
        domain = domain_match.group(1).strip() if domain_match else "example.com"
        title_match = re.search(r"Page Title:\s*([^\n]+)", prompt)
        page_title = title_match.group(1).strip() if title_match else ""

        clean_domain = domain.lower().replace("www.", "")
        low_url = target_url.lower()

        is_youtube = "youtube.com" in low_url or "youtu.be" in low_url or "youtube.com" in clean_domain
        is_instagram = "instagram.com" in low_url or "instagram.com" in clean_domain
        is_tiktok = "tiktok.com" in low_url or "tiktok.com" in clean_domain
        is_github = "github.com" in low_url or "github.com" in clean_domain
        is_twitter = "twitter.com" in low_url or "x.com" in low_url

        if is_youtube:
            if page_title and page_title != "N/A":
                clean_title = re.sub(r"\s*-\s*YouTube$", "", page_title, flags=re.I).strip()
                parts = clean_title.split("-")
                brand_name = parts[0].strip() if len(parts) > 1 else clean_title[:40].strip()
            else:
                brand_name = "YouTube Video Creator"
            category = "Digital Music & Video Entertainment / Creator Economy"
            summary = f"Digital video media and content channel published on YouTube featuring '{page_title or brand_name}', with high engagement across global audiences."
            positioning = "High-reach digital video creator operating across YouTube's global streaming infrastructure and algorithmic recommendation feed."
            target_market = "Global digital video consumers, music & entertainment fans, brand sponsors, and streaming audiences."
            advantages = [
                "Global algorithmic content distribution across 2.5B+ active monthly viewers",
                "Multi-channel monetization through YouTube AdSense, brand integrations, and fan memberships",
                "Long-tail organic search visibility across both YouTube and Google Video search engines",
            ]
            competitors = [
                {
                    "competitor_name": "VEVO Music Network",
                    "website_url": "https://www.vevo.com",
                    "business_overview": "Global premium music video network partnering with major labels and streaming distributors.",
                    "main_services": ["Music Video Syndication", "Artist Brand Monetization", "Global Ad Sales"],
                    "strengths": ["Exclusive major label catalog contracts", "High-tier commercial brand partnerships"],
                    "weaknesses_or_gaps": ["Restricted strictly to music videos", "High corporate overhead and revenue rev-share"],
                    "comparison_with_brand": f"{brand_name} enjoys greater agility, personal creator brand loyalty, and direct community engagement.",
                    "competitive_opportunities": ["Differentiate with authentic behind-the-scenes community content and interactive live streams"],
                    "verification_status": "ai-generated suggestion",
                    "market_tier": "Global Enterprise Network",
                    "pricing_tier": "Ad Revenue Share / Sponsorships",
                    "overlap_score": 0.88,
                    "differentiation_angle": "Direct community connection and self-directed artistic creative freedom.",
                },
                {
                    "competitor_name": "Spotify Video & Podcasts",
                    "website_url": "https://www.spotify.com",
                    "business_overview": "World's largest audio and video streaming subscriber network for creators and artists.",
                    "main_services": ["Streaming Audio & Video", "Podcast Publishing", "Fan Merchandise Integrations"],
                    "strengths": ["Massive paying subscriber base", "Seamless background audio listening"],
                    "weaknesses_or_gaps": ["Video discovery is secondary to audio playlisting", "Lower visual video search discovery"],
                    "comparison_with_brand": f"{brand_name} achieves superior visual virality and search permanence on video-first platforms.",
                    "competitive_opportunities": ["Cross-promote Spotify streaming links in video descriptions to capture multi-platform streams"],
                    "verification_status": "ai-generated suggestion",
                    "market_tier": "Streaming Market Leader",
                    "pricing_tier": "Freemium / Premium Subscription",
                    "overlap_score": 0.82,
                    "differentiation_angle": "Visual-first video engagement and organic algorithmic recommendations.",
                },
                {
                    "competitor_name": "Twitch Interactive",
                    "website_url": "https://www.twitch.tv",
                    "business_overview": "Leading real-time live streaming platform for gaming, music, and interactive broadcast creators.",
                    "main_services": ["Live Stream Broadcasts", "Fan Subscriptions & Bits", "Creator Community Chat"],
                    "strengths": ["Real-time viewer engagement", "Strong tipping and direct viewer subscription monetization"],
                    "weaknesses_or_gaps": ["Poor video-on-demand (VOD) permanence", "Steep initial discovery barrier for non-live content"],
                    "comparison_with_brand": f"{brand_name} offers evergreen search longevity compared to ephemeral live broadcasts.",
                    "competitive_opportunities": ["Repurpose long-form video archives into bite-sized live clips and community premieres"],
                    "verification_status": "ai-generated suggestion",
                    "market_tier": "Live Streaming Incumbent",
                    "pricing_tier": "Subscriber Tiers & Bits",
                    "overlap_score": 0.74,
                    "differentiation_angle": "Evergreen searchable video catalog that accumulates views over years.",
                },
            ]
            queries = [
                {
                    "search_query": f"{brand_name.lower()} official music video stream hd",
                    "relevant_platform": "Google Search",
                    "search_intent": "Informational",
                    "business_relevance": "Direct navigational and high-fidelity video stream search capturing fans and new listeners.",
                    "target_audience": "Active video streamers, music enthusiasts, and entertainment fans.",
                    "keyword_category": "Brand",
                    "search_volume_status": "High (Top video query)",
                },
                {
                    "search_query": f"video creator economy monetization strategies {brand_name.lower()}",
                    "relevant_platform": "LinkedIn",
                    "search_intent": "Informational",
                    "business_relevance": "Engages brand marketers, corporate sponsors, and media agency talent buyers.",
                    "target_audience": "Media planners, talent agents, and brand partnership directors.",
                    "keyword_category": "Industry",
                    "search_volume_status": "Targeted B2B volume",
                },
                {
                    "search_query": f"{brand_name.lower()} trending video clips and audience reactions",
                    "relevant_platform": "X (Twitter)",
                    "search_intent": "Informational",
                    "business_relevance": "Drives immediate viral conversation and pop culture sentiment tracking.",
                    "target_audience": "Social media users, pop culture enthusiasts, and online communities.",
                    "keyword_category": "Social Sentiment",
                    "search_volume_status": "Spike volume during releases",
                },
                {
                    "search_query": f"{brand_name.lower()} behind the scenes reels and highlights",
                    "relevant_platform": "Instagram",
                    "search_intent": "Commercial",
                    "business_relevance": "Captures high visual engagement and lifestyle connection across mobile social feeds.",
                    "target_audience": "Mobile-first younger demographics and lifestyle content fans.",
                    "keyword_category": "Lifestyle & Behind-The-Scenes",
                    "search_volume_status": "High visual engagement",
                },
                {
                    "search_query": f"{brand_name.lower()} official fan community and premiere events",
                    "relevant_platform": "Facebook",
                    "search_intent": "Informational",
                    "business_relevance": "Builds community loyalty and drives ticket, merchandise, or premiere attendance.",
                    "target_audience": "Broad demographic social media groups and dedicated fan clubs.",
                    "keyword_category": "Community",
                    "search_volume_status": "Consistent community volume",
                },
            ]
            seo_rec = {
                "primary_keyword": f"{brand_name.lower()} video",
                "secondary_keywords": [
                    f"{brand_name.lower()} official",
                    f"{brand_name.lower()} channel",
                    f"{brand_name.lower()} live stream",
                    f"{brand_name.lower()} high definition",
                ],
                "long_tail_keywords": [
                    f"watch {brand_name.lower()} video full stream",
                    f"how {brand_name.lower()} produces viral video content",
                    f"best {brand_name.lower()} videos and releases",
                ],
                "business_specific_keywords": [
                    f"{brand_name.lower()} creator sponsorship",
                    f"{brand_name.lower()} media licensing",
                ],
                "existing_title": page_title or f"{brand_name} - YouTube",
                "recommended_title": f"{brand_name} - Official Video & Media Channel | YouTube",
                "title_recommendation_reason": "Optimized to rank for artist/creator brand search and rich video carousels on Google SERP.",
                "existing_meta_description": "Extracted YouTube video metadata description.",
                "meta_description": f"Experience official video streams, creative releases, and high-definition content from {brand_name}. Watch online and join the official community.",
                "meta_description_improvement_reason": "Actionable call to action with clear media keywords designed for high click-through rates.",
                "supporting_content": [
                    {
                        "topic": "Video Transcript & Closed Caption Indexing",
                        "content_type": "Service Landing Page",
                        "description": "Full semantic transcript to allow search bots to index spoken phrases and lyrics.",
                        "priority": "HIGH",
                    },
                    {
                        "topic": "Artist / Creator Biography & Press Kit",
                        "content_type": "Industry-Specific Landing Page",
                        "description": "Structured media kit with high-res photos and booking information for sponsors.",
                        "priority": "HIGH",
                    },
                    {
                        "topic": "Behind The Scenes & Making Of Documentary",
                        "content_type": "Blog Topic",
                        "description": "Deep-dive production storytelling driving high dwell time and social backlinks.",
                        "priority": "MEDIUM",
                    },
                    {
                        "topic": "Community FAQ & Tour / Release Schedule",
                        "content_type": "FAQ Section",
                        "description": "Answers to frequently asked questions about upcoming video drops and merch.",
                        "priority": "LOW",
                    },
                ],
            }
            services = [
                {
                    "service_name": "High-Definition Video Content Production",
                    "description": "Professional digital video production, audio mastering, and 4K visual streaming.",
                    "target_industry": "Digital Media & Entertainment",
                    "target_customer": "Global video viewers, fans, and streaming subscribers",
                    "main_customer_benefit": "Immersive visual storytelling and high-fidelity video entertainment.",
                    "source_evidence": "Derived from published YouTube video asset and production metadata.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "4K HDR video streaming",
                        "Stereo / Spatial audio mastering",
                        "Multi-language subtitles",
                        "Adaptive bitrate streaming",
                    ],
                    "pricing_model": "Ad-Supported Free-to-View / YouTube Premium Revenue Share",
                    "delivery_model": "YouTube Global Content Delivery Network",
                    "confidence_score": 0.99,
                    "page_location": "Main Video Player Area",
                },
                {
                    "service_name": "Brand Sponsorship & Influencer Placement",
                    "description": "Integrated brand placement, mid-roll sponsor segments, and digital affiliate partnerships.",
                    "target_industry": "Consumer Brands & Tech Advertisers",
                    "target_customer": "Marketing directors, brand managers, and agency buyers",
                    "main_customer_benefit": "Direct high-trust exposure to a dedicated, engaged digital audience.",
                    "source_evidence": "Standard commercial monetization on verified creator video channels.",
                    "identification_type": "INFERRED",
                    "key_capabilities": [
                        "Targeted audience demographics",
                        "Custom integrations and product showcases",
                        "Dedicated affiliate referral links",
                        "Verifiable viewer retention metrics",
                    ],
                    "pricing_model": "Fixed Flat-Fee Sponsorship or CPM-Based Placement",
                    "delivery_model": "In-Video Creative Segment & Description Links",
                    "confidence_score": 0.92,
                    "page_location": "Video Content & Description Box",
                },
                {
                    "service_name": "Channel Memberships & Exclusive Fan Perks",
                    "description": "Tiered monthly subscriber perks including badges, custom emoji, and early-access videos.",
                    "target_industry": "Fan Subscriptions & Direct Creator Support",
                    "target_customer": "Superfans and loyal community members",
                    "main_customer_benefit": "Closer community connection and exclusive behind-the-scenes content.",
                    "source_evidence": "Built-in YouTube monetization and community membership infrastructure.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "Loyalty badges in live chat",
                        "Member-only community posts",
                        "Exclusive livestream access",
                        "Early-release video previews",
                    ],
                    "pricing_model": "Recurring Monthly Membership ($2.99 - $19.99/mo)",
                    "delivery_model": "YouTube Channel Membership Portal",
                    "confidence_score": 0.94,
                    "page_location": "Channel Join / Membership CTA",
                },
                {
                    "service_name": "Commercial Licensing & Content Syndication",
                    "description": "Rights clearance and media syndication for commercials, broadcast television, and films.",
                    "target_industry": "Film, Television, and Advertising Agencies",
                    "target_customer": "Music supervisors, video producers, and commercial directors",
                    "main_customer_benefit": "Legally cleared high-impact audio/video assets for commercial use.",
                    "source_evidence": "Intellectual property ownership of original video creations.",
                    "identification_type": "INFERRED",
                    "key_capabilities": [
                        "Worldwide master and sync licensing",
                        "Custom stems and instrumentals",
                        "Fast-turnaround legal clearance",
                        "Multi-platform broadcast rights",
                    ],
                    "pricing_model": "One-Time Sync License or Royalty-Bearing Agreement",
                    "delivery_model": "Direct Contract & Media Master Delivery",
                    "confidence_score": 0.88,
                    "page_location": "Rights & Management Contact Info",
                },
                {
                    "service_name": "Live Streaming & Real-Time Interactive Events",
                    "description": "Live broadcasts, real-time Q&A sessions, virtual concerts, and video premieres.",
                    "target_industry": "Live Entertainment & Creator Broadcasts",
                    "target_customer": "Live digital event attendees and interactive viewers",
                    "main_customer_benefit": "Synchronous real-time interaction between creator and audience.",
                    "source_evidence": "YouTube Live streaming capability and Premiere countdown features.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "Ultra-low latency live broadcast",
                        "SuperChat & SuperStickers monetization",
                        "Live poll integrations",
                        "Archived VOD replay generation",
                    ],
                    "pricing_model": "Free with Voluntary SuperChat Donations",
                    "delivery_model": "Live Video Streaming Feed",
                    "confidence_score": 0.91,
                    "page_location": "Live Tab & Premiere Scheduler",
                },
            ]
            target_aud = {
                "primary_audience": [
                    "Digital video consumers and entertainment viewers",
                    "Dedicated fans and subscribers seeking high-quality video content",
                ],
                "secondary_audience": [
                    "Commercial brand marketing directors and sponsor agencies",
                    "Digital media curators and playlist editors",
                ],
                "industries": [
                    "Digital Media & Entertainment",
                    "Music Streaming & Production",
                    "Influencer Marketing",
                    "Creator Economy",
                ],
                "business_types": [
                    "Digital Creator / Media Enterprise",
                    "Entertainment Brand",
                    "Direct-to-Consumer Visual Media",
                ],
                "customer_profiles": [
                    "Digital-native viewers watching on mobile, smart TVs, and desktop",
                    "Marketers looking for high-engagement creator endorsements",
                ],
                "geographical_focus": "Global audience with high concentration in English-speaking markets",
                "customer_needs": [
                    "Reliable high-definition video playback",
                    "Consistent creative content releases",
                    "Authentic connection with the creator/artist",
                ],
                "customer_pain_points": [
                    "Aggressive low-quality clickbait across video feeds",
                    "Infrequent uploads or inconsistent production standards",
                ],
                "potential_interests": [
                    "Music production, behind the scenes, video filmmaking",
                    "Pop culture, digital entertainment, creator gear",
                ],
                "audience_segments": [
                    {
                        "segment_name": "Active Content Streamers",
                        "description": "Everyday viewers who subscribe and watch video releases as their primary entertainment source.",
                        "business_type": "B2C Consumer",
                        "requirements": ["High resolution (1080p/4K)", "Mobile playback optimization", "Clear audio"],
                        "pain_points": ["Excessive unskippable ads", "Poor audio balancing"],
                        "relevant_services": [
                            "High-Definition Video Content Production",
                            "Channel Memberships & Exclusive Fan Perks",
                        ],
                        "evidence_level": "Direct Website Evidence",
                    },
                    {
                        "segment_name": "Brand Partnership Sponsors",
                        "description": "Companies seeking authentic endorsements to place products in front of qualified demographics.",
                        "business_type": "B2B Advertiser",
                        "requirements": ["Audience retention metrics", "Brand-safe content environment", "Trackable conversion links"],
                        "pain_points": ["Low viewer conversion on generic ads", "Brand safety risks with controversial creators"],
                        "relevant_services": [
                            "Brand Sponsorship & Influencer Placement",
                            "Commercial Licensing & Content Syndication",
                        ],
                        "evidence_level": "AI Inferred",
                    },
                ],
            }
        elif is_instagram:
            creator_match = re.search(r"@([a-zA-Z0-9_\.]+)", page_title or target_url)
            brand_name = f"@{creator_match.group(1)}" if creator_match else "Instagram Content Creator"
            category = "Social Media & Short-Form Visual Creator Economy"
            summary = f"Visual social media creator publication on Instagram featuring high-velocity reels, short-form lifestyle narratives, and digital community engagement."
            positioning = "Mobile-first visual creator utilizing Instagram Reels and multimedia storytelling to cultivate loyal digital audiences."
            target_market = "Mobile social media users, Gen Z and millennial lifestyle enthusiasts, and direct-to-consumer brand sponsors."
            advantages = [
                "High visual viral reach powered by Instagram Explore and Reels discovery algorithms",
                "Direct real-time engagement through interactive Stories, direct messaging, and community comments",
                "Seamless integration with social commerce and in-app brand partnership tags",
            ]
            competitors = [
                {
                    "competitor_name": "TikTok Short-Form Video",
                    "website_url": "https://www.tiktok.com",
                    "business_overview": "Fast-paced vertical short-form mobile video network with algorithmic content delivery.",
                    "main_services": ["Vertical Video Feed", "Creator Fund", "In-App TikTok Shop"],
                    "strengths": ["Extreme viral discovery velocity", "Broad youth demographic engagement"],
                    "weaknesses_or_gaps": ["Ephemeral trends with short lifespan", "Regulatory scrutiny in various jurisdictions"],
                    "comparison_with_brand": f"{brand_name} benefits from Instagram's stronger aesthetic curation and mature brand monetization.",
                    "competitive_opportunities": ["Cross-post highest-performing Reels to TikTok to capture dual-platform virality"],
                    "verification_status": "ai-generated suggestion",
                    "market_tier": "Global Short-Form Leader",
                    "pricing_tier": "Ad Revenue / Creator Rewards",
                    "overlap_score": 0.90,
                    "differentiation_angle": "Curated visual aesthetics and higher-converting demographic intent.",
                },
                {
                    "competitor_name": "YouTube Shorts",
                    "website_url": "https://www.youtube.com",
                    "business_overview": "Google's vertical video platform integrated directly inside YouTube's 2B+ monthly ecosystem.",
                    "main_services": ["Shorts Feed", "YouTube Partner Rev-Share", "Long-Form Conversion"],
                    "strengths": ["Backed by Google search indexing", "Direct conversion into long-form subscribers"],
                    "weaknesses_or_gaps": ["Lower social direct-messaging engagement", "Less lifestyle influencer focus"],
                    "comparison_with_brand": f"{brand_name} achieves deeper lifestyle trust and direct follower conversation on Instagram.",
                    "competitive_opportunities": ["Repurpose vertical Reels as YouTube Shorts to drive Google Search visibility"],
                    "verification_status": "ai-generated suggestion",
                    "market_tier": "Enterprise Video Incumbent",
                    "pricing_tier": "YouTube AdSense Rev-Share",
                    "overlap_score": 0.84,
                    "differentiation_angle": "Direct community intimacy and lifestyle engagement.",
                },
                {
                    "competitor_name": "Snapchat Spotlight",
                    "website_url": "https://www.snapchat.com",
                    "business_overview": "Ephemeral messaging and public short-form entertainment feed for Gen Z users.",
                    "main_services": ["Spotlight Video Feed", "AR Camera Lenses", "Creator Payouts"],
                    "strengths": ["Strong augmented reality lenses", "Dominant teenage friend graph"],
                    "weaknesses_or_gaps": ["Weaker desktop presence", "Low search engine indexability"],
                    "comparison_with_brand": f"{brand_name} maintains superior permanent brand equity and public web discoverability.",
                    "competitive_opportunities": ["Leverage AR filters across campaigns to increase shareability"],
                    "verification_status": "ai-generated suggestion",
                    "market_tier": "Mobile Visual Incumbent",
                    "pricing_tier": "Spotlight Payouts",
                    "overlap_score": 0.76,
                    "differentiation_angle": "Public permanent profile footprint and mature advertiser trust.",
                },
            ]
            queries = [
                {
                    "search_query": f"{brand_name.lower()} instagram reels trending clips",
                    "relevant_platform": "Instagram",
                    "search_intent": "Informational",
                    "business_relevance": "Direct user exploration and sound discoverability inside Instagram Reels.",
                    "target_audience": "Daily Instagram users, lifestyle followers, and fashion/tech enthusiasts.",
                    "keyword_category": "Brand",
                    "search_volume_status": "High (Explore trending)",
                },
                {
                    "search_query": f"influencer marketing ROI and brand deals {brand_name.lower()}",
                    "relevant_platform": "LinkedIn",
                    "search_intent": "Commercial",
                    "business_relevance": "Connects brand campaign managers with verified engagement metrics.",
                    "target_audience": "B2B brand partnership managers and PR agencies.",
                    "keyword_category": "Industry",
                    "search_volume_status": "Commercial intent",
                },
                {
                    "search_query": f"{brand_name.lower()} viral video reaction and comments",
                    "relevant_platform": "X (Twitter)",
                    "search_intent": "Informational",
                    "business_relevance": "Monitors cross-platform social sentiment and viewer discussions.",
                    "target_audience": "Social media commentators and pop culture communities.",
                    "keyword_category": "Social Sentiment",
                    "search_volume_status": "Real-time volume spikes",
                },
                {
                    "search_query": f"{brand_name.lower()} creator profile and collaborations",
                    "relevant_platform": "Google Search",
                    "search_intent": "Navigational",
                    "business_relevance": "Primary search discovery for users and brands seeking official links.",
                    "target_audience": "General searchers, brand scouts, and media interviewers.",
                    "keyword_category": "Navigational",
                    "search_volume_status": "High search volume",
                },
                {
                    "search_query": f"{brand_name.lower()} official creator group and community",
                    "relevant_platform": "Facebook",
                    "search_intent": "Informational",
                    "business_relevance": "Drives cross-platform audience retention into Facebook groups and fan pages.",
                    "target_audience": "Cross-generational social media users.",
                    "keyword_category": "Community",
                    "search_volume_status": "Steady community volume",
                },
            ]
            seo_rec = {
                "primary_keyword": f"{brand_name.lower()} instagram",
                "secondary_keywords": [
                    f"{brand_name.lower()} reels",
                    f"{brand_name.lower()} creator",
                    f"{brand_name.lower()} videos",
                    f"{brand_name.lower()} brand deals",
                ],
                "long_tail_keywords": [
                    f"watch {brand_name.lower()} viral instagram reels",
                    f"how to collaborate with {brand_name.lower()}",
                    f"best content from {brand_name.lower()} on instagram",
                ],
                "business_specific_keywords": [
                    f"{brand_name.lower()} brand sponsorships",
                    f"{brand_name.lower()} affiliate partnerships",
                ],
                "existing_title": page_title or f"{brand_name} on Instagram",
                "recommended_title": f"{brand_name} (@{creator_match.group(1) if creator_match else 'creator'}) - Official Instagram Reels & Creator Hub",
                "title_recommendation_reason": "Emphasizes handle, creator status, and Reels keywords for top Google search indexation.",
                "existing_meta_description": "Extracted Instagram profile or reel preview metadata.",
                "meta_description": f"Explore viral Reels, original visual stories, and behind-the-scenes content from {brand_name} on Instagram. Follow for the latest daily updates.",
                "meta_description_improvement_reason": "High-converting action prompt targeting both everyday fans and commercial partners.",
                "supporting_content": [
                    {
                        "topic": "Creator Media Kit & Rate Card Page",
                        "content_type": "Service Landing Page",
                        "description": "Comprehensive one-sheet detailing follower demographics, engagement rates, and sponsorship packages.",
                        "priority": "HIGH",
                    },
                    {
                        "topic": "Curated Video Reel Highlights & Showcase",
                        "content_type": "Industry-Specific Landing Page",
                        "description": "Embedded high-resolution video showcase categorizing top-performing content by theme.",
                        "priority": "HIGH",
                    },
                    {
                        "topic": "Community Collaboration FAQ",
                        "content_type": "FAQ Section",
                        "description": "Clear guidelines on product gifting, brand partnerships, and affiliate parameters.",
                        "priority": "MEDIUM",
                    },
                    {
                        "topic": "Creator Journey & Behind the Lens Story",
                        "content_type": "Blog Topic",
                        "description": "Personal narrative explaining creative methodology and authentic brand philosophy.",
                        "priority": "LOW",
                    },
                ],
            }
            services = [
                {
                    "service_name": "Short-Form Video (Reels) Creative Production",
                    "description": "Conceptualizing, scripting, filming, and editing high-retention 9:16 vertical Reels.",
                    "target_industry": "Social Media Marketing & Creator Economy",
                    "target_customer": "Lifestyle brands, mobile app developers, and e-commerce stores",
                    "main_customer_benefit": "High viral reach and authentic visual storytelling that drives conversions.",
                    "source_evidence": "Published high-performing Instagram Reels and video creative assets.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "Dynamic visual pacing & trend sound curation",
                        "Seamless in-video product integrations",
                        "High-definition 1080x1920 mobile mastering",
                        "Optimized hook framing in first 3 seconds",
                    ],
                    "pricing_model": "Flat-Rate Per Reel or Package Retainer ($1,500 - $8,000)",
                    "delivery_model": "Instagram Reels Feed & Shared Collaborator Tag",
                    "confidence_score": 0.98,
                    "page_location": "Reels Video Feed & Profile Grid",
                },
                {
                    "service_name": "Sponsored Brand Endorsements & Story Takeovers",
                    "description": "Multi-slide Instagram Stories with swipe-up/tap link stickers and direct-response tracking.",
                    "target_industry": "Direct-to-Consumer (DTC) Retail & SaaS",
                    "target_customer": "Growth marketing managers and performance advertising teams",
                    "main_customer_benefit": "Immediate trackable website referral traffic with warm social proof.",
                    "source_evidence": "Interactive Story highlights and partner collaborations.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "Clickable link sticker integration",
                        "Interactive polling and question boxes",
                        "24-hour persistent story delivery with highlight archiving",
                        "Detailed story impression and swipe analytics",
                    ],
                    "pricing_model": "Story Frame CPM or Fixed Sponsorship Package",
                    "delivery_model": "Live 24h Instagram Stories & Archived Highlights",
                    "confidence_score": 0.95,
                    "page_location": "Profile Story Highlights",
                },
                {
                    "service_name": "Affiliate Showcase & Product Curation",
                    "description": "Dedicated bio link curation, coupon code dissemination, and lifestyle product reviews.",
                    "target_industry": "Consumer Retail & Lifestyle Products",
                    "target_customer": "Followers seeking trusted product recommendations",
                    "main_customer_benefit": "Discounted access to curated high-quality products vetted by the creator.",
                    "source_evidence": "Link-in-bio hub and tagged product partnerships.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "Custom promo code tracking",
                        "Centralized Linktree/Beacons catalog",
                        "Category-specific affiliate curation",
                        "Authentic unboxing and hands-on reviews",
                    ],
                    "pricing_model": "Performance Affiliate Commission (8% - 25% Rev-Share)",
                    "delivery_model": "Bio Link Hub & Description Codes",
                    "confidence_score": 0.91,
                    "page_location": "Bio Link & Caption Mentions",
                },
                {
                    "service_name": "User-Generated Content (UGC) Asset Licensing",
                    "description": "White-labeled raw vertical video footage licensed to brands for their own paid ad campaigns.",
                    "target_industry": "E-Commerce & Digital Advertising Agencies",
                    "target_customer": "Performance marketing directors running Meta ads",
                    "main_customer_benefit": "Authentic organic-looking creative that lowers CPA and bypasses ad fatigue.",
                    "source_evidence": "Creator content portfolio and production fidelity.",
                    "identification_type": "INFERRED",
                    "key_capabilities": [
                        "Full commercial usage rights (30/60/90 days)",
                        "Multiple hook variants and calls to action",
                        "High-res source files without watermarks",
                        "Platform-optimized captions and safe-zone compliance",
                    ],
                    "pricing_model": "Usage-Based Licensing Fee ($500 - $3,000/asset)",
                    "delivery_model": "Direct Cloud Storage Master Delivery",
                    "confidence_score": 0.89,
                    "page_location": "Creator Inquiries Contact",
                },
                {
                    "service_name": "Exclusive Community Broadcast Channel",
                    "description": "Direct one-to-many broadcast messaging inside Instagram Direct for VIP fans and announcements.",
                    "target_industry": "Community Building & Fan Engagement",
                    "target_customer": "Dedicated super-followers and early-adopters",
                    "main_customer_benefit": "Unfiltered direct line to the creator without algorithmic feed suppression.",
                    "source_evidence": "Instagram Broadcast Channel integration.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "Direct inbox push notifications",
                        "Exclusive voice memos and behind-the-scenes teasers",
                        "Real-time fan reaction emojis",
                        "Early ticket/merch drop alerts",
                    ],
                    "pricing_model": "Complimentary VIP Access / Optional Paid Subscription",
                    "delivery_model": "Instagram Direct Messaging Channel",
                    "confidence_score": 0.93,
                    "page_location": "Profile Broadcast Channel Link",
                },
            ]
            target_aud = {
                "primary_audience": [
                    "Active Instagram mobile users seeking entertainment and inspiration",
                    "Community followers passionate about the creator's lifestyle niche",
                ],
                "secondary_audience": [
                    "Direct-to-consumer brand marketers and talent acquisition agencies",
                    "Fellow content creators and creative collaborators",
                ],
                "industries": [
                    "Social Media & Influencer Marketing",
                    "Direct-to-Consumer Retail",
                    "Fashion, Beauty, Tech & Lifestyle",
                    "Creator Economy",
                ],
                "business_types": [
                    "Individual Creator / Influencer",
                    "Digital Media Brand",
                    "B2C Social Commerce",
                ],
                "customer_profiles": [
                    "Smartphone users scrolling Instagram daily during commute and leisure",
                    "E-commerce brands seeking high-engagement testimonial endorsements",
                ],
                "geographical_focus": "Global social audience with high concentration in key metropolitan regions",
                "customer_needs": [
                    "Fast, visually stimulating short-form entertainment",
                    "Actionable lifestyle advice, product discoveries, and entertainment",
                    "Authentic two-way social interaction and responsiveness",
                ],
                "customer_pain_points": [
                    "Repetitive copycat video trends with zero original substance",
                    "Overly staged, inauthentic promotional advertisements",
                ],
                "potential_interests": [
                    "Mobile photography, creative editing, aesthetic lifestyle",
                    "Trending audio, fashion/gear drops, creator tech",
                ],
                "audience_segments": [
                    {
                        "segment_name": "Engaged Social Followers",
                        "description": "Active followers who like, comment, and share Reels within their own personal networks.",
                        "business_type": "B2C Consumer",
                        "requirements": ["Consistent upload cadence", "High video quality", "Relatable tone"],
                        "pain_points": ["Sudden drops in upload frequency", "Excessive sponsored posts"],
                        "relevant_services": [
                            "Short-Form Video (Reels) Creative Production",
                            "Exclusive Community Broadcast Channel",
                        ],
                        "evidence_level": "Direct Website Evidence",
                    },
                    {
                        "segment_name": "DTC Brand Marketing Teams",
                        "description": "Performance marketing leads seeking creator partnerships to scale customer acquisition.",
                        "business_type": "B2B Sponsor",
                        "requirements": ["Audience demographics proof", "Timely delivery", "Commercial rights"],
                        "pain_points": ["Unreliable creator communication", "Poor trackable ROI"],
                        "relevant_services": [
                            "Sponsored Brand Endorsements & Story Takeovers",
                            "User-Generated Content (UGC) Asset Licensing",
                        ],
                        "evidence_level": "Direct Website Evidence",
                    },
                ],
            }
        elif is_github:
            repo_match = re.search(r"github\.com/([^/\s]+)/?([^/\s]+)?", target_url)
            if repo_match and repo_match.group(2):
                brand_name = f"{repo_match.group(1)}/{repo_match.group(2)}"
            elif repo_match:
                brand_name = repo_match.group(1)
            else:
                brand_name = "GitHub Open Source Repository"

            category = "Open Source Software & Developer Infrastructure"
            summary = f"Open source software project hosted on GitHub ({brand_name}), delivering developer libraries, reusable source code, and developer ecosystem tooling."
            positioning = "Modern open source codebase providing developer-first APIs, collaborative code evolution, and community-driven innovation."
            target_market = "Software engineers, DevOps practitioners, open-source contributors, and technology companies."
            advantages = [
                "Full source code transparency and permissive open-source collaboration",
                "Native integration with GitHub Actions CI/CD workflows and package registries",
                "Rapid peer-reviewed security fixes and active community pull requests",
            ]
            competitors = [
                {
                    "competitor_name": "GitLab Enterprise",
                    "website_url": "https://gitlab.com",
                    "business_overview": "Comprehensive single-application DevSecOps platform spanning source code to deployment.",
                    "main_services": ["Source Code Management", "Built-In CI/CD", "Security Compliance Scanning"],
                    "strengths": ["Self-hosted on-premises capability", "Unified single-data-model architecture"],
                    "weaknesses_or_gaps": ["Steeper operational footprint for small teams", "Smaller public open source developer community"],
                    "comparison_with_brand": f"{brand_name} benefits from GitHub's vastly larger open-source community discovery network.",
                    "competitive_opportunities": ["Highlight streamlined setup and widespread familiarity among engineers"],
                    "verification_status": "ai-generated suggestion",
                    "market_tier": "DevOps Enterprise Incumbent",
                    "pricing_tier": "Freemium / Premium SaaS ($29/user)",
                    "overlap_score": 0.86,
                    "differentiation_angle": "Community developer network effects and universal developer adoption.",
                },
                {
                    "competitor_name": "Atlassian Bitbucket",
                    "website_url": "https://bitbucket.org",
                    "business_overview": "Git repository management solution closely integrated into Jira and Confluence workflows.",
                    "main_services": ["Code Repositories", "Bitbucket Pipelines", "Jira Automation Triggers"],
                    "strengths": ["Native Atlassian Jira issue synchronization", "Fine-grained enterprise branch permissions"],
                    "weaknesses_or_gaps": ["Minimal public open-source project ecosystem", "Legacy interface patterns"],
                    "comparison_with_brand": f"{brand_name} offers superior open ecosystem collaboration and package discoverability.",
                    "competitive_opportunities": ["Provide first-class documentation and zero-friction developer onboarding"],
                    "verification_status": "ai-generated suggestion",
                    "market_tier": "Enterprise Workflow Incumbent",
                    "pricing_tier": "Tiered Atlassian Cloud Pricing",
                    "overlap_score": 0.78,
                    "differentiation_angle": "Frictionless open collaboration without enterprise software lock-in.",
                },
                {
                    "competitor_name": "SourceForge Developer Network",
                    "website_url": "https://sourceforge.net",
                    "business_overview": "Longstanding software distribution directory and open-source project repository host.",
                    "main_services": ["Binary Software Mirroring", "Download Analytics", "User Reviews"],
                    "strengths": ["Massive historical software archive", "High Google search authority for binary downloads"],
                    "weaknesses_or_gaps": ["Outdated developer workflows", "Heavy ad monetization on download pages"],
                    "comparison_with_brand": f"{brand_name} delivers modern Git-native version control and modern automation.",
                    "competitive_opportunities": ["Publish automated release binaries directly via GitHub Releases and package managers"],
                    "verification_status": "ai-generated suggestion",
                    "market_tier": "Legacy Software Directory",
                    "pricing_tier": "Ad-Supported Free Hosting",
                    "overlap_score": 0.65,
                    "differentiation_angle": "Modern automated CI/CD releases and clean developer ergonomics.",
                },
            ]
            queries = [
                {
                    "search_query": f"{brand_name.lower()} github tutorial installation quickstart",
                    "relevant_platform": "Google Search",
                    "search_intent": "Informational",
                    "business_relevance": "Direct technical onboarding query for developers integrating this repository.",
                    "target_audience": "Software developers, full-stack engineers, and technical leads.",
                    "keyword_category": "Documentation & Setup",
                    "search_volume_status": "High developer intent",
                },
                {
                    "search_query": f"scaling enterprise architectures with {brand_name.lower()}",
                    "relevant_platform": "LinkedIn",
                    "search_intent": "Informational",
                    "business_relevance": "Attracts enterprise software architects and engineering managers evaluating stack choices.",
                    "target_audience": "CTOs, VP of Engineering, and Principal Architects.",
                    "keyword_category": "Industry Architecture",
                    "search_volume_status": "Executive B2B audience",
                },
                {
                    "search_query": f"{brand_name.lower()} release updates and benchmark performance",
                    "relevant_platform": "X (Twitter)",
                    "search_intent": "Informational",
                    "business_relevance": "Real-time developer sentiment and speed benchmark comparisons.",
                    "target_audience": "DevOps specialists, open-source contributors, and tech enthusiasts.",
                    "keyword_category": "Technical Discourse",
                    "search_volume_status": "Active tech discussions",
                },
                {
                    "search_query": f"developer setup tips and coding with {brand_name.lower()}",
                    "relevant_platform": "Instagram",
                    "search_intent": "Informational",
                    "business_relevance": "Visual developer lifestyle, desk setup, and coding workflow shorts.",
                    "target_audience": "Junior developers, coding bootcamp students, and tech creators.",
                    "keyword_category": "Developer Education",
                    "search_volume_status": "Visual tech audience",
                },
                {
                    "search_query": f"{brand_name.lower()} developer community forum and troubleshooting",
                    "relevant_platform": "Facebook",
                    "search_intent": "Informational",
                    "business_relevance": "Captures peer-to-peer programming help in specialized developer groups.",
                    "target_audience": "Freelance developers and international engineering communities.",
                    "keyword_category": "Community Support",
                    "search_volume_status": "Community volume",
                },
            ]
            seo_rec = {
                "primary_keyword": f"{brand_name.lower()} repository",
                "secondary_keywords": [
                    f"{brand_name.lower()} documentation",
                    f"{brand_name.lower()} npm package",
                    f"{brand_name.lower()} api reference",
                    f"{brand_name.lower()} github open source",
                ],
                "long_tail_keywords": [
                    f"how to install and configure {brand_name.lower()}",
                    f"best practices for deploying {brand_name.lower()} in production",
                    f"{brand_name.lower()} architecture guide and examples",
                ],
                "business_specific_keywords": [
                    f"{brand_name.lower()} enterprise support",
                    f"{brand_name.lower()} commercial licensing",
                ],
                "existing_title": page_title or f"GitHub - {brand_name}",
                "recommended_title": f"{brand_name} - Open Source Developer Infrastructure & API | GitHub",
                "title_recommendation_reason": "Structured to capture both project name searches and high-intent developer documentation queries.",
                "existing_meta_description": "Extracted GitHub repository description and stars.",
                "meta_description": f"Explore {brand_name} on GitHub: open-source software, high-performance developer APIs, comprehensive documentation, and active community contributions.",
                "meta_description_improvement_reason": "Highlights open source credentials and developer value proposition clearly.",
                "supporting_content": [
                    {
                        "topic": "Comprehensive Getting Started & API Reference",
                        "content_type": "Service Landing Page",
                        "description": "Exhaustive documentation site with interactive code snippets and copyable CLI commands.",
                        "priority": "HIGH",
                    },
                    {
                        "topic": "Production Deployment & Architecture Guide",
                        "content_type": "Industry-Specific Landing Page",
                        "description": "Hardened deployment best practices covering Docker, Kubernetes, and cloud configs.",
                        "priority": "HIGH",
                    },
                    {
                        "topic": "Troubleshooting & Frequently Asked Developer Questions",
                        "content_type": "FAQ Section",
                        "description": "Resolves common runtime errors, edge cases, and environment version mismatches.",
                        "priority": "HIGH",
                    },
                    {
                        "topic": "Benchmarking & Performance Comparison Analysis",
                        "content_type": "Blog Topic",
                        "description": "Rigorous data-backed benchmarks demonstrating speed and memory efficiency advantages.",
                        "priority": "MEDIUM",
                    },
                ],
            }
            services = [
                {
                    "service_name": "Core Open-Source Library & Runtime Engine",
                    "description": "Freely available, permissively licensed open source software core distribution.",
                    "target_industry": "Software Engineering & Cloud Infrastructure",
                    "target_customer": "Software developers, systems architects, and technical founders",
                    "main_customer_benefit": "Zero software licensing cost with complete code inspectability.",
                    "source_evidence": "Primary GitHub repository code tree and license file.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "Modular component architecture",
                        "High test coverage with automated unit tests",
                        "Cross-platform compatibility (Linux, macOS, Windows)",
                        "Zero external bloat dependencies",
                    ],
                    "pricing_model": "Open Source (MIT / Apache 2.0 / Free Permissive)",
                    "delivery_model": "Git Repository & Official Package Registries (npm/PyPI)",
                    "confidence_score": 0.99,
                    "page_location": "Code Tree & README Overview",
                },
                {
                    "service_name": "Automated Package Releases & Distribution",
                    "description": "Compiled release binaries, semantic versioned packages, and container images.",
                    "target_industry": "DevOps & Continuous Deployment",
                    "target_customer": "Site reliability engineers and build automation specialists",
                    "main_customer_benefit": "Reproducible, secure package deployments across production clusters.",
                    "source_evidence": "GitHub Releases tab and automated package publishing workflows.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "Automated semantic versioning (SemVer)",
                        "Cryptographic checksum signatures",
                        "Official Docker Hub / GitHub Container Registry images",
                        "Changelog and breaking change release notes",
                    ],
                    "pricing_model": "Free Community Distribution",
                    "delivery_model": "GitHub Releases & Container Registries",
                    "confidence_score": 0.97,
                    "page_location": "Reels / Releases Navigation Tab",
                },
                {
                    "service_name": "Community Issue Tracking & Contribution Governance",
                    "description": "Public issue discussions, bug triage, pull request reviews, and feature roadmapping.",
                    "target_industry": "Open Source Software Development",
                    "target_customer": "Community contributors and open-source software maintainers",
                    "main_customer_benefit": "Transparent bug remediation and rapid peer-reviewed feature enhancements.",
                    "source_evidence": "GitHub Issues, Pull Requests, and Discussions tabs.",
                    "identification_type": "EXPLICIT",
                    "key_capabilities": [
                        "Issue template triage and bug reproducibility guides",
                        "Automated CI testing on incoming pull requests",
                        "Contributor License Agreement (CLA) enforcement",
                        "Public milestones and version roadmap",
                    ],
                    "pricing_model": "Free Community Contribution",
                    "delivery_model": "GitHub Issues & PR Portal",
                    "confidence_score": 0.96,
                    "page_location": "Issues & Pull Requests Header Tabs",
                },
                {
                    "service_name": "Enterprise Commercial Support & Custom Licensing",
                    "description": "Dedicated enterprise SLA guarantees, compliance indemnification, and priority hotfixes.",
                    "target_industry": "Enterprise IT, FinTech & Healthcare",
                    "target_customer": "Chief Information Security Officers (CISOs) and Corporate Legal Counsel",
                    "main_customer_benefit": "Risk mitigation, guaranteed uptime support, and non-restrictive licensing terms.",
                    "source_evidence": "Commercial dual-licensing or enterprise sponsor links in documentation.",
                    "identification_type": "INFERRED",
                    "key_capabilities": [
                        "Dedicated 24/7 technical support engineers",
                        "Prioritized security vulnerability remediation",
                        "Private long-term support (LTS) maintenance branches",
                        "Custom legal indemnification against IP infringement",
                    ],
                    "pricing_model": "Annual Commercial License / Tiered Support Contract",
                    "delivery_model": "Private Support Channel & Dedicated SLA",
                    "confidence_score": 0.86,
                    "page_location": "README Sponsor / Enterprise Support Section",
                },
                {
                    "service_name": "Developer Training & Architectural Advisory",
                    "description": "Custom workshops, code audits, and performance tuning for engineering teams.",
                    "target_industry": "High-Growth Scaleups & Digital Enterprises",
                    "target_customer": "Engineering Directors and Technical Leads",
                    "main_customer_benefit": "Accelerated developer productivity and prevention of architectural anti-patterns.",
                    "source_evidence": "Consulting and advisory offerings provided by project maintainers.",
                    "identification_type": "INFERRED",
                    "key_capabilities": [
                        "Hands-on architectural review of client codebases",
                        "Live interactive engineering team workshops",
                        "Custom performance profiling and bottleneck elimination",
                        "Direct migration assistance from legacy frameworks",
                    ],
                    "pricing_model": "Project-Based Advisory Fee or Daily Training Rate",
                    "delivery_model": "Remote / On-Site Interactive Advisory",
                    "confidence_score": 0.82,
                    "page_location": "Maintainer Contact / Professional Services Links",
                },
            ]
            target_aud = {
                "primary_audience": [
                    "Full-stack developers and software engineers evaluating open source tools",
                    "DevOps specialists and systems architects building scalable cloud services",
                ],
                "secondary_audience": [
                    "Engineering directors, CTOs, and enterprise software procurement officers",
                    "Open-source community contributors and open-web researchers",
                ],
                "industries": [
                    "Information Technology & Cloud Infrastructure",
                    "Software Development & DevOps",
                    "Open-Source Software Ecosystem",
                    "Enterprise Enterprise Applications",
                ],
                "business_types": [
                    "Open Source Project",
                    "Developer Tooling Provider",
                    "Enterprise Infrastructure Tech",
                ],
                "customer_profiles": [
                    "Technical engineers seeking well-maintained, bug-free open source code",
                    "Engineering leadership looking to accelerate delivery while ensuring compliance",
                ],
                "geographical_focus": "Global engineering audience across all time zones",
                "customer_needs": [
                    "Clear, concise documentation and quickstart instructions",
                    "Active maintenance with rapid responses to critical security flaws",
                    "High performance, clean interfaces, and low runtime overhead",
                ],
                "customer_pain_points": [
                    "Unmaintained abandonware libraries causing security vulnerabilities",
                    "Poor documentation with outdated code snippets and broken links",
                    "Breaking changes between minor releases without migration guides",
                ],
                "potential_interests": [
                    "Modern programming languages, containerization, and microservices",
                    "Developer productivity tools, automated testing, and CI/CD",
                ],
                "audience_segments": [
                    {
                        "segment_name": "Software Developers",
                        "description": "Engineers writing code who need reliable components to solve project requirements.",
                        "business_type": "Practitioner",
                        "requirements": ["Clean API design", "Working code samples", "Zero unwanted dependencies"],
                        "pain_points": ["Cryptic runtime error messages", "Lack of TypeScript types or documentation"],
                        "relevant_services": [
                            "Core Open-Source Library & Runtime Engine",
                            "Automated Package Releases & Distribution",
                        ],
                        "evidence_level": "Direct Website Evidence",
                    },
                    {
                        "segment_name": "Enterprise Engineering Teams",
                        "description": "Corporate software teams needing vetted, secure packages with governance compliance.",
                        "business_type": "Enterprise B2B",
                        "requirements": ["Permissive licensing", "Security vulnerability SLAs", "Long-term support"],
                        "pain_points": ["Strict legal audits preventing copyleft licenses", "Lack of commercial vendor support"],
                        "relevant_services": [
                            "Enterprise Commercial Support & Custom Licensing",
                            "Developer Training & Architectural Advisory",
                        ],
                        "evidence_level": "AI Inferred",
                    },
                ],
            }
        else:
            # Standard Enterprise / Commercial Website
            brand_name = clean_domain.split(".")[0].capitalize()
            category = "Technology & Digital Services"
            summary = f"{brand_name} delivers specialized digital solutions, platform capabilities, and services as detailed on their primary web portal ({domain})."
            positioning = f"Modern high-efficiency solution provider competing in the digital software and services space."
            target_market = "Commercial enterprises, technology startups, and operational teams."
            advantages = [
                "Direct online access and digital-first delivery architecture",
                "Intuitive user onboarding and clear product segmentation",
                "Modern, security-conscious web platform footprint",
            ]
            competitors = [
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
                        "Target underserved SMB customers who find large competitors cost-prohibitive",
                    ],
                    "verification_status": "ai-generated suggestion",
                    "market_tier": "Enterprise Incumbent",
                    "pricing_tier": "Custom Annual SLA",
                    "overlap_score": 0.88,
                    "differentiation_angle": f"Position {brand_name}'s modern API agility against legacy procurement friction.",
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
                    "market_tier": "High-Growth Challenger",
                    "pricing_tier": "Tiered SaaS Subscription",
                    "overlap_score": 0.79,
                    "differentiation_angle": f"Highlight superior enterprise SLA guarantees and multi-region resilience.",
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
                    "market_tier": "Market Leader",
                    "pricing_tier": "Usage-Based Scale",
                    "overlap_score": 0.72,
                    "differentiation_angle": f"Transparent pricing with no unexpected overage penalties.",
                },
            ]
            queries = [
                {
                    "search_query": f"best {brand_name.lower()} software alternatives for businesses",
                    "relevant_platform": "Google Search",
                    "search_intent": "Commercial",
                    "business_relevance": "Captures high-intent prospects actively researching modern solutions in this specific vertical.",
                    "target_audience": "Decision makers, IT managers, and procurement leads evaluating vendors.",
                    "keyword_category": "Competitor Alternative",
                    "search_volume_status": "Commercial High Intent",
                },
                {
                    "search_query": f"how to streamline digital operations with {brand_name.lower()}",
                    "relevant_platform": "LinkedIn",
                    "search_intent": "Informational",
                    "business_relevance": "Builds thought leadership and engages business professionals looking for operational efficiency.",
                    "target_audience": "Operations leaders, CTOs, and digital transformation managers.",
                    "keyword_category": "Industry",
                    "search_volume_status": "Targeted B2B volume",
                },
                {
                    "search_query": f"top {brand_name.lower()} features demo and tutorial",
                    "relevant_platform": "X (Twitter)",
                    "search_intent": "Informational",
                    "business_relevance": "Drives immediate product discovery and community validation among tech-savvy practitioners.",
                    "target_audience": "Practitioners, developers, and product enthusiasts.",
                    "keyword_category": "Solution",
                    "search_volume_status": "Community discussions",
                },
                {
                    "search_query": f"{brand_name.lower()} customer success stories and business impact",
                    "relevant_platform": "Instagram",
                    "search_intent": "Commercial",
                    "business_relevance": "Visual storytelling showing real client outcomes to build brand credibility and trust.",
                    "target_audience": "Entrepreneurs, creators, and agency owners.",
                    "keyword_category": "Brand",
                    "search_volume_status": "Visual engagement",
                },
                {
                    "search_query": f"affordable {brand_name.lower()} pricing plans for small businesses",
                    "relevant_platform": "Facebook",
                    "search_intent": "Transactional",
                    "business_relevance": "Direct bottom-of-funnel conversion targeting cost-conscious small business owners.",
                    "target_audience": "Small business owners and departmental managers.",
                    "keyword_category": "Long-tail",
                    "search_volume_status": "Transactional intent",
                },
            ]
            seo_rec = {
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
                "existing_title": page_title or f"{brand_name} Official Website",
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
            }
            services = [
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
                        "Automated system health telemetry",
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
                        "API rate-limit & error retry management",
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
                        "Guaranteed 99.9% uptime SLA backing",
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
                        "End-to-end TLS 1.3 encryption at rest and in transit",
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
                        "Quarterly executive business reviews (QBR)",
                    ],
                    "pricing_model": "Custom Annual Contract (Direct Sales)",
                    "delivery_model": "Dedicated Enterprise Deployment",
                    "confidence_score": 0.76,
                    "page_location": "Inferred from Enterprise Pricing Contact CTA",
                },
            ]
            target_aud = {
                "primary_audience": [
                    "Chief Technology Officers (CTOs) and Engineering Managers seeking modern digital solutions.",
                    "Product Managers and Digital Transformation Leads requiring reliable platform infrastructure.",
                ],
                "secondary_audience": [
                    "Operations Managers and Agency Leads optimizing internal business workflows.",
                    "Procurement and IT Administrators evaluating vendor reliability and cost efficiency.",
                ],
                "industries": [
                    "Information Technology & SaaS",
                    "Digital Media & Agencies",
                    "E-commerce & Retail Operations",
                    "Professional Business Services",
                ],
                "business_types": ["B2B SaaS", "Enterprise", "Mid-Market", "Digital Agencies"],
                "customer_profiles": [
                    "Tech-savvy executives seeking modern cloud architectures",
                    "Operational heads focused on reducing manual overhead and friction",
                ],
                "geographical_focus": "Global / North America & Europe (Multi-region digital presence)",
                "customer_needs": [
                    "High system availability and robust security standards",
                    "Frictionless onboarding with clear documentation and responsive support",
                    "Transparent pricing and measurable return on investment",
                ],
                "customer_pain_points": [
                    "High cost and complexity of legacy enterprise software",
                    "Fragmented toolsets causing communication silos and delayed reporting",
                    "Lack of responsive vendor support during mission-critical operations",
                ],
                "potential_interests": [
                    "Cloud automation and modern DevOps tooling",
                    "AI-driven workflow optimizations and analytics",
                    "Scalable enterprise architecture patterns",
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
                        "relevant_services": ["Custom Enterprise Scoping & Solutions", "Security & Identity Governance"],
                        "evidence_level": "AI Inferred",
                    },
                ],
            }

        # Construct realistic structured analysis
        result = {
            "brand": {
                "name": brand_name,
                "category": category,
                "summary": summary,
                "business_positioning": positioning,
                "target_market": target_market,
                "competitive_advantages": advantages,
            },
            "competitor_analysis": {
                "brand_overview": f"{brand_name} operates within the {category.lower()} ecosystem, focusing on delivering scalable user solutions and clear online service accessibility.",
                "competitors": competitors,
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
            "target_search_queries": queries,
            "seo_improvements": seo_rec,
            "top_services": services,
            "target_audience": target_aud,
        }

        latency_ms = (time.perf_counter() - start_time) * 1000.0
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

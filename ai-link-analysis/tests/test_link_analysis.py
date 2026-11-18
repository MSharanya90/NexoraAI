"""Comprehensive test suite covering URL hygiene, SSRF security, HTML parsing, LLM fallback, and API endpoints."""

import json
import socket
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse
import httpx
import pytest
from starlette.testclient import TestClient

from app.main import app
from app.schemas import ExtractedWebsiteData, LinkAnalysisResponse
from app.services.llm_service import BaseLLMProvider, LLMProviderError, MockLLMProvider
from app.services.website_extractor import WebsiteExtractionError, WebsiteExtractor
from app.utils.validators import (
    SSRFValidationError,
    is_safe_ip,
    resolve_and_validate_hostname,
    validate_target_url,
)

client = TestClient(app)


# ============================================================================
# 1. URL & SSRF VALIDATION TESTS
# ============================================================================

@patch("app.utils.validators.resolve_and_validate_hostname", return_value=(True, ["93.184.216.34"], None))
def test_valid_public_urls(mock_dns):
    """Verify standard public web URLs pass validation with mocked safe public IP."""
    valid_urls = [
        "https://example.com",
        "http://example.org/path?query=1",
        "https://www.google.com",
    ]
    for url in valid_urls:
        is_safe, error_msg, parsed = validate_target_url(url)
        assert is_safe is True, f"Failed for valid URL {url}: {error_msg}"
        assert error_msg is None
        assert parsed is not None


def test_invalid_url_schemes_and_formats():
    """Verify non-HTTP/HTTPS schemes and malformed URLs are rejected."""
    invalid_cases = [
        ("ftp://ftp.example.com", "Unsupported URL scheme"),
        ("file:///etc/passwd", "Unsupported URL scheme"),
        ("javascript:alert(1)", "Unsupported URL scheme"),
        ("gopher://gopher.example.com", "Unsupported URL scheme"),
        ("not-a-valid-url", "Unsupported URL scheme"),
        ("http://", "URL must contain a valid hostname"),
        ("", "URL must be a non-empty string"),
    ]
    for url, expected_err in invalid_cases:
        is_safe, error_msg, _ = validate_target_url(url)
        assert is_safe is False
        assert expected_err.lower() in error_msg.lower()


def test_localhost_and_loopback_rejection():
    """Verify loopback addresses and localhost are blocked."""
    loopback_cases = [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8080/admin",
        "http://127.0.1.1",
        "http://[::1]",
    ]
    for url in loopback_cases:
        is_safe, error_msg, _ = validate_target_url(url)
        assert is_safe is False, f"Expected {url} to be blocked"
        assert any(term in error_msg.lower() for term in ["loopback", "localhost", "blocked", "prohibited"])


def test_private_and_cloud_metadata_ip_rejection():
    """Verify RFC 1918 private IPs and cloud metadata endpoints are strictly rejected."""
    private_cases = [
        "http://10.0.0.1",
        "http://10.255.255.255",
        "http://172.16.0.1",
        "http://172.31.255.255",
        "http://192.168.1.1",
        "http://192.168.0.254",
        "http://169.254.169.254",  # AWS/GCP IMDS
        "http://100.100.100.200",  # Alibaba Cloud IMDS
        "http://[fc00::1]",        # IPv6 ULA
        "http://[fe80::1]",        # IPv6 Link-Local
    ]
    for url in private_cases:
        is_safe, error_msg, _ = validate_target_url(url)
        assert is_safe is False, f"Expected {url} to be blocked"
        assert any(term in error_msg.lower() for term in ["private", "metadata", "link-local", "prohibited", "restricted"])


def test_internal_domain_suffixes_rejection():
    """Verify internal domain suffixes (.local, .internal, .onion, etc.) are blocked."""
    internal_domains = [
        "http://database.local",
        "http://service.internal",
        "http://backend.corp",
        "http://hidden.onion",
        "http://app.cluster.local",
    ]
    for url in internal_domains:
        is_safe, error_msg, _ = validate_target_url(url)
        assert is_safe is False
        assert "prohibited for internal isolation" in error_msg.lower()


def test_dns_resolution_failure():
    """Verify handling when DNS resolution fails."""
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("Name or service not known")):
        is_safe, ips, error_msg = resolve_and_validate_hostname("nonexistent-domain-xyz-9876543210.org")
        assert is_safe is False
        assert len(ips) == 0
        assert "dns resolution failed" in error_msg.lower()


def test_is_safe_ip_direct_checks():
    """Directly test IP classification helper."""
    assert is_safe_ip("8.8.8.8")[0] is True
    assert is_safe_ip("1.1.1.1")[0] is True
    assert is_safe_ip("127.0.0.1")[0] is False
    assert is_safe_ip("10.0.0.5")[0] is False
    assert is_safe_ip("192.168.1.50")[0] is False
    assert is_safe_ip("169.254.169.254")[0] is False
    assert is_safe_ip("::1")[0] is False
    assert is_safe_ip("invalid-ip")[0] is False


# ============================================================================
# 2. WEBSITE EXTRACTION & HTML PARSING TESTS
# ============================================================================

SAMPLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <title>Acme Cloud Solutions | Next-Gen Cloud Platform</title>
    <meta name="description" content="Acme Cloud provides high-performance enterprise cloud infrastructure, Kubernetes automation, and 24/7 technical advisory.">
    <link rel="canonical" href="https://acmecloud.example.com/">
    <meta property="og:title" content="Acme Cloud Solutions">
    <meta property="og:description" content="Next-generation cloud infrastructure for engineering teams.">
</head>
<body>
    <header>
        <nav>
            <a href="/products">Products</a>
            <a href="/pricing">Pricing</a>
            <a href="/about">About Us</a>
            <a href="https://twitter.com/acmecloud">Twitter</a>
        </nav>
    </header>
    <main>
        <h1>Next-Gen Cloud Infrastructure</h1>
        <p>Empower your DevOps team with automated infrastructure and global edge caching.</p>
        
        <h2>Enterprise Services</h2>
        <div class="services">
            <p>We provide automated cluster provisioning, 24/7 dedicated SRE advisory, and multi-region disaster recovery.</p>
        </div>
        
        <h2>About Acme Cloud</h2>
        <div id="about-us">
            <p>Founded in 2021, Acme Cloud powers over 5,000 engineering teams worldwide with zero downtime architecture.</p>
        </div>

        <h3>Contact Our Team</h3>
        <p>Email us at sales@acmecloud.example.com or call +1 555-0199.</p>
        
        <img src="/img/diagram.png" alt="Architecture Diagram">
        <img src="/img/hero.png"> <!-- Missing alt attribute -->
    </main>
    <footer>
        <p>&copy; 2025 Acme Cloud Inc. All rights reserved.</p>
    </footer>
</body>
</html>"""


def test_html_parsing_and_metadata_extraction():
    """Verify BeautifulSoup extraction parses titles, headings, alts, and links accurately."""
    extractor = WebsiteExtractor()
    extracted = extractor.parse_html_content(
        html_text=SAMPLE_HTML,
        original_url="https://acmecloud.example.com",
        final_url="https://acmecloud.example.com/",
        status_code=200,
        latency_ms=120.5,
    )

    assert extracted.title == "Acme Cloud Solutions | Next-Gen Cloud Platform"
    assert "Acme Cloud provides high-performance" in extracted.meta_description
    assert extracted.canonical_url == "https://acmecloud.example.com/"
    assert "Next-Gen Cloud Infrastructure" in extracted.h1_headings
    assert "Enterprise Services" in extracted.h2_headings
    assert "Contact Our Team" in extracted.h3_headings

    # Verify images & alt counts
    assert extracted.images_count == 2
    assert extracted.images_missing_alt == 1  # hero.png lacks alt

    # Verify contact info
    assert "sales@acmecloud.example.com" in extracted.contact_info["emails"]

    # Verify technical issues generated deterministically
    issue_names = [issue["issue_name"] for issue in extracted.detected_technical_issues]
    assert "Images Missing Alt Attributes" in issue_names
    assert "SEO Title Tag" in issue_names
    assert "H1 Heading Hierarchy" in issue_names


def test_html_parsing_missing_elements():
    """Verify handling of barebones HTML missing title, meta, and H1."""
    bare_html = "<html><body><p>Bare text with no header or meta.</p></body></html>"
    extractor = WebsiteExtractor()
    extracted = extractor.parse_html_content(
        html_text=bare_html,
        original_url="https://empty.example.com",
        final_url="https://empty.example.com/",
        status_code=200,
        latency_ms=80.0,
    )

    assert extracted.title is None
    assert extracted.meta_description is None
    assert len(extracted.h1_headings) == 0

    # Ensure technical issues reflect failures
    issue_statuses = {i["issue_name"]: i["status"] for i in extracted.detected_technical_issues}
    assert issue_statuses["Missing SEO Title Tag"] == "FAIL"
    assert issue_statuses["Missing Meta Description"] == "FAIL"
    assert issue_statuses["Missing H1 Heading"] == "FAIL"


@pytest.mark.asyncio
async def test_redirect_to_private_ip_blocked():
    """Verify that a redirect leading to a private IP (e.g. 127.0.0.1) is blocked by SSRF checks."""
    extractor = WebsiteExtractor()

    mock_redirect_response = MagicMock()
    mock_redirect_response.is_redirect = True
    mock_redirect_response.headers = {"location": "http://127.0.0.1/secret"}

    with patch("app.services.website_extractor.validate_target_url") as mock_val:
        mock_val.side_effect = [
            (True, None, urlparse("https://example.com")),
            (False, "Loopback address (127.0.0.1) is prohibited", None),
        ]
        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_redirect_response
            with pytest.raises(SSRFValidationError) as exc_info:
                await extractor.fetch_html_securely("https://example.com")
            assert "ssrf protection" in str(exc_info.value).lower()


# ============================================================================
# 3. LLM SERVICE & JSON PARSING TESTS
# ============================================================================

class DummyProvider(BaseLLMProvider):
    async def generate_json(self, prompt: str, system_prompt: str):
        return {}, 0, 0, 0.0


def test_llm_json_extractor_clean():
    """Verify parsing clean JSON, markdown-wrapped JSON, and embedded JSON."""
    provider = DummyProvider(api_key="test", model="test")

    # Clean JSON
    res1 = provider.extract_and_parse_json('{"brand": "Acme", "score": 95}')
    assert res1["brand"] == "Acme"

    # Markdown fenced
    res2 = provider.extract_and_parse_json('```json\n{"status": "ok"}\n```')
    assert res2["status"] == "ok"

    # Surrounding prose
    res3 = provider.extract_and_parse_json('Here is your analysis:\n{"result": true}\nHope this helps!')
    assert res3["result"] is True


def test_llm_json_extractor_invalid():
    """Verify invalid JSON raises LLMProviderError."""
    provider = DummyProvider(api_key="test", model="test")
    with pytest.raises(LLMProviderError):
        provider.extract_and_parse_json("This is pure text with no JSON object at all.")


@pytest.mark.asyncio
async def test_mock_llm_provider_offline():
    """Verify MockLLMProvider produces valid structured analysis without network."""
    provider = MockLLMProvider()
    data, in_tok, out_tok, lat = await provider.generate_json(
        prompt="Website Domain: stripe.com\nPage Title: Stripe | Financial Infrastructure",
        system_prompt="Analyze this site.",
    )

    assert "brand" in data
    assert "competitor_analysis" in data
    assert "target_search_queries" in data
    assert "seo_improvements" in data
    assert "top_services" in data
    assert "target_audience" in data
    assert len(data["competitor_analysis"]["competitors"]) >= 3
    assert len(data["target_search_queries"]) == 5
    assert len(data["top_services"]) == 5
    assert in_tok > 0
    assert out_tok > 0


# ============================================================================
# 4. FASTAPI ENDPOINT INTEGRATION TESTS
# ============================================================================

def test_endpoint_root_info():
    """Verify GET / returns application details."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "app" in data
    assert "version" in data
    assert data["documentation"] == "/docs"


def test_endpoint_health_check():
    """Verify GET /health returns operational status."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "active_provider" in data


def test_endpoint_model_info():
    """Verify GET /model-info returns provider and official pricing metadata."""
    response = client.get("/model-info")
    assert response.status_code == 200
    data = response.json()
    assert "configured_provider" in data
    assert "pricing_per_million_tokens" in data
    assert "context_window" in data


def test_endpoint_link_analysis_ssrf_rejection():
    """Verify POST /link-analysis rejects private/internal URLs with HTTP 400."""
    response = client.post("/link-analysis", json={"url": "http://127.0.0.1:8000"})
    assert response.status_code == 400
    data = response.json()
    assert data["success"] is False
    assert data["error_code"] == "SSRF_SECURITY_VIOLATION"


def test_endpoint_link_analysis_invalid_url_format():
    """Verify POST /link-analysis rejects non-HTTP schemes with HTTP 422."""
    response = client.post("/link-analysis", json={"url": "ftp://bad-scheme.com"})
    assert response.status_code == 422
    data = response.json()
    assert data["success"] is False


@patch("app.utils.validators.resolve_and_validate_hostname", return_value=(True, ["93.184.216.34"], None))
@patch("app.services.website_extractor.WebsiteExtractor.fetch_html_securely", new_callable=AsyncMock)
def test_endpoint_link_analysis_success_mocked(mock_fetch, mock_dns):
    """
    Verify complete POST /link-analysis workflow with mocked HTML extraction and mocked DNS.
    Validates that the output matches the required Section 11 Pydantic schema structure.
    """
    mock_fetch.return_value = (
        SAMPLE_HTML,
        "https://acmecloud.example.com/",
        200,
        95.0,
    )

    response = client.post(
        "/link-analysis",
        json={"url": "https://acmecloud.example.com", "preferred_provider": "mock", "refresh_cache": True},
    )
    assert response.status_code == 200
    data = response.json()

    # Section 11 Response Schema Verification
    assert data["success"] is True
    assert data["website"] == "https://acmecloud.example.com/"

    # Brand
    assert "name" in data["brand"]
    assert "category" in data["brand"]
    assert "summary" in data["brand"]

    # Extraction
    assert data["extraction"]["pages_analyzed"] == 1
    assert data["extraction"]["content_length"] > 0
    assert "Acme Cloud" in data["extraction"]["page_title"]

    # Analysis Container - 5 Core Areas
    analysis = data["analysis"]

    # 1. Competitor Analysis
    assert "competitor_analysis" in analysis
    comps = analysis["competitor_analysis"]["competitors"]
    assert len(comps) >= 3
    for comp in comps:
        assert "competitor_name" in comp
        assert comp["verification_status"] in ("website-derived", "externally verified", "ai-generated suggestion")

    # 2. Top 5 Target Search Queries
    queries = analysis["target_search_queries"]
    assert len(queries) == 5
    for q in queries:
        assert q["relevant_platform"] in ("Google Search", "Facebook", "Instagram", "LinkedIn", "X (Twitter)")
        assert q["search_intent"] in ("Informational", "Commercial", "Transactional", "Navigational")
        assert "Unavailable" in q["search_volume_status"]

    # 3. SEO Improvements
    seo = analysis["seo_improvements"]
    assert "primary_keyword" in seo
    assert "recommended_title" in seo
    assert "meta_description" in seo
    assert len(seo["technical_issues"]) > 0

    # 4. Top Services
    services = analysis["top_services"]
    assert len(services) == 5
    for s in services:
        assert s["identification_type"] in ("EXPLICIT", "INFERRED")
        assert "source_evidence" in s
        assert "key_capabilities" in s
        assert len(s["key_capabilities"]) > 0
        assert "pricing_model" in s
        assert "delivery_model" in s
        assert "confidence_score" in s
        assert 0.0 <= s["confidence_score"] <= 1.0
        assert "page_location" in s

    # 5. Target Audience
    audience = analysis["target_audience"]
    assert len(audience["primary_audience"]) > 0
    assert len(audience["customer_needs"]) > 0

    # LLM Metadata & Pricing
    assert "llm_metadata" in data
    assert "estimated_cost_usd" in data["llm_metadata"]
    assert "latency_ms" in data["llm_metadata"]

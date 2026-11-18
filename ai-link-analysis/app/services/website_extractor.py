"""Secure website extraction service utilizing HTTPX, BeautifulSoup4, and rigorous SSRF guards."""

import json
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment
import httpx

from app.config import settings
from app.schemas import ExtractedWebsiteData
from app.utils.validators import (
    SSRFValidationError,
    validate_target_url,
)


class WebsiteExtractionError(Exception):
    """Raised when an external website cannot be fetched or parsed."""
    def __init__(self, message: str, status_code: int = 400, original_error: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.original_error = original_error


class WebsiteExtractor:
    """Production-grade secure website extraction engine."""

    def __init__(
        self,
        timeout: float = settings.REQUEST_TIMEOUT_SECONDS,
        max_redirects: int = settings.MAX_REDIRECTS,
        max_bytes: int = settings.MAX_RESPONSE_BYTES,
        user_agent: str = settings.USER_AGENT,
    ):
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.user_agent = user_agent

    async def fetch_html_securely(self, initial_url: str) -> Tuple[str, str, int, float]:
        """
        Fetch HTML content while validating SSRF boundaries at every redirect hop.
        Returns: (html_text, final_url, status_code, latency_ms)
        """
        current_url = initial_url
        start_time = time.perf_counter()

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }

        # Follow redirects manually to validate each hop against SSRF rules
        async with httpx.AsyncClient(
            verify=True,
            follow_redirects=False,
            timeout=httpx.Timeout(self.timeout, connect=7.0),
        ) as client:
            hops = 0
            while hops <= self.max_redirects:
                # 1. SSRF and DNS check on the target URL
                is_safe, error_msg, _ = validate_target_url(current_url)
                if not is_safe:
                    raise SSRFValidationError(
                        f"SSRF Protection: Blocked URL '{current_url}'. Reason: {error_msg}"
                    )

                try:
                    # Stream response to enforce max response size limit
                    response = await client.get(current_url, headers=headers)
                except httpx.ConnectTimeout:
                    raise WebsiteExtractionError(
                        f"Connection timed out while attempting to reach '{current_url}'",
                        status_code=504,
                    )
                except httpx.ReadTimeout:
                    raise WebsiteExtractionError(
                        f"Read timed out while downloading content from '{current_url}'",
                        status_code=504,
                    )
                except httpx.ConnectError as e:
                    raise WebsiteExtractionError(
                        f"Failed to connect to '{current_url}'. Host may be unreachable or offline.",
                        status_code=502,
                        original_error=str(e),
                    )
                except httpx.HTTPStatusError as e:
                    raise WebsiteExtractionError(
                        f"HTTP error {e.response.status_code} occurred while requesting '{current_url}'",
                        status_code=e.response.status_code,
                        original_error=str(e),
                    )
                except Exception as e:
                    raise WebsiteExtractionError(
                        f"Network or SSL failure accessing '{current_url}': {str(e)}",
                        status_code=502,
                        original_error=str(e),
                    )

                # Check if this is a redirect
                if response.is_redirect:
                    redirect_location = response.headers.get("location")
                    if not redirect_location:
                        raise WebsiteExtractionError(
                            "Redirect status received without Location header",
                            status_code=502,
                        )
                    # Resolve relative redirect
                    current_url = urljoin(current_url, redirect_location)
                    hops += 1
                    continue

                # Final response reached
                latency_ms = (time.perf_counter() - start_time) * 1000.0

                # Validate content size
                content_bytes = response.content
                if len(content_bytes) > self.max_bytes:
                    raise WebsiteExtractionError(
                        f"Website response exceeded maximum allowed size of {self.max_bytes / (1024*1024):.1f}MB",
                        status_code=413,
                    )

                # Validate content-type
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    # Non-HTML content check
                    if response.status_code >= 400:
                        raise WebsiteExtractionError(
                            f"Target website returned HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
                    # If not explicitly html, attempt decoding if text-like
                    if not content_type.startswith("text/"):
                        raise WebsiteExtractionError(
                            f"Unsupported content type '{content_type}'. Target must serve HTML.",
                            status_code=415,
                        )

                if response.status_code >= 400:
                    raise WebsiteExtractionError(
                        f"Target website returned error status code {response.status_code}",
                        status_code=response.status_code,
                    )

                html_text = response.text
                return html_text, str(response.url), response.status_code, latency_ms

            raise WebsiteExtractionError(
                f"Exceeded maximum redirect limit of {self.max_redirects} hops",
                status_code=508,
            )

    def parse_html_content(
        self,
        html_text: str,
        original_url: str,
        final_url: str,
        status_code: int,
        latency_ms: float,
    ) -> ExtractedWebsiteData:
        """
        Parse raw HTML using BeautifulSoup4 into strongly structured extraction metadata.
        """
        soup = BeautifulSoup(html_text, "html.parser")

        # 1. Page Title
        title: Optional[str] = None
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            title = title_tag.string.strip()
        elif soup.find("meta", property="og:title"):
            title = soup.find("meta", property="og:title").get("content", "").strip()

        # 2. Meta Description
        meta_description: Optional[str] = None
        meta_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        if meta_tag and meta_tag.get("content"):
            meta_description = meta_tag["content"].strip()
        elif soup.find("meta", property="og:description"):
            meta_description = soup.find("meta", property="og:description").get("content", "").strip()

        # 3. Canonical URL
        canonical_url: Optional[str] = None
        canonical_tag = soup.find("link", rel=re.compile(r"^canonical$", re.I))
        if canonical_tag and canonical_tag.get("href"):
            canonical_url = urljoin(final_url, canonical_tag["href"].strip())

        # 4. Open Graph Tags
        open_graph: Dict[str, str] = {}
        for og_tag in soup.find_all("meta", property=re.compile(r"^og:", re.I)):
            prop = og_tag.get("property", "").strip()
            content = og_tag.get("content", "").strip()
            if prop and content:
                open_graph[prop] = content

        # 5. Heading hierarchy
        h1_headings = [h.get_text(" ", strip=True) for h in soup.find_all("h1") if h.get_text(strip=True)]
        h2_headings = [h.get_text(" ", strip=True) for h in soup.find_all("h2") if h.get_text(strip=True)][:25]
        h3_headings = [h.get_text(" ", strip=True) for h in soup.find_all("h3") if h.get_text(strip=True)][:30]

        # 6. Links (Navigation, Internal, External)
        parsed_base = urlparse(final_url)
        base_domain = parsed_base.netloc.lower()

        nav_links: List[str] = []
        for nav in soup.find_all(["nav", "header"]):
            for a in nav.find_all("a", href=True):
                href = a["href"].strip()
                text = a.get_text(" ", strip=True)
                if text and href and not href.startswith("#"):
                    full_link = urljoin(final_url, href)
                    nav_links.append(f"{text} ({full_link})")

        internal_links_set: Set[str] = set()
        external_links_set: Set[str] = set()

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            full_url = urljoin(final_url, href)
            p = urlparse(full_url)
            if p.scheme in ("http", "https"):
                if p.netloc.lower() == base_domain or p.netloc.lower().endswith("." + base_domain):
                    internal_links_set.add(full_url)
                else:
                    external_links_set.add(full_url)

        # 7. Images and Alt Attributes (Technical SEO check)
        images = soup.find_all("img")
        images_count = len(images)
        images_missing_alt = sum(
            1 for img in images if not img.get("alt") or not img.get("alt").strip()
        )

        # 8. Extract specialized sections before stripping noise tags
        about_text = self._extract_section_text(soup, ["about", "story", "mission", "who-we-are", "company"])
        services_text = self._extract_section_text(
            soup, ["service", "solution", "feature", "offering", "product", "what-we-do"]
        )

        # 9. Extract contact info
        contact_info = self._extract_contact_info(soup, html_text)

        # 10. Clean and extract main website text
        main_text = self._extract_clean_main_text(soup)

        # 11. Extract explicit structured service candidates from DOM (JSON-LD, Nav, Feature cards)
        structured_service_candidates = self._extract_structured_services(soup, final_url)

        # 12. Run deterministic Technical SEO diagnostics
        technical_issues = self._detect_technical_issues(
            title=title,
            meta_description=meta_description,
            canonical_url=canonical_url,
            h1_headings=h1_headings,
            h2_headings=h2_headings,
            h3_headings=h3_headings,
            images_count=images_count,
            images_missing_alt=images_missing_alt,
        )

        return ExtractedWebsiteData(
            original_url=original_url,
            final_url=final_url,
            status_code=status_code,
            title=title,
            meta_description=meta_description,
            canonical_url=canonical_url,
            open_graph=open_graph,
            h1_headings=h1_headings,
            h2_headings=h2_headings,
            h3_headings=h3_headings,
            main_text=main_text,
            about_text=about_text,
            services_text=services_text,
            contact_info=contact_info,
            navigation_links=nav_links[:20],
            internal_links=list(internal_links_set)[:30],
            external_links=list(external_links_set)[:30],
            images_count=images_count,
            images_missing_alt=images_missing_alt,
            content_length=len(main_text),
            extraction_latency_ms=latency_ms,
            detected_technical_issues=technical_issues,
            structured_service_candidates=structured_service_candidates,
        )

    def _extract_structured_services(self, soup: BeautifulSoup, final_url: str) -> List[Dict[str, Any]]:
        """Extract explicit, high-confidence service candidates from JSON-LD, nav menus, and card grids."""
        candidates: List[Dict[str, Any]] = []
        seen_names = set()

        # 1. Inspect Schema.org JSON-LD scripts
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                raw_json = json.loads(script.string or "{}")
                items = raw_json if isinstance(raw_json, list) else [raw_json]
                for item in items:
                    t = item.get("@type", "")
                    if t in ("Service", "Product", "Offer", "SoftwareApplication"):
                        name = item.get("name", "").strip()
                        desc = item.get("description", "").strip()
                        if name and name.lower() not in seen_names:
                            seen_names.add(name.lower())
                            candidates.append({
                                "name": name,
                                "description": desc[:300] if desc else f"Officially declared {t} schema.",
                                "source": "Schema.org JSON-LD microdata",
                                "page_location": "Metadata Head (JSON-LD)",
                                "confidence": 0.99,
                            })
            except Exception:
                pass

        # 2. Inspect Navigation menus under Products/Services/Solutions
        for nav in soup.find_all(["nav", "header"]):
            for parent in nav.find_all(["li", "div"]):
                header_text = parent.find(["a", "span", "button"])
                if header_text:
                    h_text = header_text.get_text(" ", strip=True).lower()
                    if any(k in h_text for k in ["product", "service", "solution", "feature"]):
                        for sub_a in parent.find_all("a", href=True):
                            sub_text = sub_a.get_text(" ", strip=True)
                            if 2 < len(sub_text) < 50 and sub_text.lower() not in seen_names:
                                if sub_text.lower() not in ["all products", "overview", "view all", "pricing"]:
                                    seen_names.add(sub_text.lower())
                                    candidates.append({
                                        "name": sub_text,
                                        "description": f"Featured product/service link in navigation under '{h_text}'.",
                                        "source": f"Navigation Menu Link ({sub_a['href']})",
                                        "page_location": "Primary Header Navigation",
                                        "confidence": 0.95,
                                    })

        # 3. Inspect cards and grids with service/feature keywords
        card_selectors = [
            ("div", re.compile(r"(service|feature|solution|product|offering)[-_]?(card|item|block|box)", re.I)),
            ("section", re.compile(r"(service|feature|solution|product)", re.I)),
        ]
        for tag_name, pat in card_selectors:
            for el in soup.find_all(tag_name, class_=pat)[:12]:
                h = el.find(["h2", "h3", "h4", "h5", "strong"])
                p = el.find(["p", "span"])
                if h:
                    name = h.get_text(" ", strip=True)
                    desc = p.get_text(" ", strip=True) if p else ""
                    if 3 < len(name) < 60 and name.lower() not in seen_names:
                        seen_names.add(name.lower())
                        candidates.append({
                            "name": name,
                            "description": desc[:250] if desc else "Identified component from services showcase grid.",
                            "source": f"Grid Element: '{name} - {desc[:100]}'",
                            "page_location": "Homepage Solutions Grid",
                            "confidence": 0.92,
                        })

        return candidates[:10]

    def _extract_clean_main_text(self, soup: BeautifulSoup) -> str:
        """Strip script, styles, comments, SVG, and footer noise to obtain clean body text."""
        # Work on a clone or copy so original soup can be inspected
        soup_copy = BeautifulSoup(str(soup), "html.parser")

        # Remove irrelevant elements
        noise_tags = [
            "script", "style", "noscript", "svg", "iframe", "canvas",
            "footer", "nav", "aside", "form", "select", "option", "button"
        ]
        for tag in soup_copy.find_all(noise_tags):
            tag.decompose()

        # Remove HTML comments
        for comment in soup_copy.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        # Extract textual paragraphs and blocks
        raw_text = soup_copy.get_text(separator="\n", strip=True)

        # Deduplicate consecutive lines and normalize whitespace
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        unique_lines: List[str] = []
        seen_lines: Set[str] = set()

        for line in lines:
            # Skip very short generic lines or boilerplate
            if len(line) < 3 and not line.isalnum():
                continue
            normalized = line.lower()
            if normalized not in seen_lines:
                seen_lines.add(normalized)
                unique_lines.append(line)

        clean_text = "\n".join(unique_lines)
        return clean_text

    def _extract_section_text(self, soup: BeautifulSoup, keywords: List[str]) -> Optional[str]:
        """Extract text from divs or sections having matching id, class, or data-test attributes."""
        extracted_parts: List[str] = []
        for kw in keywords:
            pattern = re.compile(rf"(^|[-_]){kw}([-_]|$)", re.I)
            candidates = soup.find_all(
                lambda tag: tag.name in ["section", "div", "article"]
                and (
                    pattern.search(tag.get("id", "") or "")
                    or pattern.search(" ".join(tag.get("class", []) or []))
                )
            )
            for cand in candidates[:3]:
                txt = cand.get_text(" ", strip=True)
                if len(txt) > 40:
                    extracted_parts.append(txt[:800])

        if extracted_parts:
            return " ... ".join(extracted_parts[:4])
        return None

    def _extract_contact_info(self, soup: BeautifulSoup, raw_html: str) -> Dict[str, Any]:
        """Extract publicly available contact information (emails, phone numbers, addresses)."""
        contacts: Dict[str, Any] = {"emails": [], "phones": [], "socials": []}

        # 1. Emails
        email_pattern = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
        # Check mailto: links first
        for a in soup.find_all("a", href=re.compile(r"^mailto:", re.I)):
            email = a["href"].replace("mailto:", "").split("?")[0].strip()
            if email and email not in contacts["emails"]:
                contacts["emails"].append(email)

        # Search in text if few found
        if not contacts["emails"]:
            found = email_pattern.findall(raw_html[:30000])
            for email in set(found):
                # Filter out image filenames or common false positives
                if not email.endswith((".png", ".jpg", ".webp", ".svg")):
                    contacts["emails"].append(email)

        # 2. Phone numbers (international and US format)
        phone_pattern = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
        for a in soup.find_all("a", href=re.compile(r"^tel:", re.I)):
            phone = a["href"].replace("tel:", "").strip()
            if phone and phone not in contacts["phones"]:
                contacts["phones"].append(phone)

        # 3. Social media links
        social_domains = ["twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com", "github.com", "youtube.com"]
        for a in soup.find_all("a", href=True):
            href = a["href"]
            for s_domain in social_domains:
                if s_domain in href.lower() and href not in contacts["socials"]:
                    contacts["socials"].append(href)

        contacts["emails"] = contacts["emails"][:5]
        contacts["phones"] = contacts["phones"][:5]
        contacts["socials"] = contacts["socials"][:10]
        return contacts

    def _detect_technical_issues(
        self,
        title: Optional[str],
        meta_description: Optional[str],
        canonical_url: Optional[str],
        h1_headings: List[str],
        h2_headings: List[str],
        h3_headings: List[str],
        images_count: int,
        images_missing_alt: int,
    ) -> List[Dict[str, Any]]:
        """Perform verified, evidence-backed Technical SEO diagnostics without hallucination."""
        issues: List[Dict[str, Any]] = []

        # 1. Page Title Check
        if not title:
            issues.append({
                "issue_name": "Missing SEO Title Tag",
                "status": "FAIL",
                "details": "The document lacks a <title> tag. Search engines cannot index the page title properly.",
                "priority": "HIGH",
                "evidence": "No <title> or <meta property='og:title'> detected in HTML head.",
            })
        elif len(title) < 20:
            issues.append({
                "issue_name": "Short SEO Title",
                "status": "WARNING",
                "details": f"The page title is very short ({len(title)} chars). Optimal length is 50-60 characters.",
                "priority": "MEDIUM",
                "evidence": f"Existing title: '{title}' ({len(title)} characters).",
            })
        elif len(title) > 65:
            issues.append({
                "issue_name": "Long SEO Title Truncation Risk",
                "status": "WARNING",
                "details": f"The page title ({len(title)} chars) may be truncated on Google SERPs (max ~60 chars).",
                "priority": "LOW",
                "evidence": f"Existing title: '{title}' ({len(title)} characters).",
            })
        else:
            issues.append({
                "issue_name": "SEO Title Tag",
                "status": "PASS",
                "details": "Page title tag is present and within recommended character length.",
                "priority": "LOW",
                "evidence": f"Existing title: '{title}' ({len(title)} characters).",
            })

        # 2. Meta Description Check
        if not meta_description:
            issues.append({
                "issue_name": "Missing Meta Description",
                "status": "FAIL",
                "details": "No meta description tag was found. Search engines will generate snippets automatically.",
                "priority": "HIGH",
                "evidence": "No <meta name='description'> or <meta property='og:description'> in HTML head.",
            })
        elif len(meta_description) < 60:
            issues.append({
                "issue_name": "Under-optimized Meta Description Length",
                "status": "WARNING",
                "details": f"Meta description is only {len(meta_description)} chars. Recommended length is 140-160 characters.",
                "priority": "MEDIUM",
                "evidence": f"Existing description: '{meta_description}'.",
            })
        elif len(meta_description) > 170:
            issues.append({
                "issue_name": "Meta Description Truncation Risk",
                "status": "WARNING",
                "details": f"Meta description exceeds 160 characters ({len(meta_description)} chars) and will be truncated.",
                "priority": "LOW",
                "evidence": f"Existing description length: {len(meta_description)} characters.",
            })
        else:
            issues.append({
                "issue_name": "Meta Description",
                "status": "PASS",
                "details": "Meta description is properly configured.",
                "priority": "LOW",
                "evidence": f"Existing description length: {len(meta_description)} characters.",
            })

        # 3. Canonical URL
        if not canonical_url:
            issues.append({
                "issue_name": "Missing Canonical Tag",
                "status": "WARNING",
                "details": "No <link rel='canonical'> found. This can lead to duplicate content penalties.",
                "priority": "MEDIUM",
                "evidence": "HTML head does not define rel='canonical'.",
            })
        else:
            issues.append({
                "issue_name": "Canonical Tag Configuration",
                "status": "PASS",
                "details": "Canonical link tag is present and valid.",
                "priority": "LOW",
                "evidence": f"Canonical URL: '{canonical_url}'.",
            })

        # 4. Heading Hierarchy
        if len(h1_headings) == 0:
            issues.append({
                "issue_name": "Missing H1 Heading",
                "status": "FAIL",
                "details": "The page has no H1 tag. A main H1 is essential for topical hierarchy and search engine understanding.",
                "priority": "HIGH",
                "evidence": "Found 0 <h1> elements.",
            })
        elif len(h1_headings) > 1:
            issues.append({
                "issue_name": "Multiple H1 Headings Detected",
                "status": "WARNING",
                "details": f"Detected {len(h1_headings)} H1 tags. Best practice is a single dominant H1 per landing page.",
                "priority": "MEDIUM",
                "evidence": f"H1 list: {', '.join([h[:30] for h in h1_headings[:3]])}...",
            })
        else:
            issues.append({
                "issue_name": "H1 Heading Hierarchy",
                "status": "PASS",
                "details": "Single, well-defined H1 tag detected.",
                "priority": "LOW",
                "evidence": f"H1: '{h1_headings[0]}'.",
            })

        # Check skipped heading levels (H1 to H3 with no H2)
        if len(h1_headings) > 0 and len(h2_headings) == 0 and len(h3_headings) > 0:
            issues.append({
                "issue_name": "Skipped Heading Levels",
                "status": "WARNING",
                "details": "Page skips directly from H1 to H3 headings without any H2 headings.",
                "priority": "LOW",
                "evidence": f"Found {len(h1_headings)} H1s, 0 H2s, and {len(h3_headings)} H3s.",
            })

        # 5. Image Alt Text
        if images_count > 0:
            if images_missing_alt > 0:
                issues.append({
                    "issue_name": "Images Missing Alt Attributes",
                    "status": "WARNING",
                    "details": f"{images_missing_alt} out of {images_count} images are missing descriptive alt text, impairing accessibility and image SEO.",
                    "priority": "MEDIUM",
                    "evidence": f"{images_missing_alt}/{images_count} images lack alt attributes.",
                })
            else:
                issues.append({
                    "issue_name": "Image Alt Attributes",
                    "status": "PASS",
                    "details": "All images have alt attributes defined.",
                    "priority": "LOW",
                    "evidence": f"All {images_count} images contain alt attributes.",
                })

        # 6. Mobile & Performance (Explicitly not tested to avoid false claims)
        issues.append({
            "issue_name": "Mobile Responsiveness & Core Web Vitals",
            "status": "NOT_TESTED",
            "details": "Mobile viewport rendering, CLS, LCP, and FID require real-device rendering or Chrome Lighthouse execution.",
            "priority": "LOW",
            "evidence": "Synthetic DOM extraction only. No browser lab attached.",
        })

        return issues

    async def extract(self, url: str) -> ExtractedWebsiteData:
        """Execute complete secure fetch and extraction pipeline."""
        html_text, final_url, status_code, latency_ms = await self.fetch_html_securely(url)
        return self.parse_html_content(
            html_text=html_text,
            original_url=url,
            final_url=final_url,
            status_code=status_code,
            latency_ms=latency_ms,
        )

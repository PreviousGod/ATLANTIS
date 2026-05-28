"""Web Search — Internet čulo za Nucleus (DuckDuckGo, 0 tokena)."""
import html
import json
import logging
import re
import urllib.parse
import urllib.request

from .config import WEB_SEARCH_TIMEOUT, WEB_SEARCH_MAX_RESULTS, WEB_SEARCH_MAX_BYTES_PER_SOURCE

log = logging.getLogger("nucleus")

# Denylist of domains that should never be fetched
_DENYLIST_HOSTS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "youtube.com", "youtu.be", "reddit.com", "pinterest.com", "tumblr.com",
}


def _host_allowed(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host not in _DENYLIST_HOSTS


def fetch_page(url: str, max_bytes: int = None, timeout: float = None) -> str:
    """Fetch a single web page and return cleaned text.

    Limits:
      - max_bytes: max characters to return (default from config)
      - timeout: seconds (default from config)
      - denylist: social/media sites blocked
    """
    max_bytes = max_bytes or WEB_SEARCH_MAX_BYTES_PER_SOURCE
    timeout = timeout or WEB_SEARCH_TIMEOUT
    if not _host_allowed(url):
        return ""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 NucleusSearch/1.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "identity",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Read slightly more than max_bytes to avoid truncation mid-tag
            data = resp.read(max_bytes + 4096)
        text = data.decode("utf-8", "replace")
    except Exception as exc:
        log.info("Page fetch failed for %s: %s", url, exc)
        return ""

    # Strip scripts, styles, nav, footer, aside
    for tag in ("script", "style", "nav", "footer", "aside", "header", "noscript"):
        text = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ", text, flags=re.S | re.I)
    # Strip all remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_bytes]


def _ddgs_search(query, limit):
    """Try ddgs/duckduckgo_search package first."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=limit):
                results.append({
                    "url": r.get("href", ""),
                    "title": r.get("title", ""),
                    "snippet": r.get("body", r.get("title", "")),
                })
        return results if results else None
    except Exception:
        return None


def _html_fallback(query, limit, timeout):
    """Fallback: parse DuckDuckGo HTML results."""
    params = urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(
        f"https://html.duckduckgo.com/html/?{params}",
        headers={"User-Agent": "Mozilla/5.0 NucleusSearch/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception:
        return []
    results = []
    pattern = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
    snippet_pattern = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.I | re.S)
    snippets = [re.sub(r"<[^>]+>", " ", html.unescape(m.group(1))).strip()
                for m in snippet_pattern.finditer(body)]
    for i, m in enumerate(pattern.finditer(body)):
        url = html.unescape(m.group(1))
        title = re.sub(r"<[^>]+>", " ", html.unescape(m.group(2))).strip()
        if url.startswith("http"):
            results.append({
                "url": url,
                "title": title,
                "snippet": snippets[i] if i < len(snippets) else title,
            })
        if len(results) >= limit:
            break
    return results


def search(query, limit=None, timeout=None):
    """Search DuckDuckGo. Returns list of {url, title, snippet}."""
    limit = limit or WEB_SEARCH_MAX_RESULTS
    timeout = timeout or WEB_SEARCH_TIMEOUT
    results = _ddgs_search(query, limit)
    if results:
        return results
    return _html_fallback(query, limit, timeout)


def search_for_solution(problem_label, profile=None, max_sources=None, max_bytes_per_source=None):
    """Convert a problem label to a search query, find solutions, and optionally crawl pages.

    Returns dict with:
      - query: search string used
      - results: list of {url, title, snippet, page_text?}
      - suggested_action: one of review_sources | extract_recipe | no_sources
      - source_confidence: float 0.0-1.0
      - needs_crawl: bool
    """
    from .domain_profiles import get_domain_profile
    profile = profile or get_domain_profile(problem_label)
    max_sources = max_sources or WEB_SEARCH_MAX_RESULTS
    max_bytes_per_source = max_bytes_per_source or WEB_SEARCH_MAX_BYTES_PER_SOURCE

    query = problem_label.replace("_", " ") + " linux solution"
    results = search(query, limit=max_sources)
    if not results:
        return {
            "query": query,
            "results": [],
            "suggested_action": "no_sources",
            "source_confidence": 0.0,
            "needs_crawl": False,
        }

    needs_crawl = False
    crawled_count = 0
    for result in results:
        url = result.get("url", "")
        if profile.allows_url(url) and _host_allowed(url):
            page_text = fetch_page(url, max_bytes=max_bytes_per_source)
            if page_text:
                result["page_text"] = page_text
                needs_crawl = True
                crawled_count += 1

    source_confidence = min(
        0.95,
        0.25 + len(results) * 0.08 + crawled_count * 0.12,
    )

    if crawled_count >= 2:
        suggested_action = "extract_recipe"
    elif results:
        suggested_action = "review_sources"
    else:
        suggested_action = "no_sources"

    return {
        "query": query,
        "results": results,
        "suggested_action": suggested_action,
        "source_confidence": round(source_confidence, 2),
        "needs_crawl": needs_crawl,
    }

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser


@dataclass(frozen=True)
class WebEntry:
    title: str
    link: str
    summary: str
    published_at: datetime | None
    raw: dict[str, Any]


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _compile_regex(pattern: str | None) -> re.Pattern | None:
    if not pattern:
        return None
    pattern = pattern.strip()
    if not pattern:
        return None
    return re.compile(pattern, flags=re.IGNORECASE)


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _child_text(el: ET.Element, name: str) -> str | None:
    for child in list(el):
        if _strip_ns(child.tag) != name:
            continue
        if child.text is None:
            return None
        text = child.text.strip()
        return text or None
    return None


def _http_get(url: str, user_agent: str, accept: str, timeout_seconds: int = 30) -> requests.Response:
    return requests.get(
        url,
        headers={"User-Agent": user_agent, "Accept": accept},
        timeout=timeout_seconds,
        allow_redirects=True,
    )


def _extract_page_meta(html: str) -> tuple[str, str, datetime | None]:
    soup = BeautifulSoup(html, "html.parser")

    og_title = soup.find("meta", attrs={"property": "og:title"})
    og_desc = soup.find("meta", attrs={"property": "og:description"})
    meta_desc = soup.find("meta", attrs={"name": "description"})
    meta_pub = soup.find("meta", attrs={"property": "article:published_time"})
    meta_pub2 = soup.find("meta", attrs={"name": "pubdate"})

    title = ""
    for cand in [
        (og_title.get("content") if og_title else None),
        (soup.title.get_text(strip=True) if soup.title else None),
        (soup.find("h1").get_text(strip=True) if soup.find("h1") else None),
    ]:
        if cand:
            title = str(cand).strip()
            if title:
                break

    desc = ""
    for cand in [
        (og_desc.get("content") if og_desc else None),
        (meta_desc.get("content") if meta_desc else None),
    ]:
        if cand:
            desc = str(cand).strip()
            if desc:
                break

    if not desc:
        first_p = soup.find("p")
        if first_p:
            desc = first_p.get_text(" ", strip=True)

    desc = (desc or "").strip()
    if len(desc) > 600:
        desc = desc[:597].rstrip() + "..."

    published_at = _parse_datetime(
        str((meta_pub.get("content") if meta_pub else None) or (meta_pub2.get("content") if meta_pub2 else None) or "")
    )

    return title, desc, published_at


def fetch_web_page(url: str, user_agent: str, published_at: datetime | None = None) -> WebEntry | None:
    resp = _http_get(
        url=url,
        user_agent=user_agent,
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        timeout_seconds=30,
    )
    if resp.status_code != 200:
        return None

    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "html" not in content_type and "xml" not in content_type:
        return None

    html = resp.text or ""
    title, summary, page_published_at = _extract_page_meta(html)
    final_published = page_published_at or published_at

    # Store a bounded amount of HTML to avoid huge objects.
    raw_html = html
    if len(raw_html) > 250_000:
        raw_html = raw_html[:250_000] + "\n<!-- truncated -->\n"

    entry = WebEntry(
        title=title or url,
        link=resp.url or url,
        summary=summary or "",
        published_at=final_published,
        raw={
            "kind": "web_page",
            "requested_url": url,
            "final_url": resp.url,
            "status_code": resp.status_code,
            "headers": {k: v for k, v in resp.headers.items()},
            "content_type": resp.headers.get("Content-Type"),
            "title": title,
            "summary": summary,
            "published_at": final_published.isoformat() if final_published else None,
            "html": raw_html,
        },
    )
    return entry


def _normalize_links(base_url: str, hrefs: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    base_parsed = urlparse(base_url)
    base_netloc = base_parsed.netloc.lower()

    for href in hrefs:
        href = (href or "").strip()
        if not href or href.startswith("#"):
            continue
        if href.startswith("mailto:") or href.startswith("javascript:"):
            continue

        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if not parsed.netloc:
            continue

        # Prefer same-host links to reduce noise.
        if parsed.netloc.lower() != base_netloc:
            continue

        normalized = parsed._replace(fragment="").geturl()
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)

    return out


def _set_query_param(url: str, name: str, value: str) -> str:
    parts = urlsplit(url)
    pairs = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True) if k != name]
    pairs.append((name, value))
    query = urlencode(pairs, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _parse_reliefweb_river_entries(
    soup: BeautifulSoup,
    *,
    base_url: str,
    include_re: re.Pattern | None,
    exclude_re: re.Pattern | None,
) -> list[WebEntry]:
    entries: list[WebEntry] = []
    for article in soup.find_all("article", class_=lambda c: c and "rw-river-article" in str(c)):
        title_el = article.find("h3", class_="rw-river-article__title")
        a = title_el.find("a", href=True) if title_el else None
        if not a:
            continue

        href = str(a.get("href") or "").strip()
        if not href:
            continue
        link = urljoin(base_url, href)
        if include_re and not include_re.search(link):
            continue
        if exclude_re and exclude_re.search(link):
            continue

        title = a.get_text(" ", strip=True) or link

        summary = ""
        content = article.find("div", class_="rw-river-article__content")
        if content:
            p = content.find("p")
            if p:
                summary = p.get_text(" ", strip=True)

        meta: dict[str, Any] = {}
        sources: list[str] = []
        orgs: list[str] = []
        posted_dt: datetime | None = None
        published_dt: datetime | None = None

        dl = article.find("dl", class_=lambda c: c and "rw-entity-meta--core" in str(c))
        if dl:
            for dt_el in dl.find_all("dt"):
                label = dt_el.get_text(" ", strip=True)
                if not label:
                    continue
                dd_el = dt_el.find_next_sibling("dd")
                if not dd_el:
                    continue

                time_el = dd_el.find("time")
                if time_el and time_el.get("datetime"):
                    dt_val = _parse_datetime(str(time_el.get("datetime")))
                    if label.lower() == "posted":
                        posted_dt = dt_val or posted_dt
                    if label.lower() in {"originally published", "published"}:
                        published_dt = dt_val or published_dt

                if label.lower() in {"source", "sources"}:
                    for a_el in dd_el.find_all("a"):
                        t = a_el.get_text(" ", strip=True)
                        if t:
                            sources.append(t)
                if label.lower() == "organization":
                    for a_el in dd_el.find_all("a"):
                        t = a_el.get_text(" ", strip=True)
                        if t:
                            orgs.append(t)

                meta[label] = dd_el.get_text(" ", strip=True)

        published_at = posted_dt or published_dt

        prefix_lines: list[str] = []
        if orgs:
            prefix_lines.append(f"Organization: {', '.join(orgs)}")
        if sources:
            prefix_lines.append(f"Source: {', '.join(sources)}")

        combined_summary = summary or ""
        if prefix_lines:
            combined_summary = "\n".join(prefix_lines + ([combined_summary] if combined_summary else []))

        entries.append(
            WebEntry(
                title=title,
                link=link,
                summary=combined_summary,
                published_at=published_at,
                raw={
                    "kind": "reliefweb_river_item",
                    "title": title,
                    "link": link,
                    "summary": combined_summary,
                    "published_at": published_at.isoformat() if published_at else None,
                    "meta": meta,
                },
            )
        )
    return entries


def fetch_html_list(
    listing_url: str,
    user_agent: str,
    max_items: int = 20,
    include_regex: str | None = None,
    exclude_regex: str | None = None,
    max_pages: int = 1,
    page_param: str = "page",
    start_page: int = 0,
) -> list[WebEntry]:
    include_re = _compile_regex(include_regex)
    exclude_re = _compile_regex(exclude_regex)

    seen_links: set[str] = set()
    entries: list[WebEntry] = []

    pages = int(max_pages or 1)
    if pages < 1:
        pages = 1

    for i in range(pages):
        page_value = int(start_page or 0) + i
        needs_page_param = pages > 1 or int(start_page or 0) != 0
        page_url = listing_url if not needs_page_param else _set_query_param(listing_url, page_param, str(page_value))

        resp = _http_get(
            url=page_url,
            user_agent=user_agent,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            timeout_seconds=30,
        )
        if resp.status_code != 200:
            continue

        soup = BeautifulSoup(resp.text or "", "html.parser")

        # ReliefWeb's list pages expose useful summary + metadata inline, avoid fetching each detail page.
        river_entries = _parse_reliefweb_river_entries(
            soup,
            base_url=str(resp.url or page_url),
            include_re=include_re,
            exclude_re=exclude_re,
        )
        if river_entries:
            for e in river_entries:
                if e.link in seen_links:
                    continue
                seen_links.add(e.link)
                entries.append(e)
                if len(entries) >= max_items:
                    return entries
            continue

        hrefs = [str(a.get("href")) for a in soup.find_all("a", href=True)]
        links = _normalize_links(resp.url or page_url, hrefs)

        filtered: list[str] = []
        for link in links:
            if include_re and not include_re.search(link):
                continue
            if exclude_re and exclude_re.search(link):
                continue
            if link in seen_links:
                continue
            seen_links.add(link)
            filtered.append(link)

        for link in filtered:
            if len(entries) >= max_items:
                return entries
            entry = fetch_web_page(url=link, user_agent=user_agent)
            if not entry:
                continue
            entries.append(
                WebEntry(
                    title=entry.title,
                    link=entry.link,
                    summary=entry.summary,
                    published_at=entry.published_at,
                    raw={
                        "kind": "html_list_item",
                        "listing_url": page_url,
                        "item": entry.raw,
                    },
                )
            )

    return entries


def fetch_sitemap(
    sitemap_url: str,
    user_agent: str,
    max_items: int = 20,
    include_regex: str | None = None,
    exclude_regex: str | None = None,
) -> list[WebEntry]:
    include_re = _compile_regex(include_regex)
    exclude_re = _compile_regex(exclude_regex)

    resp = _http_get(
        url=sitemap_url,
        user_agent=user_agent,
        accept="application/xml,text/xml,application/xhtml+xml,text/html;q=0.9,*/*;q=0.8",
        timeout_seconds=30,
    )
    if resp.status_code != 200:
        return []

    try:
        root = ET.fromstring(resp.text or "")
    except Exception:
        return []

    tag = _strip_ns(root.tag)
    candidates: list[tuple[str, datetime | None]] = []

    def add_url(loc: str | None, lastmod: str | None) -> None:
        if not loc:
            return
        loc = loc.strip()
        if not loc:
            return
        if include_re and not include_re.search(loc):
            return
        if exclude_re and exclude_re.search(loc):
            return
        candidates.append((loc, _parse_datetime(lastmod)))

    if tag == "urlset":
        for url_el in list(root):
            if _strip_ns(url_el.tag) != "url":
                continue
            add_url(_child_text(url_el, "loc"), _child_text(url_el, "lastmod"))
    elif tag == "sitemapindex":
        # Best-effort: read child sitemaps until we have enough URLs.
        for sm_el in list(root):
            if _strip_ns(sm_el.tag) != "sitemap":
                continue
            loc = _child_text(sm_el, "loc")
            if not loc:
                continue
            sub_entries = fetch_sitemap(
                sitemap_url=loc,
                user_agent=user_agent,
                max_items=max_items,
                include_regex=include_regex,
                exclude_regex=exclude_regex,
            )
            return sub_entries
    else:
        return []

    candidates.sort(key=lambda t: t[1] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)

    entries: list[WebEntry] = []
    for loc, lastmod in candidates[:max_items]:
        entry = fetch_web_page(url=loc, user_agent=user_agent, published_at=lastmod)
        if not entry:
            continue
        entries.append(
            WebEntry(
                title=entry.title,
                link=entry.link,
                summary=entry.summary,
                published_at=entry.published_at,
                raw={
                    "kind": "sitemap_item",
                    "sitemap_url": sitemap_url,
                    "lastmod": lastmod.isoformat() if lastmod else None,
                    "item": entry.raw,
                },
            )
        )
    return entries

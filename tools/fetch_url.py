"""Robust URL fetcher for the /ingest flows of the ecosystem.

Standalone CLI: given a URL (article, Instagram post/reel, Google Maps place)
it fetches the page and emits structured data for the ingesting agent.

    python tools/fetch_url.py <url> [--json] [--media-dir DIR]

Pipeline:
    1. requests GET (realistic user-agent, timeout, redirects followed)
    2. trafilatura.extract with metadata (title, author, date, text)
    3. fallback BeautifulSoup: <title> + og:* meta when trafilatura is empty

Special cases:
    - Instagram (/p/, /reel/): best-effort yt-dlp media download into
      --media-dir (if yt-dlp is available) + og:* caption extraction from the
      public page. Never uses login/credentials. On total failure it still
      returns url+kind so the LLM can ingest manually.
    - Google Maps (maps.google.*, goo.gl, maps.app.goo.gl): follows redirects,
      extracts place name/coordinates from the final URL and og:title.

stdout is reserved for the result (JSON with --json, human-readable
otherwise); all logging goes to stderr. Exit code is 0 even on partial
extraction — problems are reported in the "warnings" field.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from importlib import util as importlib_util
from pathlib import Path
from shutil import which
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests

logger = logging.getLogger("fetch_url")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 25  # seconds
YTDLP_TIMEOUT = 180  # seconds

_COORDS_BANG_RE = re.compile(r"!3d(-?\d+\.?\d*)!4d(-?\d+\.?\d*)")
_COORDS_AT_RE = re.compile(r"@(-?\d+\.?\d*),(-?\d+\.?\d*)")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def classify(url: str) -> str:
    """Classify a URL as instagram | maps | article."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    if "instagram.com" in host and re.search(r"/(p|reel|reels|tv)/", path):
        return "instagram"
    if "google." in host and "/maps/placelists/" in path:
        return "maps-list"
    if (
        host.startswith("maps.google.")
        or host == "maps.app.goo.gl"
        or host == "goo.gl"
        or (("google." in host) and path.startswith("/maps"))
    ):
        return "maps"
    return "article"


def fetch_page(url: str, warnings: list[str]) -> requests.Response | None:
    """GET the URL with a realistic UA; return None (and a warning) on failure."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code >= 400:
            warnings.append(f"HTTP {resp.status_code} fetching {url}")
            logger.warning("HTTP %s fetching %s", resp.status_code, url)
            return resp  # body may still contain og: meta (e.g. soft 404s)
        return resp
    except requests.RequestException as exc:
        warnings.append(f"fetch failed: {exc.__class__.__name__}: {exc}")
        logger.warning("fetch failed for %s: %s", url, exc)
        return None


def extract_with_trafilatura(html: str, url: str, warnings: list[str]) -> dict[str, Any]:
    """Main-content extraction with metadata. Empty dict when nothing extracted."""
    try:
        import trafilatura
    except ImportError:
        warnings.append("trafilatura not installed; using BeautifulSoup fallback only")
        return {}
    try:
        raw = trafilatura.extract(html, url=url, output_format="json", with_metadata=True)
    except Exception as exc:  # noqa: BLE001 — third-party extractor, any failure is non-fatal
        warnings.append(f"trafilatura failed: {exc.__class__.__name__}: {exc}")
        logger.warning("trafilatura failed: %s", exc)
        return {}
    if not raw:
        return {}
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        warnings.append(f"trafilatura returned invalid JSON: {exc}")
        return {}
    return data


def extract_og_meta(html: str, warnings: list[str]) -> dict[str, str]:
    """Fallback extraction: <title> + og:*/description meta tags."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        warnings.append("beautifulsoup4 not installed; og: fallback unavailable")
        return {}
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:  # noqa: BLE001 — malformed HTML must not crash the tool
        warnings.append(f"HTML parse failed: {exc.__class__.__name__}: {exc}")
        return {}

    meta: dict[str, str] = {}
    if soup.title and soup.title.string:
        meta["title"] = soup.title.string.strip()
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name") or ""
        content = (tag.get("content") or "").strip()
        if not content:
            continue
        if key in ("og:title", "og:description", "og:image", "og:url", "og:type",
                   "description", "author", "article:author", "article:published_time",
                   "twitter:title", "twitter:description"):
            meta.setdefault(key, content)
    return meta


# --------------------------------------------------------------------------- #
# per-kind handlers
# --------------------------------------------------------------------------- #
def handle_article(url: str, result: dict[str, Any]) -> None:
    """Generic page: trafilatura first, og: meta fallback."""
    warnings: list[str] = result["warnings"]
    resp = fetch_page(url, warnings)
    if resp is None:
        return
    result["url"] = resp.url  # final URL after redirects
    html = resp.text

    data = extract_with_trafilatura(html, resp.url, warnings)
    if data.get("text"):
        result["title"] = data.get("title") or ""
        result["text"] = data.get("text") or ""
        result["author"] = data.get("author") or ""
        result["date"] = data.get("date") or ""
        if data.get("description"):
            result["extra"]["description"] = data["description"]
        return

    warnings.append("trafilatura extracted no text; using og:/title fallback")
    meta = extract_og_meta(html, warnings)
    result["title"] = meta.get("og:title") or meta.get("twitter:title") or meta.get("title", "")
    result["text"] = meta.get("og:description") or meta.get("twitter:description") or meta.get("description", "")
    result["author"] = meta.get("article:author") or meta.get("author", "")
    result["date"] = meta.get("article:published_time", "")
    if meta.get("og:image"):
        result["extra"]["og_image"] = meta["og:image"]
    if not result["title"] and not result["text"]:
        warnings.append("no content extracted; only the URL is available")


def _ytdlp_command() -> list[str] | None:
    """Locate yt-dlp: prefer the importable module, then the binary on PATH."""
    if importlib_util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    binary = which("yt-dlp")
    if binary:
        return [binary]
    return None


def download_media(url: str, media_dir: Path, warnings: list[str]) -> list[str]:
    """Best-effort media download via yt-dlp. Returns list of downloaded paths."""
    base = _ytdlp_command()
    if base is None:
        warnings.append("yt-dlp not available; media not downloaded")
        return []
    try:
        media_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warnings.append(f"cannot create media dir {media_dir}: {exc}")
        return []
    cmd = base + [
        "--no-playlist",
        "--no-progress",
        "--no-simulate",
        "--print", "after_move:filepath",
        "-o", str(media_dir / "%(id)s.%(ext)s"),
        url,
    ]
    logger.info("running yt-dlp for %s", url)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=YTDLP_TIMEOUT, check=False
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        warnings.append(f"yt-dlp execution failed: {exc.__class__.__name__}: {exc}")
        return []
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        warnings.append(f"yt-dlp failed (rc={proc.returncode}): {tail[-1] if tail else 'no stderr'}")
        return []
    paths = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return [p for p in paths if Path(p).exists()]


def handle_instagram(url: str, media_dir: Path | None, result: dict[str, Any]) -> None:
    """Instagram post/reel: yt-dlp media + public og: meta. Never logs in."""
    warnings: list[str] = result["warnings"]

    if media_dir is not None:
        result["media"] = download_media(url, media_dir, warnings)
    else:
        warnings.append("no --media-dir given; skipping media download")

    resp = fetch_page(url, warnings)
    if resp is not None:
        meta = extract_og_meta(resp.text, warnings)
        result["title"] = meta.get("og:title") or meta.get("title", "")
        result["text"] = meta.get("og:description") or meta.get("description", "")
        if meta.get("og:image"):
            result["extra"]["og_image"] = meta["og:image"]
        # og:title is usually '<author> on Instagram: "<caption>"'
        author_match = re.match(r"^(.{1,80}?) on Instagram", result["title"])
        if author_match:
            result["author"] = author_match.group(1).strip()
        if "login" in resp.url.lower() or "accounts/login" in resp.url:
            warnings.append("Instagram redirected to login wall; metadata may be missing")
        elif result["title"].strip().lower() == "instagram" and not result["text"]:
            result["title"] = ""
            warnings.append(
                "Instagram served the generic shell page (soft login wall); no caption available"
            )

    if not result["title"] and not result["text"] and not result["media"]:
        warnings.append(
            "Instagram extraction failed entirely (login wall); "
            "only url+kind returned — ingest manually from user input"
        )


def _maps_coords(final_url: str) -> tuple[float, float] | None:
    """Pull lat/lng from a Maps URL: !3d..!4d.. (place pin) wins over @lat,lng (viewport)."""
    match = _COORDS_BANG_RE.search(final_url) or _COORDS_AT_RE.search(final_url)
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except ValueError:
        return None


def _maps_place_from_path(final_url: str) -> str:
    """Extract the place name from /maps/place/<name>/..."""
    match = re.search(r"/maps/place/([^/@]+)", urlparse(final_url).path)
    if not match:
        return ""
    return unquote(match.group(1)).replace("+", " ").strip()


# Open Location Code alphabet (no vowels / easily-confused chars). A short code
# is <prefix>+<suffix>; after URL query-decoding Google renders the '+' as a
# space, so we accept either separator.
_PLUS_CODE_RE = re.compile(
    r"\b([23456789CFGHJMPQRVWX]{4,8})[+\s]([23456789CFGHJMPQRVWX]{2,3})\b", re.I
)


def _geocode_query(query: str, warnings: list[str]) -> tuple[float, float] | None:
    """Best-effort (lat, lng) for a free-text place via OSM Nominatim.

    Shared Maps links often redirect to `?q=<name>&ftid=...` with NO coordinate
    marker in the URL; geocoding the name recovers them. Returns None (and a
    warning) on any failure — never raises."""
    q = (query or "").strip()
    if not q:
        return None
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
        warnings.append(f"geocode found nothing for {q!r}")
    except (requests.RequestException, ValueError, KeyError) as exc:
        warnings.append(f"geocode failed: {exc.__class__.__name__}: {exc}")
    return None


def _reference_candidates(locality: str) -> list[str]:
    """Progressively broader place strings to geocode as a Plus-Code reference.

    A short code's locality is often a name Nominatim can't find
    (`Cascate santuario tre fossi, 06181 Assisi PG`); broadening to its trailing
    segments and the bare town (`Assisi`) eventually hits."""
    loc = (locality or "").strip(" ,")
    out: list[str] = []
    seen: set[str] = set()

    def add(c: str) -> None:
        c = c.strip(" ,")
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)

    add(loc)
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    for i in range(1, len(parts)):
        add(", ".join(parts[i:]))
    if parts:  # bare town: drop postal code (4-5 digits) and province (2 caps)
        town = re.sub(r"\b\d{4,5}\b", "", parts[-1])
        town = re.sub(r"\b[A-Z]{2}\b", "", town)
        add(town)
    return out


def _decode_plus_code(q: str, warnings: list[str]) -> tuple[float, float] | None:
    """If `q` contains an Open Location Code (Plus Code), resolve it to (lat, lng).

    Full codes decode directly; short codes (e.g. `4M6R+G7`) are recovered against
    a reference geocoded from the remaining locality text (broadened until it
    hits). Needs the optional `openlocationcode` package — returns None (+ warning)
    when unavailable."""
    m = _PLUS_CODE_RE.search(q or "")
    if not m:
        return None
    code = f"{m.group(1)}+{m.group(2)}".upper()
    try:
        from openlocationcode import openlocationcode as olc
    except ImportError:
        warnings.append("plus code present but openlocationcode not installed")
        return None
    try:
        if not olc.isFull(code):
            locality = _PLUS_CODE_RE.sub("", q).strip(" ,")
            ref = None
            for cand in _reference_candidates(locality):
                ref = _geocode_query(cand, [])  # quiet: misses are expected
                if ref:
                    break
            if not ref:
                warnings.append("short plus code but locality could not be geocoded")
                return None
            code = olc.recoverNearest(code, ref[0], ref[1])
        area = olc.decode(code)
        return area.latitudeCenter, area.longitudeCenter
    except Exception as exc:  # noqa: BLE001 — best effort, never fatal
        warnings.append(f"plus code decode failed: {exc.__class__.__name__}: {exc}")
        return None


_LIST_NAME_COORDS_RE = re.compile(
    r'"([^"\\]{2,90})"[^"]{0,400}?!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)'
)


def _extract_list_places(html: str) -> list[dict[str, Any]]:
    """Best-effort: pull (name, lat, lng) from a shared Maps list page.

    Anchors on the stable '!3d<lat>!4d<lng>' coordinate marker preceded by a
    quoted place name within a short window. Returns [] (never raises) when the
    page shape doesn't match — caller must fall back to manual paste."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for m in _LIST_NAME_COORDS_RE.finditer(html):
        name = m.group(1).strip()
        key = name.lower()
        if not name or key in seen:
            continue
        try:
            lat, lng = float(m.group(2)), float(m.group(3))
        except ValueError:
            continue
        seen.add(key)
        out.append({"place": name, "lat": lat, "lng": lng})
    return out


def _maybe_playwright_html(url: str, warnings: list[str]) -> str | None:
    """Optional: render the list with Playwright (scroll to load all cards).
    Returns rendered HTML, or None when Playwright isn't installed/usable."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, timeout=30000, wait_until="networkidle")
                for _ in range(8):  # scroll the list panel to load lazy items
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(600)
                html = page.content()
            finally:
                browser.close()  # close even if goto/scroll raised (no leaked Chromium)
            return html
    except Exception as exc:  # noqa: BLE001 — best effort, never fatal
        warnings.append(f"playwright render failed: {exc.__class__.__name__}: {exc}")
        return None


def handle_maps_list(url: str, result: dict[str, Any]) -> None:
    """Shared Google Maps list → extra.places (best effort)."""
    warnings: list[str] = result["warnings"]
    resp = fetch_page(url, warnings)
    if resp is None:
        return
    final_url = resp.url
    if "consent.google" in urlparse(final_url).netloc:
        cont = parse_qs(urlparse(final_url).query).get("continue", [""])[0]
        if cont:
            final_url = cont
            warnings.append("Google consent wall hit; refetching the real list URL")
            resp2 = fetch_page(final_url, warnings)
            if resp2 is not None:
                resp = resp2  # extract from the real list page, not the consent page
    result["url"] = final_url

    meta = extract_og_meta(resp.text, warnings)
    result["title"] = meta.get("og:title", "") or "Google Maps list"

    places = _extract_list_places(resp.text)
    rendered = _maybe_playwright_html(final_url, warnings)
    if rendered:
        merged = {p["place"].lower(): p for p in places}
        for p in _extract_list_places(rendered):
            merged.setdefault(p["place"].lower(), p)
        places = list(merged.values())

    result["extra"]["places"] = places
    result["extra"]["count"] = len(places)
    if not places:
        warnings.append(
            "no places extracted from the list (JS-rendered or DOM changed); "
            "ask the user to paste individual place links"
        )


def handle_maps(url: str, result: dict[str, Any]) -> None:
    """Google Maps place: follow redirects, parse final URL + og:title."""
    warnings: list[str] = result["warnings"]
    resp = fetch_page(url, warnings)
    if resp is None:
        return

    final_url = resp.url
    # EU consent interstitial: the real Maps URL is in the `continue` param.
    if "consent.google" in urlparse(final_url).netloc:
        cont = parse_qs(urlparse(final_url).query).get("continue", [""])[0]
        if cont:
            final_url = cont
            warnings.append("Google consent wall hit; place parsed from redirect URL only")
    result["url"] = final_url

    place = _maps_place_from_path(final_url)
    coords = _maps_coords(final_url)
    # Shared links carry the query in ?q= / ?query= (place name and/or plus code).
    qs = parse_qs(urlparse(final_url).query)
    q_value = (qs.get("q") or qs.get("query") or [""])[0].strip()
    if not place:
        # Drop a leading Plus Code from the display name (kept for coords below).
        place = _PLUS_CODE_RE.sub("", q_value.replace("+", " ")).strip(" ,")

    meta = extract_og_meta(resp.text, warnings)
    og_title = meta.get("og:title", "")
    if og_title and og_title.lower() != "google maps":
        result["title"] = og_title
    else:
        result["title"] = place
    if meta.get("og:description"):
        result["text"] = meta["og:description"]
    if meta.get("og:image"):
        result["extra"]["og_image"] = meta["og:image"]

    result["extra"]["place"] = place or result["title"]
    # No coordinate marker in the URL (typical of shared `?q=...&ftid=...` links):
    # recover them from a plus code, else by geocoding the place name.
    if not coords and q_value:
        coords = _decode_plus_code(q_value, warnings)
    if not coords:
        coords = _geocode_query(place or result["title"], warnings)
    if coords:
        result["extra"]["lat"], result["extra"]["lng"] = coords
    else:
        warnings.append("no coordinates found in URL, plus code, or by geocoding")
    if not result["extra"]["place"]:
        warnings.append("no place name extracted from URL or og:title")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_result(url: str) -> dict[str, Any]:
    return {
        "url": url,
        "kind": "unknown",
        "title": "",
        "text": "",
        "author": "",
        "date": "",
        "media": [],
        "extra": {},
        "warnings": [],
    }


def run(url: str, media_dir: Path | None) -> dict[str, Any]:
    result = build_result(url)
    kind = classify(url)
    result["kind"] = kind
    logger.info("url classified as %s", kind)

    if kind == "instagram":
        handle_instagram(url, media_dir, result)
    elif kind == "maps-list":
        handle_maps_list(url, result)
    elif kind == "maps":
        handle_maps(url, result)
    else:
        handle_article(url, result)
        resolved_url = result["url"]
        resolved = classify(resolved_url)
        if resolved == "maps-list" and resolved_url != url:
            result = build_result(url); result["kind"] = "maps-list"; handle_maps_list(resolved_url, result)
        elif resolved == "maps" and resolved_url != url:
            logger.info("redirect resolved to Google Maps; re-handling as maps")
            result = build_result(url); result["kind"] = "maps"; handle_maps(resolved_url, result)
    return result


def print_human(result: dict[str, Any]) -> None:
    lines = [
        f"url:      {result['url']}",
        f"kind:     {result['kind']}",
        f"title:    {result['title']}",
        f"author:   {result['author']}",
        f"date:     {result['date']}",
    ]
    if result["extra"]:
        lines.append(f"extra:    {json.dumps(result['extra'], ensure_ascii=False)}")
    if result["media"]:
        lines.append("media:    " + ", ".join(result["media"]))
    if result["warnings"]:
        lines.append("warnings: " + " | ".join(result["warnings"]))
    text = result["text"] or ""
    if text:
        lines.append("--- text ---")
        lines.append(text)
    sys.stdout.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch a URL (article / Instagram / Google Maps) and emit structured data."
    )
    parser.add_argument("url", help="URL to fetch")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON on stdout")
    parser.add_argument(
        "--media-dir", type=Path, default=None,
        help="directory where Instagram media is downloaded (yt-dlp, best effort)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging on stderr")
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    result = run(args.url, args.media_dir)

    if args.as_json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print_human(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

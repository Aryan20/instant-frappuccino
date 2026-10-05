"""Frappe Cloud Marketplace client.

The only guest API is ``press.api.marketplace.get_marketplace_apps`` (name/title/route).
Everything else — tagline, icon, supported versions, publisher, categories — comes from
the public app page, parsed best-effort. Source repos are not published at all, so we
resolve them from a curated map, GitHub links on the page, or ``frappe/<name>``; the UI
always lets the user correct the guess before installing.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from fmapp import __version__
from fmapp.core import paths

BASE = "https://cloud.frappe.io"
INDEX_URL = f"{BASE}/api/method/press.api.marketplace.get_marketplace_apps"
INDEX_TTL = 24 * 3600
DETAIL_TTL = 7 * 24 * 3600
_UA = f"instant-frappuccino/{__version__} (+https://github.com/rtCamp/frappe-manager)"

# Apps whose repo name differs from their marketplace name, or that are popular enough
# to deserve a guaranteed-correct source.
KNOWN_REPOS: dict[str, str] = {
    "erpnext": "frappe/erpnext",
    "hrms": "frappe/hrms",
    "crm": "frappe/crm",
    "helpdesk": "frappe/helpdesk",
    "lms": "frappe/lms",
    "insights": "frappe/insights",
    "builder": "frappe/builder",
    "drive": "frappe/drive",
    "gameplan": "frappe/gameplan",
    "wiki": "frappe/wiki",
    "payments": "frappe/payments",
    "print_designer": "frappe/print_designer",
    "raven": "The-Commit-Company/raven",
    "webshop": "frappe/webshop",
    "ecommerce_integrations": "frappe/ecommerce_integrations",
    "india_compliance": "resilient-tech/india-compliance",
    "lending": "frappe/lending",
    "education": "frappe/education",
    "non_profit": "frappe/non_profit",
    "erpnext_shipping": "frappe/erpnext-shipping",
    "frappe_whatsapp": "shridarpatil/frappe_whatsapp",
    "telephony": "frappe/telephony",
    "mail": "frappe/mail",
}

FEATURED = (
    "erpnext",
    "hrms",
    "crm",
    "helpdesk",
    "lms",
    "insights",
    "builder",
    "drive",
    "raven",
    "wiki",
    "gameplan",
    "print_designer",
    "payments",
    "india_compliance",
)


@dataclass
class MarketplaceApp:
    name: str
    title: str
    route: str = ""

    @property
    def page_url(self) -> str:
        return f"{BASE}/{self.route or 'marketplace/apps/' + self.name}"


@dataclass
class AppDetails:
    name: str
    tagline: str = ""
    icon_url: str = ""
    publisher: str = ""
    installs: str = ""
    supported_versions: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    github_links: list[str] = field(default_factory=list)
    repo: str = ""  # best guess, "org/repo"
    repo_confidence: str = "guess"  # known | page | guess

    def branches(self) -> list[str]:
        """Frappe branch names matching the advertised versions."""
        out = []
        for version in self.supported_versions:
            match = re.search(r"(\d+)", version)
            if match:
                out.append(f"version-{match.group(1)}")
            elif "nightly" in version.lower():
                out.append("develop")
        return out


# -- HTTP + cache ------------------------------------------------------------------------
def _get(url: str, timeout: float = 20) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _cache_file(key: str) -> Path:
    folder = paths.cache_dir() / "marketplace"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{re.sub(r'[^a-zA-Z0-9_.-]', '_', key)}.json"


def _cached(key: str, ttl: float) -> object | None:
    file = _cache_file(key)
    try:
        if time.time() - file.stat().st_mtime < ttl:
            return json.loads(file.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _store(key: str, value: object) -> None:
    _cache_file(key).write_text(json.dumps(value))


# -- index ---------------------------------------------------------------------------------
def fetch_index(force: bool = False) -> list[MarketplaceApp]:
    raw = None if force else _cached("index", INDEX_TTL)
    if raw is None:
        try:
            raw = json.loads(_get(INDEX_URL))["message"]
            _store("index", raw)
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError):
            # Offline: fall back to a stale cache rather than an empty marketplace.
            raw = _cached("index", float("inf")) or []
    apps = [MarketplaceApp(a["name"], a.get("title") or a["name"], a.get("route", "")) for a in raw]
    rank = {name: i for i, name in enumerate(FEATURED)}
    return sorted(apps, key=lambda a: (rank.get(a.name, len(rank)), a.title.lower()))


# -- details -------------------------------------------------------------------------------
class _PageParser(HTMLParser):
    """Collects visible text tokens, links and images from a marketplace page."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []
        self.links: list[str] = []
        self.images: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag == "a" and attr.get("href"):
            self.links.append(attr["href"] or "")
        elif tag == "img" and attr.get("src"):
            self.images.append(attr["src"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.texts.append(" ".join(data.split()))


def parse_details(app: MarketplaceApp, html: str) -> AppDetails:
    parser = _PageParser()
    parser.feed(html)
    texts = parser.texts
    details = AppDetails(name=app.name)

    def after(label: str) -> str:
        for i, text in enumerate(texts[:-1]):
            if text.lower() == label.lower():
                return texts[i + 1]
        return ""

    details.publisher = after("Publisher")
    versions = after("Supported versions")
    details.supported_versions = [v.strip() for v in versions.split(",") if v.strip()]

    if "Categories" in texts:
        start = texts.index("Categories") + 1
        for text in texts[start : start + 8]:
            if text in ("Resources", "Homepage", "Documentation", "Support"):
                break
            details.categories.append(text)

    # Hero block reads: <title> <tagline> "Install now" <count> "installs".
    if "Install now" in texts:
        hero = texts.index("Install now")
        if hero >= 1 and texts[hero - 1] != app.title:
            details.tagline = texts[hero - 1]
        if hero + 2 < len(texts) and texts[hero + 2].startswith("install"):
            details.installs = texts[hero + 1]

    # The app icon is the first image on the page (the site chrome uses inline SVG).
    icon = next((src for src in parser.images if src), "")
    if icon:
        details.icon_url = icon if icon.startswith("http") else BASE + urllib.parse.quote(icon)

    links = []
    for href in parser.links:
        m = re.match(r"https?://github\.com/([\w.-]+)/([\w.-]+)", href)
        if m and m.group(2) not in ("issues", "pulls"):
            slug = f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
            if slug not in links:
                links.append(slug)
    details.github_links = links
    resolve_repo(details)
    return details


def resolve_repo(details: AppDetails) -> None:
    if details.name in KNOWN_REPOS:
        details.repo, details.repo_confidence = KNOWN_REPOS[details.name], "known"
        return
    norm = details.name.replace("_", "").replace("-", "").lower()
    for slug in details.github_links:
        if slug.split("/")[1].replace("_", "").replace("-", "").lower() == norm:
            details.repo, details.repo_confidence = slug, "page"
            return
    details.repo, details.repo_confidence = f"frappe/{details.name}", "guess"


def fetch_details(app: MarketplaceApp, force: bool = False) -> AppDetails:
    key = f"app_{app.name}"
    raw = None if force else _cached(key, DETAIL_TTL)
    if isinstance(raw, dict):
        return AppDetails(**raw)
    html = _get(app.page_url).decode("utf-8", errors="ignore")
    details = parse_details(app, html)
    _store(key, asdict(details))
    return details


def fetch_icon(url: str) -> bytes | None:
    key = "icon_" + re.sub(r"\W", "_", url)[-80:]
    file = _cache_file(key).with_suffix(".bin")
    if file.exists():
        return file.read_bytes()
    try:
        data = _get(url, timeout=10)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    file.write_bytes(data)
    return data


_BRANCHES: dict[tuple[str, bool], tuple[float, list[str]]] = {}
BRANCH_TTL = 600  # seconds; GitHub allows only 60 unauthenticated requests per hour


def list_branches(org_repo: str, token: str = "") -> list[str]:
    """Remote branches via the GitHub API, cached in memory (failures are not cached)."""
    key = (org_repo.lower(), bool(token))  # a token can reveal private repos' branches
    cached = _BRANCHES.get(key)
    if cached and time.time() - cached[0] < BRANCH_TTL:
        return cached[1]
    branches = _fetch_branches(org_repo, token)
    if branches:
        _BRANCHES[key] = (time.time(), branches)
    return branches


def _fetch_branches(org_repo: str, token: str) -> list[str]:
    url = f"https://api.github.com/repos/{org_repo}/branches?per_page=100"
    headers = {"User-Agent": _UA, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            branches = [b["name"] for b in json.loads(response.read())]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, TypeError):
        return []
    preferred = [b for b in branches if re.fullmatch(r"version-\d+|develop|main|master", b)]
    return sorted(preferred, reverse=True) + sorted(b for b in branches if b not in preferred)

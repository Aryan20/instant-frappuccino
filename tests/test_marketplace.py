from fmapp.core import marketplace
from fmapp.core.catalog import Catalog, CatalogApp
from fmapp.core.settings import Settings
from fmapp.core.textutil import clean_output, mask

PAGE = """
<html><head><style>.x{}</style><script>var t = "Install now";</script></head><body>
<nav><a href="/marketplace">Marketplace</a> Raven Documentation</nav>
<img src="/files/raven icon.png"/>
<div>Raven</div><div>Messaging for teams that use ERPNext</div>
<button>Install now</button><span>7.5k</span><span>installs</span>
<p>Publisher</p><p>Frappe Tech</p>
<p>Supported versions</p><p>Version 16, Version 15, Nightly</p>
<p>Categories</p><p>Free</p><p>Communication</p><p>Resources</p>
<a href="https://github.com/The-Commit-Company/Raven/issues">Issues</a>
<a href="https://github.com/frappe/raven">Mirror</a>
<a href="https://github.com/acme/raven-addons">Addons</a>
</body></html>
"""


def test_parse_details():
    app = marketplace.MarketplaceApp("raven", "Raven")
    d = marketplace.parse_details(app, PAGE)
    assert d.tagline == "Messaging for teams that use ERPNext"
    assert d.installs == "7.5k"
    assert d.publisher == "Frappe Tech"
    assert d.supported_versions == ["Version 16", "Version 15", "Nightly"]
    assert d.categories == ["Free", "Communication"]
    assert d.icon_url == "https://cloud.frappe.io/files/raven%20icon.png"
    assert "The-Commit-Company/Raven" in d.github_links
    assert d.repo == "The-Commit-Company/raven" and d.repo_confidence == "known"
    assert d.branches() == ["version-16", "version-15", "develop"]


def test_repo_resolution_from_page_then_guess():
    app = marketplace.MarketplaceApp("lumen_pos", "LumenPOS")
    d = marketplace.parse_details(app, '<a href="https://github.com/acme/Lumen-POS">x</a>')
    assert (d.repo, d.repo_confidence) == ("acme/Lumen-POS", "page")
    d = marketplace.parse_details(marketplace.MarketplaceApp("zzz", "Z"), "<p>nothing</p>")
    assert (d.repo, d.repo_confidence) == ("frappe/zzz", "guess")


def test_index_offline_uses_stale_cache(monkeypatch):
    marketplace._store("index", [{"name": "zeta", "title": "Zeta"}, {"name": "erpnext", "title": "ERPNext"}])

    def offline(*_a, **_k):
        raise marketplace.urllib.error.URLError("offline")

    monkeypatch.setattr(marketplace, "_get", offline)
    apps = marketplace.fetch_index(force=True)
    assert [a.name for a in apps] == ["erpnext", "zeta"]  # featured first


def test_catalog_roundtrip(tmp_path):
    file = tmp_path / "apps.json"
    catalog = Catalog(file)
    app = CatalogApp(title="Mine", repo="myorg/mine", ref="main")
    catalog.upsert(app)
    app.ref = "develop"
    catalog.upsert(app)
    reloaded = Catalog(file)
    assert len(reloaded.apps) == 1 and reloaded.apps[0].ref == "develop"
    reloaded.remove(app.id)
    assert Catalog(file).apps == []


def test_settings_roundtrip_is_private():
    s = Settings(github_token="ghp_x", default_frappe_branch="version-16")
    s.save()
    assert Settings.load().default_frappe_branch == "version-16"
    assert (Settings.file().stat().st_mode & 0o777) == 0o600


def test_text_helpers():
    assert clean_output("\x1b[32mok\x1b[0m\nprogress 10%\rprogress 100%\n") == "ok\nprogress 100%\n"
    assert mask("token=ghp_abcdef", ["ghp_abcdef"]) == "token=••••••"


def test_branch_lookups_are_cached_but_failures_are_not(monkeypatch):
    calls = []
    results = iter([[], ["main", "develop"]])
    monkeypatch.setattr(marketplace, "_BRANCHES", {})
    monkeypatch.setattr(
        marketplace, "_fetch_branches", lambda repo, token: calls.append(repo) or next(results)
    )
    assert marketplace.list_branches("org/app") == []  # e.g. rate limited: not cached
    assert marketplace.list_branches("org/app") == ["main", "develop"]
    assert marketplace.list_branches("Org/App") == ["main", "develop"]  # served from cache
    assert len(calls) == 2

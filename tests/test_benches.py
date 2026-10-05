from fmapp.core import benches, paths
from fmapp.core.models import BenchKind, BenchStatus
from tests.conftest import make_bench


def test_discover_fm_bench():
    root = paths.benches_dir()
    make_bench(root, "dev.localhost", admin_tools=True)
    compose = (root / "dev.localhost" / "docker-compose.yml").resolve()
    [bench] = benches.discover(statuses={compose: BenchStatus.RUNNING})
    assert bench.name == "dev.localhost"
    assert bench.kind is BenchKind.FM
    assert bench.status is BenchStatus.RUNNING
    assert bench.admin_tools
    assert [a.name for a in bench.apps] == ["frappe", "erpnext"]
    assert bench.frappe_version == "15.40.0"
    frappe = bench.apps[0]
    assert (frappe.branch, frappe.short_commit) == ("version-15", "01234567")
    assert frappe.remote == "https://github.com/frappe/frappe.git"


def test_discover_deployer_bench_and_releases():
    make_bench(paths.benches_dir(), "prod.localhost", deployer=True)
    [bench] = benches.discover(statuses={})
    assert bench.kind is BenchKind.DEPLOYER
    assert bench.status is BenchStatus.STOPPED
    assert [r.name for r in bench.releases] == ["release_20260101_120000", "release_20251201_090000"]
    [active] = [r for r in bench.releases if r.active]
    assert active.name == "release_20260101_120000" and active.created.year == 2026
    assert {a.name for a in bench.apps} == {"frappe", "erpnext"}


def test_missing_config_is_broken_and_unknown_without_docker():
    root = paths.benches_dir()
    make_bench(root, "ok.localhost")
    broken = make_bench(root, "broken.localhost")
    (broken / "bench_config.toml").unlink()
    found = {b.name: b for b in benches.discover(statuses=None)}
    assert found["broken.localhost"].status is BenchStatus.BROKEN
    assert "bench_config.toml" in found["broken.localhost"].error
    assert found["ok.localhost"].status is BenchStatus.UNKNOWN


def test_dirs_without_compose_are_ignored():
    (paths.benches_dir() / "not-a-bench").mkdir()
    assert benches.discover() == []


def test_compose_state_parsing():
    assert benches._parse_compose_state("running(10)") is BenchStatus.RUNNING
    assert benches._parse_compose_state("running(2), exited(1)") is BenchStatus.PARTIAL
    assert benches._parse_compose_state("exited(3)") is BenchStatus.STOPPED
    assert benches._parse_compose_state("") is BenchStatus.STOPPED

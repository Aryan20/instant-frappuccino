import json
import plistlib
import sys

import pytest

from fmapp.core import autostart, engine, paths
from fmapp.core.containers import parse_ps
from fmapp.core.info import parse_info
from fmapp.core.operations import SYSTEM, Operations
from fmapp.core.settings import Settings


@pytest.mark.parametrize(
    ("context", "endpoint", "platform", "have", "expected"),
    [
        ("desktop-linux", "unix:///Users/a/.docker/run/docker.sock", "darwin", set(), "docker-desktop"),
        ("orbstack", "unix:///Users/a/.orbstack/run/docker.sock", "darwin", set(), "orbstack"),
        ("colima", "unix:///Users/a/.colima/default/docker.sock", "darwin", set(), "colima"),
        ("default", "unix:///var/run/docker.sock", "darwin", {"orb"}, "orbstack"),
        ("default", "unix:///var/run/docker.sock", "darwin", set(), "unknown"),
        ("default", "unix:///var/run/docker.sock", "linux", {"systemctl"}, "systemd"),
        ("rootless", "unix:///run/user/1000/docker.sock", "linux", {"systemctl"}, "rootless"),
        ("desktop-linux", "unix:///home/a/.docker/desktop/docker.sock", "linux", set(), "docker-desktop"),
    ],
)
def test_classify(context, endpoint, platform, have, expected):
    assert engine.classify(context, endpoint, platform, have).value == expected


def test_control_argv(monkeypatch):
    monkeypatch.setattr(engine, "_has_desktop_cli", lambda _d: True)
    assert engine.control_argv(engine.Provider.DOCKER_DESKTOP, "restart", "docker") == [
        ["docker", "desktop", "restart", "--timeout", "300"]
    ]
    orb = engine.control_argv(engine.Provider.ORBSTACK, "restart", "docker")
    assert [a[-1] for a in orb] == ["stop", "start"]
    assert engine.control_argv(engine.Provider.SYSTEMD, "start", "docker") == [
        ["pkexec", "systemctl", "start", "docker"]
    ]
    with pytest.raises(ValueError):
        engine.control_argv(engine.Provider.UNKNOWN, "start", "docker")
    with pytest.raises(ValueError):
        engine.control_argv(engine.Provider.COLIMA, "explode", "docker")


def test_docker_desktop_legacy_fallback(monkeypatch):
    monkeypatch.setattr(engine, "_has_desktop_cli", lambda _d: False)
    monkeypatch.setattr(sys, "platform", "darwin")
    argvs = engine.control_argv(engine.Provider.DOCKER_DESKTOP, "start", "docker")
    assert argvs == [["open", "-g", "-j", "-a", "Docker"]]


def _ps_line(name, project, service, workdir, state="running", ports=""):
    labels = (
        f"com.docker.compose.project={project},com.docker.compose.service={service},"
        f"com.docker.compose.project.working_dir={workdir}"
    )
    return json.dumps(
        {
            "ID": name + "id",
            "Names": name,
            "Labels": labels,
            "State": state,
            "Status": "Up 1 hour",
            "Image": "img",
            "Ports": ports,
        }
    )


def test_parse_ps_groups_fm_containers():
    home = paths.fm_home()
    lines = "\n".join(
        [
            _ps_line("fm_global-db", "services", "global-db", str(home / "services")),
            _ps_line("fm__a__frappe", "alocalhost", "frappe", str(home / "sites" / "a.localhost")),
            _ps_line("lando_proxy", "lando", "proxy", "/Users/x/.lando", ports="0.0.0.0:80->80/tcp"),
        ]
    )
    db, frappe, lando = parse_ps(lines, home)
    assert db.is_global and db.bench is None
    assert frappe.bench == "a.localhost" and frappe.running
    assert lando.bench == "" and lando.publishes_web_ports


FM_INFO = """
⚙️  Getting bench info
┌───────────────────┬──────────────────────────────────┐
│ Bench Url         │ http://a.localhost               │
├───────────────────┼──────────────────────────────────┤
│ Frappe Username   │ administrator                    │
├───────────────────┼──────────────────────────────────┤
│ Frappe Password   │ adm1n-pw                         │
├───────────────────┼──────────────────────────────────┤
│ Admin Tools       │ Service  ┃ URL                   │
│                   │ ━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━ │
│                   │ Mailpit  │ http://a.localhost/mailpit │
│                   │ Adminer  │ http://a.localhost/adminer │
│                   │                                  │
│                   │ Authentication Required:         │
│                   │   Username: admin                │
│                   │   Password: s3cr3t               │
├───────────────────┼──────────────────────────────────┤
│ Bench Services    │  frappe   ✓    nginx   ✓         │
│                   │  redis    ✓                      │
└───────────────────┴──────────────────────────────────┘
"""


def test_parse_info():
    rows = {r.key: r for r in parse_info(FM_INFO)}
    assert rows["Bench Url"].is_url
    assert rows["Frappe Password"].secret and rows["Frappe Password"].value == "adm1n-pw"
    assert rows["Admin Tools · Mailpit"].value == "http://a.localhost/mailpit"
    assert rows["Admin Tools username"].value == "admin"
    assert rows["Admin Tools password"].value == "s3cr3t" and rows["Admin Tools password"].secret
    assert "redis" in rows["Bench Services"].value and "nginx" in rows["Bench Services"].value


@pytest.fixture
def ops(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("fm", "docker"):
        exe = bin_dir / tool
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    monkeypatch.setattr(engine, "provider", lambda *_a, **_k: engine.Provider.COLIMA)
    return Operations(Settings(fm_path=str(bin_dir / "fm"), docker_path=str(bin_dir / "docker")))


def test_start_everything(ops):
    job = ops.start_everything(["a.localhost", "b.localhost"])
    assert job.bench == SYSTEM
    titles = [s.title for s in job.steps]
    assert titles[:3] == ["Start Docker engine (if stopped)", "Wait for Docker", "Start global services"]
    check = job.steps[0].argv  # "start only if not running" happens inside the process
    assert check[:2] == ["/bin/sh", "-c"] and "info >/dev/null" in check[2]
    assert check[-2:] == ["colima", "start"] or check[-1] == "start"
    assert [s.argv[-1] for s in job.steps[3:]] == ["a.localhost", "b.localhost"]


def test_restart_parts_and_repair(ops):
    argv = ops.restart_parts("a.localhost", "redis").steps[0].argv
    assert argv[-3:] == ["--no-web", "--no-workers", "--redis"]
    repair = [s.title for s in ops.repair("a.localhost").steps]
    assert repair[-2:] == ["Restart proxy", "Recreate bench containers"]


def test_reclaim_never_touches_volumes(ops):
    argvs = [" ".join(s.argv) for s in ops.reclaim_space().steps]
    assert all("volume" not in a and "system prune" not in a for a in argvs)


def test_login_item_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.Path, "home", lambda: tmp_path)
    autostart.sync("login")
    assert autostart.is_enabled()
    item = autostart.item_path()
    if sys.platform == "darwin":
        plist = plistlib.loads(item.read_bytes())
        assert plist["RunAtLoad"] and plist["ProgramArguments"][-1] == "--background"
    else:
        assert "--background" in item.read_text()
    autostart.sync("launch")
    assert not autostart.is_enabled()


def test_legacy_config_is_migrated(tmp_path):
    old, new = tmp_path / "fm-app", tmp_path / "instant-frappuccino"
    (old / "deployer").mkdir(parents=True)
    (old / "settings.json").write_text("{}")
    assert paths.migrate_legacy(old, new)
    assert (new / "settings.json").exists() and (new / "deployer").is_dir() and not old.exists()
    assert not paths.migrate_legacy(old, new)  # idempotent


def test_sync_removes_legacy_login_item(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.Path, "home", lambda: tmp_path)
    legacy = autostart._legacy_item()
    legacy.parent.mkdir(parents=True)
    legacy.write_text("old")
    autostart.sync("off")
    assert not legacy.exists()


def test_engine_provider_never_contacts_the_daemon(monkeypatch):
    calls = []
    monkeypatch.setattr(
        engine, "_out", lambda argv, timeout=8: calls.append(argv) or (0, "colima unix:///x/.colima/d.sock")
    )
    monkeypatch.setattr(engine, "which", lambda name, override="": "/usr/bin/docker")
    assert engine.provider() is engine.Provider.COLIMA
    assert all("info" not in argv for argv in calls)
    assert engine.provider(override="orbstack") is engine.Provider.ORBSTACK

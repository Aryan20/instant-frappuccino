import tomllib
from pathlib import Path

import pytest

from fmapp.core import benches, deployer, paths
from fmapp.core.models import AppRef, BenchKind, SiteSpec
from fmapp.core.operations import Operations, display_argv, strip_credentials
from fmapp.core.settings import Settings
from tests.conftest import bench_script, make_bench


@pytest.fixture
def ops(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for tool in ("fm", "fmd"):
        exe = fake_bin / tool
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return Operations(
        Settings(
            fm_path=str(fake_bin / "fm"), fmd_path=str(fake_bin / "fmd"), github_token="ghp_secret_token"
        )
    )


def test_create_fm_site(ops):
    spec = SiteSpec(
        "demo.localhost", apps=[AppRef("hrms", "version-15"), AppRef("org/x")], admin_password="s3cret!"
    )
    job = ops.create_site(spec)
    argv = job.steps[0].argv
    assert argv[1:4] == ["-n", "create", "demo.localhost"]
    assert argv.count("--apps") == 3
    assert argv[argv.index("--apps") + 1] == "frappe/frappe:version-15"
    assert "frappe/hrms:version-15" in argv
    assert job.env["GITHUB_TOKEN"] == "ghp_secret_token"
    assert "BatchMode=yes" in job.env["GIT_SSH_COMMAND"]  # git over SSH never waits on a prompt
    assert "s3cret!" not in job.preview()
    assert "ghp_secret_token" not in " ".join(argv)


def test_create_deployer_site_writes_config(ops):
    spec = SiteSpec(
        "prod.localhost", kind=BenchKind.DEPLOYER, apps=[AppRef("erpnext", "version-15")], rollback=False
    )
    job = ops.create_site(spec)
    create, write, deploy = job.steps
    assert "--environment" in create.argv and create.argv[create.argv.index("--environment") + 1] == "prod"
    assert create.argv.count("--apps") == 1  # fmd builds the rest
    assert deploy.argv[1:] == ["deploy", "pull", "--config", str(deployer.config_path("prod.localhost"))]
    write.action({})
    config = tomllib.loads(deployer.config_path("prod.localhost").read_text())
    assert config["github_token"] == "${GITHUB_TOKEN}"
    assert [a["repo"] for a in config["apps"]] == ["frappe/frappe", "frappe/erpnext"]
    assert config["switch"]["rollback"] is False


def test_add_apps_fm_bench_installs_only_new(ops):
    bench_dir = make_bench(paths.benches_dir(), "dev.localhost")
    [bench] = benches.discover(compose=[])
    job = ops.add_apps(bench, [AppRef("frappe/hrms", "version-15")])
    note, get_app, install = job.steps
    assert bench_script(note).endswith("ls apps > /tmp/.if-apps-before")
    script = bench_script(get_app)
    assert "bench get-app --branch version-15" in script
    assert "x-access-token:ghp_secret_token@github.com/frappe/hrms" in script
    assert "remote set-url upstream" in script  # token scrubbed after clone
    # whatever get-app added is installed (folder names can differ from repo names)
    assert "grep -vxF -f /tmp/.if-apps-before" in bench_script(install)
    assert "bench --site dev.localhost install-app $new" in bench_script(install)
    assert bench_dir.exists()


def test_add_apps_deployer_bench_redeploys(ops):
    make_bench(paths.benches_dir(), "prod.localhost", deployer=True)
    [bench] = benches.discover(compose=[])
    job = ops.add_apps(bench, [AppRef("frappe/hrms", "version-15")])
    write, deploy = job.steps
    write.action({})
    apps = deployer.apps_of(deployer.load("prod.localhost"))
    assert [a.name_guess for a in apps] == ["frappe", "erpnext", "hrms"]
    assert deploy.argv[1:3] == ["deploy", "pull"]


def test_delete_and_update(ops):
    argv = ops.delete("x.localhost", drop_db=False).steps[0].argv
    assert argv[1:] == ["-n", "delete", "x.localhost", "--yes", "--no-delete-db-from-global-db"]
    argv = ops.update("x.localhost", developer_mode="enable").steps[0].argv
    assert argv[-2:] == ["--developer-mode", "enable"]


def test_display_and_strip_helpers():
    assert display_argv(["fm", "create", "x", "--admin-pass", "hunter2"]) == "fm create x --admin-pass ••••••"
    assert strip_credentials("https://x-access-token:abc@github.com/o/r") == "https://github.com/o/r"


def test_missing_tool_raises():
    ops = Operations(Settings(fm_path="/nonexistent/fm"))
    with pytest.raises(RuntimeError, match="Frappe Manager"):
        ops.start("x")


def test_import_fmd_config(tmp_path):
    imported = deployer.load_file("tests/fixtures/site.toml")
    assert imported.site == "shop.localhost"
    assert imported.frappe_ref == "version-15"
    assert [a.name_guess for a in imported.apps] == ["erpnext", "shop"]
    assert imported.options.maintenance_mode is False and imported.options.python_version == "3.11"
    assert "hooks on 1 app kept" in imported.notes and "ship target kept" in imported.notes
    assert "Frappe Cloud sync kept" in imported.notes


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("not = [valid", "not valid TOML"),
        ('bench_name = "x"', "no site_name"),
        ('site_name = "a.localhost"\nbench_name = "b"', "differs"),
        ('site_name = "a.localhost"\n[[apps]]\nref = "main"', "has no repo"),
    ],
)
def test_import_rejects_bad_configs(tmp_path, body, error):
    file = tmp_path / "bad.toml"
    file.write_text(body)
    with pytest.raises(deployer.ConfigError, match=error):
        deployer.load_file(file)


def test_deployer_site_from_imported_config_keeps_everything(ops):
    imported = deployer.load_file("tests/fixtures/site.toml")
    spec = SiteSpec(
        "shop.localhost",
        kind=BenchKind.DEPLOYER,
        frappe_ref=imported.frappe_ref,
        apps=[*imported.apps, AppRef("frappe/hrms", "version-15")],
        maintenance_mode=True,
        backups=True,
        rollback=False,
        python_version="3.12",
        base_config=imported.config,
    )
    job = ops.create_site(spec)
    job.steps[1].action({})
    saved = deployer.config_path("shop.localhost")
    config = tomllib.loads(saved.read_text())
    assert (saved.stat().st_mode & 0o777) == 0o600
    assert config["bench_name"] == "shop.localhost"
    erpnext = next(a for a in config["apps"] if "erpnext" in a["repo"])
    assert erpnext["repo"] == "https://github.com/frappe/erpnext"  # user's spelling kept
    assert erpnext["after_bench_build"] == "echo built"  # per-app hook kept
    mono = next(a for a in config["apps"] if a.get("subdir_path") == "apps/shop")
    assert mono["symlink"] is False  # user's choice wins over our default
    assert any(a["repo"] == "frappe/hrms" for a in config["apps"])  # added in wizard
    assert config["release"]["python_version"] == "3.12"
    assert config["release"]["releases_retain_limit"] == 9 and config["release"]["use_fc_deps"] is True
    assert config["switch"]["maintenance_mode"] is True and config["switch"]["migrate_timeout"] == 900
    assert config["switch"]["site_config"]["host_name"] == "https://shop.example.com"
    assert config["ship"]["host"] == "10.0.0.5" and config["fc"]["api_key"] == "fc_key_123"
    assert {"ghp_literal_in_file", "fc_key_123", "fc_secret_456"} <= set(job.secrets)


def test_import_into_existing_site_renames_to_bench(ops):
    make_bench(paths.benches_dir(), "prod.localhost", deployer=True)
    [bench] = benches.discover(compose=[])
    job = ops.import_config(bench, deployer.load_file("tests/fixtures/site.toml"))
    job.steps[0].action({})
    config = deployer.load("prod.localhost")
    assert config["site_name"] == config["bench_name"] == "prod.localhost"
    assert config["apps"][0]["repo"] == "frappe/frappe"


def test_build_whole_bench_and_selected_apps(ops):
    whole = ops.build("a.localhost").steps
    assert len(whole) == 1 and bench_script(whole[0]).endswith("|| exit 1; bench build")
    job = ops.build("a.localhost", ["erpnext", "hrms"], production=True, force=True, clear_cache=True)
    build, clear = job.steps
    assert bench_script(build).endswith("bench build --apps erpnext,hrms --production --force")
    assert bench_script(clear).endswith("bench --site a.localhost clear-cache")
    assert job.title == "Build erpnext, hrms · a.localhost"
    with pytest.raises(ValueError):
        ops.build("a.localhost", ["erpnext; rm -rf /"])


def test_everyday_site_tools(ops):
    def script(job, index=0):
        return bench_script(job.steps[index]).split("|| exit 1; ", 1)[1]

    assert script(ops.clear_cache("a.localhost", "site")) == "bench --site a.localhost clear-cache"
    both = script(ops.clear_cache("a.localhost"))
    assert "clear-cache" in both and "clear-website-cache" in both
    maintenance = ops.maintenance("a.localhost", True)
    assert script(maintenance).endswith("set-maintenance-mode on")
    # web workers cache site_config for ~60s, so the toggle restarts web to apply immediately
    assert maintenance.steps[1].argv[-4:] == ["restart", "a.localhost", "--web", "--no-workers"]
    assert script(ops.maintenance("a.localhost", False)).endswith("set-maintenance-mode off")
    assert script(ops.scheduler("a.localhost", False)).endswith("scheduler resume")
    job = ops.set_admin_password("a.localhost", "n3w p@ss")
    assert script(job).endswith("set-admin-password 'n3w p@ss'")
    assert "n3w p@ss" in job.secrets and "n3w p@ss" not in job.preview()
    assert script(ops.run_command("a.localhost", "bench --site a.localhost list-apps")).endswith("list-apps")
    tests = ops.run_tests("a.localhost", "erpnext", "erpnext.stock.tests")
    assert script(tests, 0).endswith("set-config allow_tests true")
    assert script(tests, 1).endswith("run-tests --app erpnext --module erpnext.stock.tests")
    assert ops.shell_command("a.localhost", console=True)[-1] == "--bench-console"
    for bad in (
        lambda: ops.run_command("a", "  "),
        lambda: ops.set_admin_password("a", ""),
        lambda: ops.run_tests("a", "x;y"),
        lambda: ops.pull_apps("a", []),
    ):
        with pytest.raises(ValueError):
            bad()


def test_login_url_parsing():
    out = "⚙️  Working\nLogin URL: http://a.localhost:80/app?sid=abc123\n"
    assert Operations.parse_login_url(out) == "http://a.localhost:80/app?sid=abc123"
    assert Operations.parse_login_url("User Administrator does not exist") is None


def test_pull_apps_then_migrate_and_build(ops):
    job = ops.pull_apps("a.localhost", ["erpnext", "hrms"])
    titles = [s.title for s in job.steps]
    assert titles == ["Pull erpnext", "Pull hrms", "bench migrate", "Build pulled apps"]
    assert "git -C apps/erpnext pull --ff-only ||" in bench_script(job.steps[0])
    assert bench_script(job.steps[-1]).endswith("bench build --apps erpnext,hrms")
    assert len(ops.pull_apps("a.localhost", ["erpnext"], migrate=False, build=False).steps) == 1


def test_backups_listing_and_restore(ops):
    bench_dir = make_bench(paths.benches_dir(), "a.localhost")
    [bench] = benches.discover(compose=[])
    folder = bench.backups_dir
    folder.mkdir(parents=True)
    for name in (
        "20260101_100000-a_localhost-database.sql.gz",
        "20260101_100000-a_localhost-files.tar",
        "20260101_100000-a_localhost-private-files.tar",
        "20260102_090000-a_localhost-database.sql.gz",
        "20260102_090000-a_localhost-site_config_backup.json",
        "notes.txt",
    ):
        (folder / name).write_text("x")
    [bench] = benches.discover(compose=[])
    newest, older = bench.backups
    assert newest.stamp == "20260102_090000" and newest.public_files is None
    assert older.public_files and older.private_files and older.size == 3
    secrets = paths.fm_home() / "services" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "db_root_password.txt").write_text("r00t\n")
    job = ops.restore(bench, older)
    restore = bench_script(job.steps[0])
    assert "restore sites/a.localhost/private/backups/20260101_100000-a_localhost-database.sql.gz" in restore
    assert (
        "--with-public-files sites/a.localhost/private/backups/20260101_100000-a_localhost-files.tar"
        in restore
    )
    assert "r00t" in job.secrets and "r00t" not in job.preview()
    assert job.steps[1].title == "bench migrate"
    assert bench_dir.exists()


def test_site_flags_from_site_config():
    make_bench(paths.benches_dir(), "a.localhost")
    site_dir = paths.benches_dir() / "a.localhost" / "workspace" / "frappe-bench" / "sites" / "a.localhost"
    site_dir.mkdir(parents=True)
    (site_dir / "site_config.json").write_text(
        '{"maintenance_mode": 1, "pause_scheduler": 1, "db_password": "s"}'
    )
    [bench] = benches.discover(compose=[])
    assert bench.maintenance_mode and bench.scheduler_paused


def test_terminal_argv():
    from fmapp.core.terminal import terminal_argv

    mac = terminal_argv(["fm", "shell", "a.localhost"], name="a.localhost-shell", platform="darwin")
    assert mac[0] == "/usr/bin/open" and mac[1].endswith("a.localhost-shell.command")
    script = Path(mac[1])
    assert script.stat().st_mode & 0o100  # executable, so Terminal runs it
    body = script.read_text()
    assert body.startswith("#!/bin/bash") and "\nfm shell a.localhost\n" in body
    assert "osascript" not in body  # no Automation permission needed


def test_redeploy_keeps_per_app_extras(ops):
    make_bench(paths.benches_dir(), "prod.localhost", deployer=True)
    [bench] = benches.discover(compose=[])
    deployer.save(
        "prod.localhost",
        {
            "site_name": "prod.localhost",
            "apps": [
                {"repo": "frappe/frappe", "ref": "version-15"},
                {"repo": "frappe/erpnext", "ref": "version-15", "after_bench_build": "echo hi"},
            ],
        },
    )
    ops.add_apps(bench, [AppRef("frappe/hrms", "version-15")]).steps[0].action({})
    apps = deployer.load("prod.localhost")["apps"]
    assert [a["repo"] for a in apps] == ["frappe/frappe", "frappe/erpnext", "frappe/hrms"]
    assert apps[1]["after_bench_build"] == "echo hi"
    ops.remove_app(bench, "hrms").steps[0].action({})
    assert [a["repo"] for a in deployer.load("prod.localhost")["apps"]] == ["frappe/frappe", "frappe/erpnext"]


def test_adopt_config_pins_detached_head_to_commit(ops):
    from fmapp.core.models import InstalledApp

    make_bench(paths.benches_dir(), "a.localhost")
    [bench] = benches.discover(compose=[])
    bench.apps = [
        InstalledApp(
            "frappe",
            branch="",
            commit="abc123def456" * 3 + "abcd",
            remote="https://github.com/frappe/frappe.git",
        )
    ]
    [frappe] = ops.adopt_config(bench)["apps"]
    assert frappe["ref"] == "abc123def456" * 3 + "abcd"


def test_private_files_are_created_owner_only(tmp_path):
    target = tmp_path / "secret.json"
    paths.write_private(target, "{}")
    assert (target.stat().st_mode & 0o777) == 0o600 and target.read_text() == "{}"

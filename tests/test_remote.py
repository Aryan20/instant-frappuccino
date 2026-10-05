import os
import shlex
import subprocess

import pytest

from fmapp.core import benches, deployer, operations, paths, remote
from fmapp.core.hosts import LOCAL, Host
from fmapp.core.models import AppRef, Backup
from fmapp.core.operations import NotOnServer, Operations
from fmapp.core.settings import Settings
from tests.conftest import make_bench

SERVER = Host(name="Staging", destination="deploy@staging.example.com", port=2222)


def _script(argv: list[str]) -> str:
    """The command line the server's login shell runs."""
    assert argv[-2] == "--" and argv[-1].startswith("bash -lc ")
    return shlex.split(argv[-1])[2]


@pytest.fixture
def ops():
    return Operations(Settings(github_token="ghp_secret_token"), SERVER)


def test_local_commands_are_unchanged():
    assert remote.wrap(LOCAL, ["fm", "list"]) == ["fm", "list"]


def test_wrap_runs_through_ssh_login_shell():
    argv = remote.wrap(SERVER, ["fm", "-n", "start", "a b.com"])
    assert "BatchMode=yes" in argv and argv[argv.index("-p") : argv.index("-p") + 2] == ["-p", "2222"]
    assert "-tt" not in argv and SERVER.destination in argv
    script = _script(argv)
    assert script.startswith("export NO_COLOR=1 ")
    assert script.endswith("fm -n start 'a b.com'")
    assert "FRAPPE_MANAGER_HOME" not in script  # ~/frappe is fm's default


def test_wrap_tty_and_custom_fm_home():
    host = Host(name="Prod", destination="prod", fm_home="~/sites home")
    argv = remote.wrap(host, ["fm", "shell", "a.com"], tty=True)
    assert "-tt" in argv
    script = _script(argv)
    assert "NO_COLOR" not in script  # an interactive shell keeps the server's TERM
    assert "FRAPPE_MANAGER_HOME=\"$HOME\"/'sites home'" in script


def test_server_jobs_use_server_tools_and_say_where(ops):
    job = ops.start("a.example.com")
    assert job.title == "Start a.example.com · Staging" and job.host is SERVER
    assert _script(job.steps[0].argv).endswith("fm -n start a.example.com")
    assert "ghp_secret_token" not in " ".join(job.steps[0].argv)


def test_local_only_actions_refuse_servers(ops):
    for build in (lambda: ops.open_code("a"), lambda: ops.engine_action("start"), ops.reclaim_space):
        with pytest.raises(NotOnServer):
            build()
    assert ops.start_everything([]).steps[0].title == "Start global services"  # no engine step


def test_deploy_uploads_config_without_local_token(ops):
    config = deployer.build_config(
        "a.example.com", [AppRef("frappe/frappe", "version-15")], deployer.DeployOptions()
    )
    config["github_token"] = "${GITHUB_TOKEN}"
    save, upload, deploy = ops.deploy("a.example.com", config).steps
    assert upload.stdin and "ghp_secret_token" not in upload.stdin
    assert 'github_token = ""' in upload.stdin
    assert "cat > .instant-frappuccino/deployer/a.example.com.toml" in _script(upload.argv)
    assert _script(deploy.argv).endswith("--config .instant-frappuccino/deployer/a.example.com.toml")
    save.action({})
    assert deployer.load("a.example.com", SERVER.id) is not None
    assert deployer.load("a.example.com") is None  # this machine's configs stay separate


def _fake_tools(folder):
    """``fm`` that treats -c exactly like fm does (``bash -c "<cmd>"`` + shlex.split), and a
    ``bench`` that prints its arguments one per line."""
    folder.mkdir()
    fm = folder / "fm"
    fm.write_text(
        "#!/usr/bin/env python3\nimport os, shlex, sys\n"
        "argv = shlex.split(f'/bin/bash -c \"{sys.argv[-1]}\"')\nos.execvp(argv[0], argv)\n"
    )
    bench = folder / "bench"
    bench.write_text('#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done\n')
    for exe in (fm, bench):
        exe.chmod(0o755)
    return {**os.environ, "PATH": f"{folder}:{os.environ['PATH']}"}


def test_server_restore_survives_fm_quoting(ops, tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "BENCH_DIR", str(tmp_path))
    make_bench(paths.benches_dir(), "a.localhost")
    [bench] = benches.discover(compose=[])
    backup = Backup("20260101_100000", "20260101_100000-a-database.sql.gz", None, None, 1)
    step = ops.restore(bench, backup).steps[0]
    assert "<root password read on the server>" in step.display()
    script = shlex.split(_script(step.argv))[-1]  # what bash -c runs on the server
    assert 'cat "$HOME"/frappe/services/secrets/db_root_password.txt' in script
    password = "p'a\"s$s `x` \\ 100%"
    secret = tmp_path / "pw"
    secret.write_text(password)
    script = script.replace('"$HOME"/frappe/services/secrets/db_root_password.txt', shlex.quote(str(secret)))
    env = _fake_tools(tmp_path / "bin")
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=True).stdout
    args = out.splitlines()
    assert args[:4] == [
        "--site",
        "a.localhost",
        "restore",
        "sites/a.localhost/private/backups/" + backup.database,
    ]
    assert args[-2:] == ["--db-root-password", password]


def test_container_scripts_survive_fm_quoting(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "BENCH_DIR", str(tmp_path))
    env = _fake_tools(tmp_path / "bin")
    local = Operations(Settings(fm_path=str(tmp_path / "bin" / "fm")))
    step = local._bench_sh("a.localhost", 'bench --site "a.localhost" echo "two words" \'$HOME\'', "x")
    out = subprocess.run(step.argv, capture_output=True, text=True, env=env, check=True).stdout
    assert out.splitlines() == ["--site", "a.localhost", "echo", "two words", "$HOME"]

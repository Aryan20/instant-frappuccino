import shlex
import subprocess

import pytest

from fmapp.core import benches, deployer, paths, remote
from fmapp.core.hosts import LOCAL, Host
from fmapp.core.models import AppRef
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


def test_server_restore_reads_password_on_server(ops, tmp_path):
    make_bench(paths.benches_dir(), "a.localhost")
    [bench] = benches.discover(compose=[])
    from fmapp.core.models import Backup

    backup = Backup("20260101_100000", "20260101_100000-a-database.sql.gz", None, None, 1)
    restore = _script(ops.restore(bench, backup).steps[0].argv)
    assert 'cat "$HOME"/frappe/services/secrets/db_root_password.txt' in restore
    # The quoting survives a password full of shell metacharacters.
    secret = tmp_path / "pw"
    secret.write_text("p'a\"s$s `x`")
    inner = shlex.split(restore)[-1]  # bash -c <script>
    probe = (
        inner.replace('"$HOME"/frappe/services/secrets/db_root_password.txt', shlex.quote(str(secret))).split(
            " && "
        )[0]
        + ' && printf "%s" "$pw"'
    )
    quoted = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True).stdout
    assert subprocess.run(["bash", "-c", f"printf %s {quoted}"], capture_output=True, text=True).stdout == (
        "p'a\"s$s `x`"
    )

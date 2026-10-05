import subprocess

import pytest

from fmapp.core import benches, deployer, marketplace, paths
from fmapp.core.models import AppRef, SiteSpec
from fmapp.core.operations import Operations
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
    settings = Settings(
        fm_path=str(fake_bin / "fm"),
        fmd_path=str(fake_bin / "fmd"),
        github_token="ghp_secret_token",
        git_over_ssh=True,
    )
    return Operations(settings)


@pytest.mark.parametrize(
    ("repo", "ssh"),
    [
        ("erpnext", "git@github.com:frappe/erpnext.git"),
        ("org/app", "git@github.com:org/app.git"),
        ("https://github.com/org/app.git", "git@github.com:org/app.git"),
        ("https://gitlab.com/group/sub/app", "git@gitlab.com:group/sub/app.git"),
        ("ssh://git@gitlab.com/group/app.git", "git@gitlab.com:group/app.git"),
        ("git@bitbucket.org:team/app.git", "git@bitbucket.org:team/app.git"),
    ],
)
def test_ssh_url(repo, ssh):
    assert AppRef(repo).ssh_url == ssh


def test_ssh_urls_for_fm_create():
    assert AppRef.parse("git@github.com:org/app.git:main").to_fm_arg() == "git@github.com:org/app.git:main#"
    assert (
        AppRef("git@gitlab.com:g/mono.git", "dev", "apps/x").to_fm_arg()
        == "git@gitlab.com:g/mono.git:dev#apps/x"
    )
    assert AppRef("git@gitlab.com:g/app.git").to_fm_arg() == "git@gitlab.com:g/app.git"
    assert AppRef("ssh://git@gitlab.com/g/app.git", "main").to_fm_arg() == "git@gitlab.com:g/app.git:main#"
    assert AppRef("ssh://git@github.com/org/app.git").org_repo == "org/app"


def test_non_github_repos_give_fmd_the_url():
    table = AppRef("git@gitlab.com:group/app.git", "main").to_fmd_table()
    assert table == {
        "repo": "group/app",
        "repo_url": "git@gitlab.com:group/app.git",
        "exists": True,
        "ref": "main",
    }
    assert AppRef.from_fmd_table(table) == AppRef("git@gitlab.com:group/app.git", "main")
    assert AppRef("org/app").to_fmd_table() == {"repo": "org/app"}  # fmd resolves GitHub itself
    masked = {"repo": "org/app", "repo_url": "git//*****@github.com:org/app.git"}  # from a .fmd.toml
    assert AppRef.from_fmd_table(masked).repo == "org/app"


def test_ssh_mode_never_uses_the_token(ops):
    job = ops.create_site(SiteSpec("demo.localhost", apps=[AppRef("org/private", "main")]))
    assert "GITHUB_TOKEN" not in job.env  # fm tries HTTPS, then SSH with the user's keys
    assert "ghp_secret_token" in job.secrets  # still masked if anything prints it
    config = ops.deployer_config(SiteSpec("demo.localhost", apps=[AppRef("org/private")]))
    assert config["github_token"] == ""  # fmd tries SSH first when there's no token


def test_add_apps_falls_back_to_ssh_inside_container(ops):
    make_bench(paths.benches_dir(), "dev.localhost")
    [bench] = benches.discover(compose=[])
    job = ops.add_apps(bench, [AppRef("org/private", "main"), AppRef("git@gitlab.com:g/app.git")])
    _, private, gitlab, _ = job.steps
    script = bench_script(private)
    assert "x-access-token" not in script and "ghp_secret_token" not in script
    assert "git ls-remote --heads" in script and "git@github.com:org/private.git" in script
    assert "/fm-sockets/ssh-agent.sock" in script
    assert bench_script(gitlab).endswith("bench get-app git@gitlab.com:g/app.git")


@pytest.mark.parametrize(
    ("public", "url"), [(True, "https://github.com/org/private"), (False, "git@github.com:org/private.git")]
)
def test_container_clone_script_runs(ops, public, url):
    """The generated bash really picks HTTPS for public repos and SSH otherwise."""
    script = ops._get_app(AppRef("org/private", "main"))
    stubs = f'git() {{ return {0 if public else 1}; }}; bench() {{ echo "$@"; }}; '
    out = subprocess.run(["bash", "-c", stubs + script], capture_output=True, text=True, check=True).stdout
    assert out.split() == ["get-app", "--branch", "main", url]


def test_token_mode_still_embeds_and_scrubs_token(ops):
    ops.settings.git_over_ssh = False
    script = ops._get_app(AppRef("org/private"))
    assert "x-access-token:ghp_secret_token@github.com/org/private" in script
    assert "remote set-url upstream" in script


def test_ls_remote_branches(tmp_path):
    repo = tmp_path / "repo"
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    run("branch", "version-15")
    run("branch", "feature/x")
    branches = marketplace.branches_for(AppRef(f"file://{repo}"))
    assert branches == ["version-15", "main", "feature/x"]


def test_deploy_config_keeps_explicit_repo_url():
    base = {
        "apps": [
            {"repo": "org/app", "repo_url": "git@github.com:org/app.git", "exists": True, "ref": "main"},
        ]
    }
    merged = deployer.merge_config(base, "a.localhost", deployer.apps_of(base), deployer.DeployOptions())
    assert merged["apps"] == [
        {"repo": "org/app", "repo_url": "git@github.com:org/app.git", "exists": True, "ref": "main"}
    ]

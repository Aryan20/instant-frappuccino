from __future__ import annotations

from pathlib import Path

import pytest

from fmapp.core import paths


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never touch the real ~/frappe or the user's app config during tests."""
    home = tmp_path / "frappe"
    (home / "sites").mkdir(parents=True)
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for folder in (config, cache):
        folder.mkdir()
    monkeypatch.setenv("FRAPPE_MANAGER_HOME", str(home))
    monkeypatch.setattr(paths, "config_dir", lambda: config)
    monkeypatch.setattr(paths, "cache_dir", lambda: cache)
    (config / "deployer").mkdir()
    monkeypatch.setattr(paths, "deployer_configs_dir", lambda: config / "deployer")
    return tmp_path


def make_app(
    bench_root: Path, name: str, branch: str = "version-15", version: str = "15.1.0", remote: str = ""
) -> Path:
    app = bench_root / "apps" / name
    (app / name).mkdir(parents=True)
    (app / name / "__init__.py").write_text(f'__version__ = "{version}"\n')
    git = app / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    (git / "refs" / "heads" / branch).parent.mkdir(parents=True, exist_ok=True)
    (git / "refs" / "heads" / branch).write_text("0123456789abcdef0123456789abcdef01234567\n")
    remote = remote or f"https://github.com/frappe/{name}.git"
    (git / "config").write_text(
        f'[core]\n\tbare = false\n[remote "upstream"]\n\turl = {remote}\n'
        "\tfetch = +refs/heads/*:refs/remotes/upstream/*\n"
    )
    return app


def make_bench(root: Path, name: str, deployer: bool = False, **config: object) -> Path:
    bench = root / name
    bench.mkdir(parents=True)
    (bench / "docker-compose.yml").write_text("services: {}\n")
    lines = [f'name = "{name}"', 'environment_type = "dev"', "developer_mode = true"]
    lines += [
        f"{k} = {v!r}".replace("'", '"').replace("True", "true").replace("False", "false")
        for k, v in config.items()
    ]
    (bench / "bench_config.toml").write_text("\n".join(lines) + "\n")
    workspace = bench / "workspace"
    if deployer:
        release = workspace / "release_20260101_120000"
        older = workspace / "release_20251201_090000"
        for r in (release, older):
            (r / "apps").mkdir(parents=True)
        (workspace / "deployment-data").mkdir()
        (workspace / "frappe-bench").symlink_to(release.name)
        bench_root = release
    else:
        bench_root = workspace / "frappe-bench"
        (bench_root / "sites").mkdir(parents=True)
        (bench_root / "sites" / "apps.txt").write_text("frappe\nerpnext\n")
    make_app(bench_root, "frappe", version="15.40.0")
    make_app(bench_root, "erpnext")
    return bench

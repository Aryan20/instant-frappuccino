import pytest

from fmapp.core.models import AppRef


@pytest.mark.parametrize(
    ("text", "repo", "ref", "subdir"),
    [
        ("erpnext", "erpnext", "", ""),
        ("erpnext:version-15", "erpnext", "version-15", ""),
        ("frappe/hrms:version-15", "frappe/hrms", "version-15", ""),
        ("myorg/app", "myorg/app", "", ""),
        ("https://github.com/org/app", "https://github.com/org/app", "", ""),
        ("https://github.com/org/app:main", "https://github.com/org/app", "main", ""),
        ("https://git.example.com:8443/org/app.git", "https://git.example.com:8443/org/app.git", "", ""),
        ("git@github.com:org/app", "git@github.com:org/app", "", ""),
        ("git@github.com:org/app:develop", "git@github.com:org/app", "develop", ""),
        ("frappe/payments:version-15#apps/payments", "frappe/payments", "version-15", "apps/payments"),
    ],
)
def test_parse(text, repo, ref, subdir):
    parsed = AppRef.parse(text)
    assert (parsed.repo, parsed.ref, parsed.subdir) == (repo, ref, subdir)


@pytest.mark.parametrize("text", ["", "   ", "org/app; rm -rf /", "org/$(whoami)"])
def test_parse_rejects(text):
    with pytest.raises(ValueError):
        AppRef.parse(text)


def test_org_repo_and_names():
    assert AppRef("erpnext").org_repo == "frappe/erpnext"
    assert AppRef("https://github.com/The-Commit-Company/Raven.git").org_repo == "The-Commit-Company/Raven"
    assert AppRef("https://gitlab.com/org/app").org_repo is None
    assert AppRef("resilient-tech/india-compliance").name_guess == "india_compliance"
    assert AppRef("org/mono", subdir="apps/my-app").name_guess == "my_app"


def test_fm_and_fmd_formats():
    ref = AppRef("hrms", "version-15")
    assert ref.to_fm_arg() == "frappe/hrms:version-15"
    assert ref.to_fmd_table() == {"repo": "frappe/hrms", "ref": "version-15"}
    mono = AppRef("org/mono", "main", "apps/x")
    assert mono.to_fm_arg() == "org/mono:main#apps/x"
    assert mono.to_fmd_table() == {
        "repo": "org/mono",
        "ref": "main",
        "subdir_path": "apps/x",
        "symlink": True,
    }
    assert AppRef.from_fmd_table(mono.to_fmd_table()) == mono
    url = AppRef("https://gitlab.com/org/app", "main")
    assert url.to_fm_arg() == "https://gitlab.com/org/app:main"

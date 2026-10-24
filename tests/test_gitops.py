"""GitOps guards (docs/DESIGN.md, GitOps with Argo CD).

With Argo CD, a merged change to deploy/envs/<env>.yaml is a deploy, so the checks that
deploy.sh makes before a deploy must happen in CI, on the files themselves:
  - every digest in an env file has release provenance (integration: real registry)
  - prod only names a digest that staging has run (git history of staging.yaml)
  - the two Argo CD Applications differ only in their environment
and scripts/promote.sh and deploy.sh behave as the runbook says.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ENVS = ("staging", "prod")
DIGEST_LINE = re.compile(r'^  digest: "(sha256:[0-9a-f]{64})"', re.M)


def _env_digest(env: str, text: str | None = None) -> str:
    text = text if text is not None else (ROOT / f"deploy/envs/{env}.yaml").read_text()
    match = DIGEST_LINE.search(text)
    assert match, f"deploy/envs/{env}.yaml has no image.digest line (promote.sh rewrites it)"
    return match[1]


@pytest.mark.parametrize("env", ENVS)
def test_env_files_name_a_release_digest_and_nothing_else(env: str) -> None:
    values = yaml.safe_load((ROOT / f"deploy/envs/{env}.yaml").read_text())
    image = values["image"]
    assert image["digest"] == _env_digest(env)
    # The repository comes from the chart default; a tag would be mutable, so never here.
    assert set(image) == {"digest"}, f"{env}: only image.digest belongs in an env file"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def test_prod_runs_only_what_staging_ran() -> None:
    """Prod may name a digest only if staging ran it on main BEFORE this change.

    In a pull request, CI checks out a merge commit: its first parent is main as it was, so
    a PR that sets staging and prod to a new digest at once is refused. Promote staging,
    merge, then prod.
    """
    if _git("rev-parse", "--is-shallow-repository").strip() == "true":
        pytest.fail("needs the full git history (CI: actions/checkout fetch-depth: 0)")
    merge = len(_git("rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    base = "HEAD^1" if merge else "HEAD"
    prod = _env_digest("prod")
    base_prod = DIGEST_LINE.search(_git("show", f"{base}:deploy/envs/prod.yaml"))
    if base_prod and base_prod[1] == prod:
        return  # prod unchanged by this change
    history = _git("log", "-p", "--format=", base, "--", "deploy/envs/staging.yaml")
    if 'digest: "sha256:' not in history:
        return  # the change that introduced digests in env files (GitOps): nothing to compare
    assert f'digest: "{prod}"' in history, (
        f"prod names {prod}, which staging has not run on main: promote it to staging first"
    )


def test_applications_differ_only_by_environment() -> None:
    apps = list(yaml.safe_load_all((ROOT / "deploy/argocd/applications.yaml").read_text()))
    assert [a["metadata"]["name"] for a in apps] == [f"shortly-{env}" for env in ENVS]
    normalised = []
    for app, env in zip(apps, ENVS, strict=True):
        spec = app["spec"]
        assert spec["destination"]["namespace"] == env
        assert spec["source"]["helm"]["valueFiles"] == [f"../../envs/{env}.yaml"]
        assert spec["source"]["targetRevision"] == "main"
        assert spec["syncPolicy"]["automated"] == {"prune": True, "selfHeal": True}
        text = yaml.safe_dump(app).replace(env, "ENV")
        normalised.append(text)
    assert normalised[0] == normalised[1]


# ------------------------------------------------------------------- promote.sh, with stubs
STAGED = "sha256:" + "b" * 64
NEW = "sha256:" + "c" * 64


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo shaped like this one, with origin/main, and a stub verifier."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    (work / "scripts").mkdir(parents=True)
    (work / "deploy/envs").mkdir(parents=True)
    shutil.copy(ROOT / "scripts/promote.sh", work / "scripts/promote.sh")
    for env in ENVS:
        (work / f"deploy/envs/{env}.yaml").write_text(
            f'# {env}\nimage:\n  digest: "{STAGED}" # v1.0.0\nreplicaCount: 1\n'
        )
    verify = tmp_path / "verify"
    verify.write_text(
        '#!/usr/bin/env bash\n[[ "$1" == *cccc* ]] || { echo refused >&2; exit 1; }\n'
        "echo 'verified: built by https://github.com/statsguy/shortly/.github/workflows/"
        "release.yml@refs/tags/v1.1.0'\n"
    )
    verify.chmod(0o755)
    git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "remote", "add", "origin", str(origin)], check=True)
    subprocess.run([*git, "push", "-q", "origin", "main"], check=True)
    return work


def _promote(repo: Path, env: str, ref: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "scripts/promote.sh"), env, ref],
        capture_output=True,
        text=True,
        env={**os.environ, "VERIFY": str(repo.parent / "verify")},
        check=False,
    )


def test_promote_writes_a_verified_digest_with_its_version(repo: Path) -> None:
    result = _promote(repo, "staging", f"ghcr.io/statsguy/shortly@{NEW}")
    assert result.returncode == 0, result.stderr
    text = (repo / "deploy/envs/staging.yaml").read_text()
    assert f'  digest: "{NEW}" # v1.1.0\n' in text
    assert "replicaCount: 1" in text  # nothing else touched


def test_promote_refuses_a_digest_without_provenance(repo: Path) -> None:
    result = _promote(repo, "staging", "sha256:" + "d" * 64)
    assert result.returncode != 0
    assert _env_digest("staging", (repo / "deploy/envs/staging.yaml").read_text()) == STAGED


def test_promote_refuses_prod_before_staging_ran_it(repo: Path) -> None:
    result = _promote(repo, "prod", NEW)
    assert result.returncode == 1
    assert "staging has not run" in result.stderr
    assert _env_digest("prod", (repo / "deploy/envs/prod.yaml").read_text()) == STAGED


def test_promote_allows_prod_once_staging_ran_it_on_main(repo: Path) -> None:
    assert _promote(repo, "staging", NEW).returncode == 0
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-qam", "deploy staging"], check=True)
    subprocess.run([*git, "push", "-q", "origin", "main"], check=True)
    result = _promote(repo, "prod", NEW)
    assert result.returncode == 0, result.stderr
    assert _env_digest("prod", (repo / "deploy/envs/prod.yaml").read_text()) == NEW


def test_promote_allows_prod_with_a_long_staging_history(repo: Path) -> None:
    """Regression: `git log -p | grep -q` under pipefail refused a digest staging HAD run.

    grep -q stops at the first match (the newest promotion); git log, still writing older
    history, dies of SIGPIPE and fails the pipeline. Only shows once the history is bigger
    than the pipe buffer, as on the real main by v0.7.1.
    """
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    staging = repo / "deploy/envs/staging.yaml"
    staging.write_text(staging.read_text() + "".join(f"# {'x' * 70} {i}\n" for i in range(4000)))
    subprocess.run([*git, "commit", "-qam", "a large old change"], check=True)
    assert _promote(repo, "staging", NEW).returncode == 0
    subprocess.run([*git, "commit", "-qam", "deploy staging"], check=True)
    subprocess.run([*git, "push", "-q", "origin", "main"], check=True)
    result = _promote(repo, "prod", NEW)
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------------- deploy.sh vs Argo CD
def test_deploy_refuses_an_environment_argo_cd_manages(tmp_path: Path) -> None:
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    bin_dir.mkdir()
    stubs = {
        # `kubectl -n argocd get application shortly-prod -o name` finds the Application.
        "kubectl": 'case "$*" in *"get application"*) echo application.argoproj.io/x ;; '
        '"config current-context") echo kind-shortly ;; esac',
        "helm": "",
        "kind": "",
        "docker": "exit 1",
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{log}"\n{body}\n')
        path.chmod(0o755)
    result = subprocess.run(
        [str(ROOT / "scripts/deploy.sh"), "prod", "shortly:dev"],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=False,
    )
    assert result.returncode == 1
    assert "deployed by Argo CD from git" in result.stderr
    assert "helm " not in log.read_text()


# ------------------------------------------------------------------- real provenance
@pytest.mark.integration
@pytest.mark.parametrize("env", ENVS)
def test_env_digests_have_release_provenance(env: str) -> None:
    if shutil.which("gh") is None:
        if os.environ.get("CI"):
            pytest.fail("gh is required in CI")
        pytest.skip("gh not installed")
    ref = f"ghcr.io/statsguy/shortly@{_env_digest(env)}"
    result = subprocess.run(
        [str(ROOT / "scripts/verify-image.sh"), ref], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr

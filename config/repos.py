"""Configurable checkout targets for the repos this integration test spans -- the
single source of truth for each repo's default URL/ref.

Read by scripts/checkout_repos.py. Override any field by exporting the matching
environment variable before running the script -- in CI, each *_REPO_REF is wired to
a workflow_dispatch input (core_repo_ref / signer_repo_ref / seeder_repo_ref) via a
job-level `env:` entry in .github/workflows/weekly-integration-test.yml, which passes
the raw input through with no literal fallback of its own -- an unset/blank input
(schedule/pull_request/push, or a manual dispatch left blank) resolves to the
defaults below, not a second copy of them in the workflow file.
"""
import os


class RepoTarget:
    """One upstream repo's checkout target: URL + ref, both env-overridable."""

    def __init__(self, name, env_prefix, default_url, default_ref):
        self.name = name
        # `or default` (not os.environ.get's own default=) so an env var present but
        # set to an empty string -- as CI sets it for a blank workflow_dispatch input
        # -- still falls back, not just an entirely absent/unset one.
        self.url = os.environ.get(f"{env_prefix}_REPO_URL") or default_url
        self.ref = os.environ.get(f"{env_prefix}_REPO_REF") or default_ref


class ReposConfig:
    """Default checkout targets for all three repos this test spans."""

    def __init__(self):
        self.signer = RepoTarget(
            "tapyrus-signer", "SIGNER",
            "https://github.com/chaintope/tapyrus-signer.git",
            "master",
        )
        self.core = RepoTarget(
            "tapyrus-core", "CORE",
            "https://github.com/chaintope/tapyrus-core.git",
            "master",
        )
        self.seeder = RepoTarget(
            "tapyrus-seeder", "SEEDER",
            "https://github.com/chaintope/tapyrus-seeder.git",
            "master",
        )

    def __iter__(self):
        return iter((self.signer, self.core, self.seeder))

"""Configurable checkout targets for the repos this integration test spans.

Read by scripts/checkout_repos.py. Override any field by exporting the matching
environment variable before running the script -- in CI, each *_REPO_REF is wired to
a workflow_dispatch input (core_repo_ref / signer_repo_ref / seeder_repo_ref) via a
job-level `env:` entry in .github/workflows/weekly-integration-test.yml.
"""
import os


class RepoTarget:
    """One upstream repo's checkout target: URL + ref, both env-overridable."""

    def __init__(self, name, env_prefix, default_url, default_ref):
        self.name = name
        self.url = os.environ.get(f"{env_prefix}_REPO_URL", default_url)
        self.ref = os.environ.get(f"{env_prefix}_REPO_REF", default_ref)


class ReposConfig:
    """Default checkout targets for all three repos this test spans."""

    def __init__(self):
        # Only needed for federation-change/rotation support; also doesn't build out
        # of the box. See doc/work-done.md.
        self.signer = RepoTarget(
            "tapyrus-signer", "SIGNER",
            "https://github.com/Naviabheeman/tapyrus-signer.git",
            "163_federationChangeTomlSetup",
        )
        self.core = RepoTarget(
            "tapyrus-core", "CORE",
            "https://github.com/chaintope/tapyrus-core.git",
            "master",
        )
        # master lacks four bug fixes that only exist on a local, unpushed branch.
        # See doc/work-done.md.
        self.seeder = RepoTarget(
            "tapyrus-seeder", "SEEDER",
            "https://github.com/chaintope/tapyrus-seeder.git",
            "master",
        )

    def __iter__(self):
        return iter((self.signer, self.core, self.seeder))

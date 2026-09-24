# Releasing

Releases are cut by pushing a tag. Nothing is published from a laptop.

```bash
# 1. bump __version__ in agentdynamics/__init__.py and `version` in pyproject.toml
# 2. move CHANGELOG "Unreleased" under the new version
# 3. merge to main with CI green
git tag v0.4.0 && git push origin v0.4.0
```

`.github/workflows/release.yml` then:

1. checks that the tag equals the package version (in both places) and that `CHANGELOG.md` has an entry for it.
   That entry becomes the GitHub Release notes.
2. builds the sdist and wheel, runs `twine check`, and smoke-tests the **installed wheel** outside the source
   tree: the web assets and bootstrap are present, `serve` starts, and `doctor` passes against it;
3. runs the full test suite;
4. signs build provenance;
5. publishes to PyPI via trusted publishing;
6. creates the GitHub Release with the artifacts;
7. pushes `ghcr.io/aditya31398/agentdynamics:{X.Y.Z, X.Y, latest}` (amd64 + arm64) with an SBOM and provenance.

## One-time setup (repository owner)

These need an account holder and can't be done from CI.

**PyPI trusted publisher.** On https://pypi.org/manage/account/publishing/ add a *pending* publisher:

| Field | Value |
|---|---|
| PyPI project name | `agentdynamics` |
| Owner | `Aditya31398` |
| Repository | `agentdynamics` |
| Workflow | `release.yml` |
| Environment | `pypi` |

**GitHub environment.** Settings → Environments → New environment `pypi`. Adding yourself as a required reviewer
makes every PyPI publish wait for a click, which is recommended.

**Container visibility.** After the first release, set the `agentdynamics` package on GHCR to public.

**Optional: live API smoke test.** Add an `ANTHROPIC_API_KEY` repository secret to enable the nightly
`live` CI job (it spends a fraction of a cent per run). Without it the job skips.

## Aegis

The governance tests need `aegis.observe` and `Kernel.reserve_spend`, so `ci.yml` and `release.yml` install
`aegis-kernel>=0.4.0` from PyPI. The distribution is **`aegis-kernel`** (the import name is still `aegis`);
it was called `aegis-guard` before 0.4.0 and that name was never published, so nothing should reference it.

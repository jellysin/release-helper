# Contributing

Develop on a feature branch from main and submit a pull request with a Conventional
Commit title. Squash merging keeps release notes attributable to reviewed changes.
Required checks are strict: a pull request merges only when its branch contains the
current base.

## Validate locally

Use the Python version pinned in `.python-version`; CI and the composite actions read
that file. The reusable workflow reads it from its own commit and refuses any other
`python3` before its standard-library programs run. Production code supports Python
3.14 and newer, the minor CI tests, and needs no pip installation. Development dependencies are pinned and
hash checked. `make bootstrap` installs them into `.venv` with `--require-hashes`; `make verify-all`
uses that environment and fails, naming `make bootstrap`, when `.venv` is missing or older than
`requirements-dev.txt`. The commands it runs, with `.venv/bin/python` as `python`:

```sh
make bootstrap
python -m ruff check .
python -m ruff format --check .
python -m coverage run -m unittest discover -s tests
python -m coverage report
python tools.py check-policy
actionlint
```

`make verify-all` runs these commands and the governance checks (agent context, text
register, audit). The governance targets run the Praetor engine that `PRAETOR_REF` in
`.github/workflows/ci.yml` pins; `.config/lefthook/engine.sh` installs it with Go on
first use, and the Git hooks call the same launcher.

The tests need `actionlint` on `PATH` and fail without it. `.github/actionlint.yaml`
holds the one workflow-lint exception: `actionlint` 1.7.12 does not know the documented
`job.workflow_repository` and `job.workflow_sha` values (rhysd/actionlint#707).
`tests/test_workflow_lint_exceptions.py` fails after 2027-01-09 and as soon as
`actionlint` accepts `release-please.yml` without the entry; remove the file then.

Coverage must reach 85%, including branches. Tests use two plugin identities and
temporary directories; they do not publish releases. Record unavailable validation.

The unit tests of `server-check` run against a stand-in server and a stand-in container
runtime and start no container. The proof on a real Jellyfin server is two steps of the
`CI` job: the `floor` fixture must pass, and the `wrong-abi` fixture must fail with
exit status 1 and its `does not offer` line. A change of `SERVER_IMAGE` is reviewed
like a dependency update, tag and digest together; those two steps run it before merge.

`pyproject.toml` declares supported runtimes (`>=3.14`, the minor of the pin; tests/test_python_pin.py holds the two equal); `.python-version` pins the
exact CI interpreter. Renovate's [PEP 621 manager](https://docs.renovatebot.com/modules/manager/pep621/#dependency-types)
identifies the former as `requires-python`. Its scoped
[`widen` strategy](https://docs.renovatebot.com/modules/versioning/pep440/#rangesconstraints)
keeps that compatibility range rather than pinning it to a single Python patch.
Exact CI interpreter and dependency updates remain enabled and require PR review.
Raise the supported minimum deliberately when production code requires it.

`JELLYFIN_ABI_MAJOR` in `jellysin_tooling/common.py` is the only place that names the
supported Jellyfin major. A new major changes that constant, the fixtures and the
documentation in one pull request; no validator carries its own copy. The floor rule
(server references equal to the release in `targetAbi`) has one implementation,
`inventory.require_floor`; a plugin that needs a looser rule asks for a named option
of that check and keeps no private variant.

## Release contract

Release-please collects conventional commits in release PRs and maintains proposed
versions and changelogs. The shared workflow sets `skip-github-release: true` and
dispatches only the caller's `ci.yml` for returned release PR branches. A main merge
can open or update a release PR; it cannot create a tag, draft or published release.
Merging the release PR updates version files and changelogs without publication.
The reusable workflow has no publication inputs or tag output.

A merged release PR keeps its `autorelease: pending` label until its version tag
exists, and release-please opens no further release PR while it does. The first step
of the reusable workflow moves the label to `autorelease: tagged` when the tag
`v<version>` names the PR's merge commit; never move it by hand for a tag that sits
elsewhere. The step's program is tested from the workflow file itself
(`tests/test_release_pr_workflow.py`); a change to it needs the case it refuses.

Publication is a separate, explicitly authorized operation. Tags use stable
three-part SemVer. Consumers pin Actions or reusable workflows to a full commit SHA
and annotate released versions for Renovate. The shared PR workflow does not
create those tags or dispatch publication workflows.

Preparation is the step between: `prepare`, dispatched manually from
`prepare-release.yml` on `main`, creates the exact tag on an approved main commit and
one unpublished draft. It checks tag, commit, version file and remote state before the
first write, creates only what is missing, and leaves an existing draft untouched. It
takes the created draft from the answer of its own write, since the release listing can
lag behind a creation by seconds. A
published or immutable release with the tag, or the tag on another commit, is a
conflict that needs a new version. Every write of the command has a test that proves
the refusal happens first; a new write needs the same proof.

This repository has one release writer: the merge train. When a person merges the
release PR, the train's release-please run creates the tag on the merge commit and
publishes the release, since `release-please-config.json` does not set
`skip-github-release`. `prepare-release.yml` stays as the fallback for a version the
train left untagged: dispatched on `main` with the tag and the merged release commit,
it passes `skip-tagged: 'true'`, so a tag that already names the commit ends the run
without a write, and a tag on another commit is refused. Only on an untagged version
does it create the tag and a draft whose text is the version's changelog section;
publishing that draft is the maintainer's own act: read the draft, set `draft` to
false on its numeric release ID, and read back that the release is published and
immutable. The publisher does not apply here, since the helper ships no plugin
artifacts. Tags `v1.0.0` to
`v1.0.3` mark the commits that recorded those versions; no release was published for
them.

When publication is authorized, the publisher requires `release.yml` to run **on
the exact tag ref** with an existing draft. Checking out a tag from a main-triggered
workflow does not change the OIDC source identity. Publication validates the exact
tag commit, attests every artifact, verifies existing bytes, uploads missing draft
assets, rechecks the complete draft, then publishes. Retry on the same tag. A
mismatch needs a new version; an incomplete published release is never repaired.
Only expected zero-byte `starter` assets in a freshly revalidated draft may be
deleted during failed-upload recovery. A changed or uploaded asset stops recovery.

If an original tag already has verified attestations but its publisher cannot
finish, a reviewed `recover-release.yml` may opt into `verified-tag-recovery`.
Dispatch that workflow from `main`; retain the actual GitHub context variables and
check out the exact original tag with full history and fetched `origin/main`.
Its source must be an ancestor of both the workflow's immutable commit and
`origin/main`. Rebuild reproducibly and verify the original artifact attestations;
do not re-attest recovery builds or change the tag. Grant only repository-scoped
contents writes for publication, with no attestation or OIDC write permissions.
The publisher independently enforces this context, all original provenance checks,
and numeric release identity. A vanished draft requires investigation, not a
replacement selected by tag.

Use only the calling repository's GITHUB_TOKEN. Plugin repository updates run in
`jellysin/repo`, which opens its own PR. No cross-repository write token, release
App, runtime package, or installed plugin dependency is required.

Workflow defaults are read-only. Writes belong to the narrow jobs that need them.
Bot CI explicitly dispatches the PR branch. No workflow assumes a token-created
event will recursively trigger another workflow.
GitHub may also create an approval-required PR run for each bot-authored update.
Review the current PR and approve that real run before merging; a successful
dispatch does not replace its approval. Do not fabricate checks or weaken protection.

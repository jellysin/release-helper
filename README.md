# JellySin Release Helper

<!-- praetor:readme-governance:start -->
[![HISS Adopted][praetor-hiss-badge]][praetor-hiss-agents]

Praetor manages this repository's declared governance policy. This managed
block records adoption state; it is not a verification certificate.

**Verification**: `make verify-all` runs the repository's configured
verification cascade.

**HISS Audit**: `praetorctl audit` enforces policy, generated-surface
integrity, and the debt ratchet.

**Context Sync**: `praetorctl compile-context --verify` verifies every
generated agent context against `AGENTS.md`.

**Debt Baseline**: `.standards-baseline.json` anchors the debt ratchet at
0 recorded infractions; audit forbids growth.

[praetor-hiss-badge]: https://img.shields.io/badge/Standards-HISS%20Adopted-blue
[praetor-hiss-agents]: https://github.com/jellysin/release-helper/blob/HEAD/AGENTS.md
<!-- praetor:readme-governance:end -->

Shared, versioned build and release tools for independent Jellyfin 12 plugins.
Python's standard library handles packaging and plugin repository validation; GitHub CLI
verifies signed provenance and performs repository-scoped publication. None of
this tooling is loaded by an installed plugin.

## Plugin descriptor

Each plugin supplies `plugin.json`, a three-part `version.txt`, and a directory of
build outputs. The descriptor records `guid`, `name`, `description`, `overview`,
`owner`, `category`, `targetAbi`, `assemblyFile`, and `repository`. Optional
`packageFiles` names additional plugin-owned files explicitly. Paths are flat,
unique and bounded; host-provided assembly names are rejected.

`targetAbi` is the floor: the oldest Jellyfin server the plugin supports, written
`12.x.y.0`. The supported major is one constant, `JELLYFIN_ABI_MAJOR` in
`jellysin_tooling/common.py`; descriptor and plugin repository validation both read it.

Optional `imageUrl` names the image Jellyfin shows for the plugin. Accepted form:
`https://raw.githubusercontent.com/<repository>/<40-hex commit>/<path>` inside the
plugin's own repository, ending in `.png`, `.jpg`, `.jpeg` or `.webp`. Branch names,
other hosts and other repositories are rejected. The plugin repository copies the
value when it first lists the plugin and checks it against the release source.

```sh
python tools.py package --publish-directory artifacts/plugin --tag v1.0.0
```

`--changelog-path CHANGELOG.md` adds the release notes: the section of the packaged
version in a release-please changelog becomes `release.json.changelog` and, after
import, the `changelog` text Jellyfin renders as Markdown. Notes are at most 4096
characters of text without control characters; a longer section is cut at a line
end and closes with a link to the release page. The result depends on the file
contents only. A tagged package fails when the section is absent; an untagged check
omits the field. Without the option the plugin repository entry links the release
page, as every earlier entry does. Listed entries never change.

Output: plugin ZIP, `release.json`, `checksums.txt` (SHA-256), and an SPDX 2.3
inventory. MD5 serves Jellyfin's plugin repository format. SPDX's required SHA-1 file
checksums and package verification code accompany SHA-256; security verification
uses SHA-256 and attestations. Shipped files and external dependencies are distinct.
Unverified third-party licenses remain `NOASSERTION`.

ZIP order, permissions, timestamp and storage are deterministic. Stored compression
avoids deflate runtime differences. The plugin DLL must itself be built
deterministically. Limits: 32 files, 64 MiB per package, 4 MiB JSON, 90-second downloads.

## Production dependency inventory

For .NET production builds, supply `nuget-lock-path`, `nuget-assets-path` and
`runtime-dependencies-path` together; `global-json-path` defaults to `global.json`.
The CLI uses the same names prefixed by `--`. Runtime declarations have this form:

```json
{
  "schemaVersion": 1,
  "targetFramework": "net10.0",
  "runtimeDependencies": [
    {"id": "Jellyfin.Controller", "version": "12.0.0", "providedBy": "jellyfin"}
  ]
}
```

List the host NuGet packages the plugin explicitly needs at runtime. Every entry
must match the production lock's exact resolved version. The tooling compares
the complete target-framework lock graph with restored assets: package identities,
resolved versions, SHA-512 package hashes and dependency edges must agree. Perform
`dotnet restore --locked-mode` first; metadata comparison supplements that check.
No package scripts or assemblies are executed. NuGet assets formats 3 and 4 are
supported, with at most 512 packages per inventory.

Server packages stay at the floor. Every host-provided `Jellyfin.*` package must have
exactly the `x.y.z` version that `targetAbi` names, and `Jellyfin.Controller` must be
among them. A reference above the floor is disabled as incompatible by the floor
server; a floor above the reference claims a server release that no build was compiled
against. A direct `Jellyfin.*` reference in the production lock must be declared in
the runtime list, so that it cannot escape the comparison. `package` refuses a
mismatch before it writes an artifact, and the plugin repository refuses a release
whose metadata carries one. Raising the floor changes `targetAbi`, the references and
the lock in one pull request.

The SBOM lists every locked production package as a build dependency, with NuGet
PURLs and archive hashes. Declared host packages also have runtime relationships;
their DLLs are not included. It records the SDK pin and a SHA-256 fingerprint of
canonical lock JSON, so checkout paths and line endings do not alter the inventory.
The same graph is stored in optional `release.json.dependencyInventory` and is
checked against the SBOM by both publisher and plugin repository. Without these inputs the
document covers shipped files only. SDK internals, the installed host's full
dependency tree and frontend development tools are outside this production graph.

## GitHub Actions

Consume `actions/package`, `actions/prepare`, `actions/publish`, `actions/catalog`, or
`actions/server-check` at a reviewed full commit SHA. Actions use their own downloaded directory; callers do not copy
scripts or check out a mutable tooling branch.

The reusable `.github/workflows/release-please.yml` collects conventional commits
into release PRs, including proposed versions and changelogs. It sets
`skip-github-release: true`: main merges can prepare PRs but never create tags,
GitHub drafts or published releases. Merging a release PR updates files only.
It explicitly dispatches the caller's `ci.yml` for at most 20 returned release PRs,
with a 60-second timeout per dispatch. It exposes no publication inputs or tag
output and never dispatches a publication workflow.

Its programs run on the interpreter `.python-version` names, not on the one the runner image
ships. The job reads that file from the helper commit that defines the workflow
(`job.workflow_repository` at `job.workflow_sha`) and refuses any other `python3`
(`tests/test_release_pr_workflow.py`). The caller's workflow token must be able to read the
helper repository, and GitHub Enterprise Server does not provide these two values.

Release-please stops while a merged release PR still carries `autorelease: pending`,
and with `skip-github-release` nothing moves that label. The workflow's first step
therefore marks a merged release PR `autorelease: tagged` once the tag `v<version>`
from its title names its merge commit. A tag that is absent or names another commit
leaves the label, and release-please stops until the release is prepared. The step
reads at most 19 pending PRs (a full page of 20 fails), allows 60 seconds per request
and adds the new label before it removes the old one.

The package action accepts `publish-directory`, `metadata-path`, `version-path`,
`output-directory`, optional exact `tag`, optional `changelog-path` and the dependency
inputs above; it outputs `archive` and `directory`.
For a separately authorized publication, attest **all four artifacts** using
`actions/attest` in the plugin's tag-dispatched
`.github/workflows/release.yml`, then call `actions/publish` with `directory`, `tag`
and the built-in `token`. Optional input `signer-workflow` defaults to
`.github/workflows/release.yml`; allowed values are `.github/workflows/release.yml`
and `.github/workflows/build-signed.yml`. An existing draft and the exact tag are prerequisites;
release PR preparation creates neither, `actions/prepare` creates both. GitHub currently requires maintainer approval of
`GITHUB_TOKEN`-created PR workflows, including later updates; dispatching CI does
not clear that separate approval gate. This is the documented
[June 11, 2026 GitHub behavior](https://github.blog/changelog/2026-06-11-bot-created-pull-requests-can-run-workflows-if-approved/).
Maintainers approve the actual PR workflow after reviewing the current changes.
The automation retains only the built-in token; unattended PR merging is not promised.

## Release preparation

`prepare` creates the two prerequisites of publication, the exact tag and one
unpublished draft. It never publishes, and it never moves, edits or deletes anything.

```sh
python tools.py prepare --repo jellysin/plugin-lastfm --tag v1.0.0 --commit <full SHA>
```

The commit must be an ancestor of fetched `origin/main`. Its version file
(`--version-path`, default `version.txt`) is read from the commit itself, not from
the checkout, and must record the version in the tag. `--changelog-path CHANGELOG.md`
takes the draft text from that version's section of the changelog at the same commit:
plain text of at most 65,536 characters; a missing or longer section stops the command.

The command reads the remote state before its first write and refuses a conflict:

- a published, immutable or prerelease release that carries the tag;
- more than one draft for the tag;
- the tag on another commit;
- a tag listing that is malformed or fills a page, since the exact tag may then be hidden.

It then creates only what is missing, the tag first and the draft second, and reads
both back. A matching tag and draft need no write, so a repeated run changes nothing
and an existing draft keeps its text. After a failure, run the same command again:
a tag without a draft, or a draft without a tag, is completed.

`--skip-tagged` (action input `skip-tagged: 'true'`) is for a repository whose
release another writer creates, such as the merge train. A tag that already names the
commit then ends the command without a read of the releases and without a write; a tag
on another commit is still refused, and an untagged version is prepared as above.

Recipe for a plugin repository:

1. Add `.github/workflows/prepare-release.yml` with `workflow_dispatch` inputs `tag`
   and `commit`. In Actions the command accepts this workflow file only, dispatched
   manually on `main`, and a commit that is an ancestor of the workflow's own commit.
2. Give the job `contents: write` and check out with `fetch-depth: 0`.
3. Call `actions/prepare` with `tag`, `commit` and the built-in `token`; add
   `changelog-path` for release notes in the draft.
4. Dispatch `release.yml` on the new tag ref. A tag created with the built-in token
   starts no workflow by itself.

The draft request names no target commit: the tag exists by then, and GitHub refuses
the built-in token a named target whose workflow files differ from the default branch
([create a release](https://docs.github.com/en/rest/releases/releases#create-a-release)).
When GitHub refuses the tag creation itself, a maintainer pushes the tag to the
approved commit and runs the command again; it then only adds the draft.

This repository keeps the same workflow file, with the action from its own checkout
and `skip-tagged: 'true'`, as the fallback behind the merge train; CONTRIBUTING.md
describes the steps.

## Server check

`server-check` answers one question about a plugin repository file: does a real
Jellyfin server offer every entry in it? Validation of the file's form cannot answer
that. A server drops each version whose `targetAbi` is above its own version, and an
unreadable file gives an empty list without an error.

```sh
python tools.py server-check --manifest manifest.json
```

The command validates the file, starts one throwaway server container, hands it a
copy of the file and reads back what the server offers. It needs Docker and nothing
else; it takes no token and never writes the file. Each run prints the image digest
that ran and the version the server reported. A refusal names every withheld entry
(at most 20 lines) with identity, version and `targetAbi`, and the command exits 1.

Two built-in control entries run beside the file in every check. One is compatible
with every supported server and must be offered; the others name an ABI above every
server and must be withheld. A server that offers nothing, or that does not filter,
therefore fails the check instead of passing an empty comparison. An empty file
passes with `0 expected entries` once the controls passed.

The default server is the reviewed image in `SERVER_IMAGE`
(`jellysin_tooling/servercheck.py`), tag and digest together; Renovate proposes
updates. `--image` selects another server, for example the floor a plugin supports.
Accepted form: `jellyfin/jellyfin[:tag]@sha256:<digest>`. The digest decides what
runs, and the command refuses a container that runs another one.

`--emit-fixture <kind> <new path>` writes a valid plugin repository file for a
caller's own proof and never replaces a file:

- `wrong-abi`: one entry above every server; the check must refuse it.
- `floor`: one entry at the floor; every supported server offers it.

A consumer proves its job in both directions: the check passes on its real file and
fails on the `wrong-abi` fixture with the `does not offer` line in the output. An exit
status alone does not show which refusal happened.

`actions/server-check` runs the same command with the inputs `manifest` and `image`.

Bounds: 120 seconds until the server answers, 20 seconds per network step and 60
seconds per answer, 4 MiB per answer, 10 minutes for the image pull, 1536 MiB and two CPUs for the container. The
container has no volume, its port is published on the loopback address only, and it
is stopped and removed on success, failure and termination. The command speaks plain
HTTP to that one loopback port and to no other host; the server reads the files from
its own web directory, so no file leaves the machine.

## Plugin repository trust and recovery

Only allowlisted public releases are accepted. Every new artifact needs valid
signatures bound to the repository, approved workflow, source SHA, exact tag and
GitHub-hosted runner. In `plugins.json`, each entry sets `signerWorkflow` to an
approved workflow (`.github/workflows/release.yml` or
`.github/workflows/build-signed.yml`). Metadata, checksums, ZIP entries and the
complete expected SPDX identity, file inventory and dependency relationships are
validated before the plugin repository changes. Existing plugin repository versions
are preserved.

Scans are bounded to 32 repositories, 1,000 releases per repository, and 20 new
versions per batch; remaining releases follow after that plugin repository PR merges.
Drafts and prereleases are skipped. A published release without metadata, checksums or
SBOM is skipped too, with a `Catalog import skipped` warning that names its repository and
tag in the run summary; every other release still imports. Failed or over-limit scans
leave the plugin repository unchanged; resolve the source problem and rerun. Already listed
versions are not re-downloaded each poll; source repositories must enable immutable
releases. Plugin repository changes go through PRs.

Publication can resume missing draft uploads. GitHub may leave an empty `starter`
asset after an upload failure; recovery removes it only when its name is expected,
its size is exactly zero, existing uploaded bytes match, and a fresh API read still
identifies the same draft asset. Uploaded or published assets are never replaced.
See GitHub's [upload failure behavior](https://docs.github.com/en/rest/releases/assets#upload-a-release-asset).

The publisher discovers drafts through the authenticated
[release list](https://docs.github.com/en/rest/releases/releases#list-releases):
GitHub's tag endpoint returns published releases only. It scans at most 1,000
entries, requires one exact tag match, and pins subsequent reads, uploads and
publication to that release's numeric ID. Missing, ambiguous or changed identities
stop publication without selecting a replacement draft.

Draft assets use their authenticated API IDs and exact repository-owned API URLs
for validation and download. GitHub's temporary `untagged-...` browser URLs are not
downloaded. Published release and plugin repository validation still require the canonical
browser URL containing the exact version tag.

An already attested tag can opt into `verified-tag-recovery: 'true'` on the publish
Action, or `--verified-tag-recovery` on the CLI. This requires a manually dispatched
`.github/workflows/recover-release.yml` on the same repository's `main`, with matching
GitHub workflow/source SHAs. The original tag must be an ancestor of both that
workflow commit and fetched `origin/main`. Recovery verifies all four original
`release.yml` attestations against the original tag and commit before any mutation.
It does not create attestations, move tags, or relax artifact and immutability
checks. Normal publication still requires the original tag workflow context.

EUPL-1.2 covers this source. License text comes from the SPDX license list;
third-party Actions retain their own licenses. See CONTRIBUTING.md for validation.

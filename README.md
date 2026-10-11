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
1 recorded infraction; audit forbids growth.

[praetor-hiss-badge]: https://img.shields.io/badge/Standards-HISS%20Adopted%20(1%20baselined)-yellow
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

```sh
python tools.py package --publish-directory artifacts/plugin --tag v1.0.0
```

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

The SBOM lists every locked production package as a build dependency, with NuGet
PURLs and archive hashes. Declared host packages also have runtime relationships;
their DLLs are not included. It records the SDK pin and a SHA-256 fingerprint of
canonical lock JSON, so checkout paths and line endings do not alter the inventory.
The same graph is stored in optional `release.json.dependencyInventory` and is
checked against the SBOM by both publisher and plugin repository. Without these inputs the
document covers shipped files only. SDK internals, the installed host's full
dependency tree and frontend development tools are outside this production graph.

## GitHub Actions

Consume `actions/package`, `actions/publish`, or `actions/catalog` at a reviewed
full commit SHA. Actions use their own downloaded directory; callers do not copy
scripts or check out a mutable tooling branch.

The reusable `.github/workflows/release-please.yml` collects conventional commits
into release PRs, including proposed versions and changelogs. It sets
`skip-github-release: true`: main merges can prepare PRs but never create tags,
GitHub drafts or published releases. Merging a release PR updates files only.
It explicitly dispatches the caller's `ci.yml` for at most 20 returned release PRs,
with a 60-second timeout per dispatch. It exposes no publication inputs or tag
output and never dispatches a publication workflow.

The package action accepts `publish-directory`, `metadata-path`, `version-path`,
`output-directory`, optional exact `tag` and the dependency inputs above; it outputs
`archive` and `directory`.
For a separately authorized publication, attest **all four artifacts** using
`actions/attest` in the plugin's tag-dispatched
`.github/workflows/release.yml`, then call `actions/publish` with `directory`, `tag`
and the built-in `token`. An existing draft and the exact tag are prerequisites;
release PR preparation creates neither. GitHub currently requires maintainer approval of
`GITHUB_TOKEN`-created PR workflows, including later updates; dispatching CI does
not clear that separate approval gate. This is the documented
[June 11, 2026 GitHub behavior](https://github.blog/changelog/2026-06-11-bot-created-pull-requests-can-run-workflows-if-approved/).
Maintainers approve the actual PR workflow after reviewing the current changes.
The automation retains only the built-in token; unattended PR merging is not promised.

## Plugin repository trust and recovery

Only allowlisted public releases are accepted. Every new artifact needs valid
signatures bound to the repository, approved workflow, source SHA, exact tag and
GitHub-hosted runner. Metadata, checksums, ZIP entries and the complete expected
SPDX identity, file inventory and dependency relationships are validated
before the plugin repository changes. Existing plugin repository versions are preserved.

Scans are bounded to 32 repositories, 1,000 releases per repository, and 20 new
versions per batch; remaining releases follow after that plugin repository PR merges.
Drafts and prereleases are skipped. Failed or over-limit scans
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

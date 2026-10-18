<!-- markdownlint-disable MD013 -->
# jellysin/release-helper Agent Operating Harness

<!-- praetor:head -->

Before concluding any turn:

```bash
make verify-all
```

`make verify-all` = repository-owned gate; adoption kept it unread + unexecuted. Steps live in `Makefile`: read there, never restate. Footer commands = project markers only.
Pass = exit 0. Signed Ed25519 Exit-0 receipt only from `praetorctl gate run`; report no receipt it did not mint. Fail -> SARIF diagnostic distillation (<= 1500 tokens).

## Core Directives & Invariants

Adopted check = coverage state + mechanism; else audit scanner; else `unknown`. Gates = (none: no generated `make verify-all`, no praetor `lefthook.yml`); edits not reflected.

| Invariant | Rule | Adopted check | On fail |
| :--- | :--- | :--- | :--- |
| **HISS-01** control flow | recursion prohibited; call graph = DAG | `praetorctl audit` HISS scan: Go `goto`, recursion + plain-function call cycles; Rust, Python direct recursion; C `goto` | advisory |
| **HISS-02** loops, I/O | scalar upper bound on every loop; explicit deadline on every I/O call | `praetorctl audit` HISS scan: unbounded loop shapes in Go, C, Rust, Python; I/O deadlines unchecked | advisory |
| **HISS-03** memory | n/a: no such construct in repository languages; advisory | unknown | advisory |
| **HISS-04** complexity | McCabe cyclomatic <= 12, cognitive <= 15, statements <= 55; func LOC <= 60 (audit ceiling) | `praetorctl audit` HISS scan: function length only; other caps need repository linter | advisory |
| **HISS-05** scoping | declare every identifier in smallest lexical scope serving it | unknown | advisory |
| **HISS-06** concurrency | explicit scalar upper bound on every worker pool + concurrent fan-out | unknown | advisory |
| **HISS-07** errors | every error handled or wrapped with context | `praetorctl audit` HISS scan: partial in Go, Rust, Python | advisory |
| **HISS-08** determinism | zero dynamic code execution (`eval` / `exec`); a process signal's target is bounded above 1 (`killpg`, `kill(-pid)`, Go `syscall.Kill`) | `praetorctl audit` HISS scan: C banned calls, Python `eval` / `exec`, Go `syscall.Kill` / `unix.Kill` | advisory |
| **HISS-09** reference safety | n/a: no such construct in repository languages; advisory | unknown | advisory |
| **HISS-10** warnings | zero warnings: compiler, linter, format sweeps | unknown | advisory |
| **HISS-11** supply chain | pinned lockfiles; zero floating tags; signed provenance | unknown | advisory |
| **HISS-12** secrets | zero credentials in Git history | unknown | advisory |
| **HISS-13** debt ratchet | recorded infractions never grow vs committed baseline | `praetorctl audit` ratchet vs `.standards-baseline.json` | advisory |
| **HISS-14** append-only ABI | public API append-only; breaking change = `!` subject + `Migration:` footer | unknown | advisory |
| **HISS-15** 3D testing | positive + negative + boundary tests, every public interface | unknown | advisory |
| **HISS-16** context integrity | single canonical `AGENTS.md`; vendor files compiled via `praetorctl compile-context` | unknown | advisory |
| **HISS-17** state ledger | turn start `praetorctl state status`; turn end `praetorctl state sync .` | unknown | advisory |
| **HISS-18** CI efficiency | diff-aware gating via `praetorctl ci filter` | unknown | advisory |
| **HISS-19** reuse before writing | one behavior = one implementation; extend or call existing code | unknown | advisory |
| **HISS-20** replayable evidence | every enforcement claim backed by fixtures replayed both directions | unknown | advisory |
| **HISS-21** platform neutrality | gates, hooks, emitted templates run on Linux, macOS, Windows, or skip with stated reason | unknown | advisory |

## Operational Rules

1. **Act on verified state.** Read source files, run real commands before hypothesis or edit. Never guess flag names, library signatures, repo configuration from memory.

2. **Lead with output.** Direct answers, diffs, commands. No filler preamble, no "Based on", no restatement, no chatter.

3. **Context transpiler first.** Never edit `CLAUDE.md`, `.gemini/GEMINI.md`, `.codex/rules.md` manually. All agent instruction updates -> `AGENTS.md`, then:

   ```bash
   praetorctl compile-context
   ```

   - `AGENTS.md` = agent-only text -> caveman (internal register). `praetorctl compile-context --verify` + `praetorctl audit` run caveman lint; findings fail gate; no opt-out. Check first: `praetorctl caveman check --kind=context AGENTS.md`.

4. **SARIF diagnostic distillation.** Compiler/linter errors -> distill to $\le 1,500$ tokens ($< 60$ lines): top 3 root-cause failures with file/line pointers; full SARIF logs -> ephemeral storage.

5. **No evasion.** Never attempt `--no-verify`, `LEFTHOOK=0`, or modifying `.git/hooks`. Adoption installed no hooks here (`lefthook.yml` not praetor-written, or `git-hooks` declined). Adoption scaffolds no CI workflow: `praetorctl` gates run local only.

6. **Anti-loop interception.** Same AST diff + error category repeats $\ge 3$ times -> halt immediately. Re-evaluate design; no micro-textual retries.

<!-- praetor:config -->

## Text Register

<!-- praetor:register:start -->
Register follows the audience, then the task label of your brief (`register:` in `.standards.yaml`; labels are the router's `target_tasks`).

| Register | Where | Form |
| :--- | :--- | :--- |
| social | forge: issues, PR bodies, review comments, commit bodies | `social-text` skill: BLUF, full sentences, scannable, enough and no more; conventional commit subject unchanged |
| docs | docs/, README, ADR bodies | complete without bloat: newcomer path first, expert reference after; every claim points at a file, command or test; no restated code |
| internal | briefs, agent-to-agent traffic, research fan-outs, workflow returns | `caveman` skill: fragments, no filler, verbatim code/paths/errors; facts, paths, commands, verdict |

- Task rows: social = commit_message_synthesis, waiver_signoff; docs = architecture_synthesis, function_docstrings; every other label and any unlabeled text = internal. Subagent launch brief: `caveman` brief shape with `task:` = routing label.
- Evidence above 58 lines or 1500 tokens leaves the message as a file under `.workingdir/evidence/`; return `evidence: <path> sha256:<12 hex> lines:<n>` and fetch it only when a decision needs it.
- An internal return carries verdict, changed paths, commands run, evidence pointers and open questions, nothing else.
<!-- praetor:register:end -->

<!-- praetor:tail -->

## Primary Verification Commands

```bash
# Fast local test suite
# Declared commands only; run them before claiming application verification.
'python3' '-c' 'import sys; sys.exit(0 if sys.version_info >= (3, 14) else '\''Python 3.14+ is required for zero-test failure detection'\'')'
'python3' '-m' 'unittest' 'discover' '-s' 'tests'

# Recompile and verify cross-agent context outputs
praetorctl compile-context --verify

# Audit repository against declared HISS standards
praetorctl audit

# Repository gate; steps live in Makefile
make verify-all
```

<!-- markdownlint-enable MD013 -->
<!-- markdownlint-disable MD025 -->
<!-- praetor:harness:end -->

---

# Repository instructions

Read CONTRIBUTING.md before changing releases, branches, or workflow permissions.
All source here is build-time tooling. Installed Jellyfin plugins never import it.

- Keep production code Python standard-library-only. Pin development tools exactly with hashes.
- Every network response, asset, page count and external process must be bounded.
- Allow only reviewed HTTPS hosts. Never forward API token to download redirects.
- Treat release metadata and ZIP contents as untrusted; validate before writing plugin repository.
- Preserve published artifact bytes and plugin repository versions. Never add upload replacement flags.
- Verify provenance against approved repository, workflow, source commit and tag.
- Limit functions to 120 lines, 60 statements and cyclomatic complexity 10.
- Test recovery, failure atomicity, and independent plugin identities.
- No broad exception swallowing, hidden retries, or raw API error logging.
- Fix Ruff and policy findings. Keep branch coverage at least 85% for these security-sensitive tools.
- Pin Actions to full SHAs, use read-only defaults and job-scoped writes, and explicitly dispatch bot CI.
- Conventional Commit PR titles, squash merges, release-please-owned versions, strict required checks.
- Do not claim GitHub setting or hosted check succeeded without live evidence.

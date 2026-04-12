# Technology Stack Validation

Evaluation of Locki's technology choices, focusing on pragmatic fit for the use case:
a CLI tool (~1000 lines) that orchestrates subprocesses (Lima, Incus, SSH, git)
and ships as cross-platform wheels with bundled native binaries.

Lima and Incus are excluded from this review (already validated as good fits).

---

## Python (language choice) -- Good fit

**Alternatives considered:** Go, Rust, Bash

The codebase is process orchestration glue: it calls `limactl`, `incus`, `git`,
`ssh-keygen`, and `sshd`, then waits for them to finish. Wall-clock time is
99%+ subprocess I/O. Go or Rust would save ~100ms on cold startup but add zero
benefit during the actual work.

Python is the right call here because:

- **Distribution is already solved.** The target audience has `uv` installed.
  `uv tool install locki` is a one-liner. A Go rewrite would need a separate
  distribution story (Homebrew, manual binary downloads, etc.) or would need to
  piggyback on another package manager anyway.

- **The packaging problem is already solved.** Platform-specific wheels with
  bundled Lima binaries across 4 targets (Linux x86_64/aarch64, macOS
  x86_64/arm64) are working. Go's `GOOS/GOARCH` cross-compilation is simpler
  in theory, but the existing wheel pipeline already handles it.

- **The code is simple.** ~1000 lines of straightforward orchestration. Python's
  readability makes this easy to maintain and contribute to. Go would add
  boilerplate (error handling, type definitions) without buying anything for
  code of this complexity.

- **Startup time is acceptable.** Python CLI cold start is 50-150ms. With `uv
  tool run`, add 20-50ms. This gates an interactive session that then takes
  seconds to minutes, so the delta vs Go's 5-10ms startup is imperceptible.

**Verdict: Keep Python.** A rewrite would trade a solved set of problems for a
new set, with no user-visible benefit.

---

## Python >=3.14,<3.15 (version pin) -- Too restrictive

The codebase uses these version-gated features:

| Feature | Minimum Python |
|---|---|
| `list[str]`, `dict[str, str]` in annotations | 3.9 |
| `str.removeprefix()` | 3.9 |
| `pathlib.Path.is_relative_to()` | 3.9 |
| `functools.cache` | 3.9 |
| `match`/`case` statements | 3.10 |
| `X \| None` union syntax in annotations | 3.10 |
| `tomllib` (stdlib) | 3.11 |

**The actual minimum is Python 3.11.** There are no template string literals
(`t"..."`), no `annotationlib`, no `concurrent.interpreters`, and no other
3.14-only features in use.

The current pin `>=3.14,<3.15` means `pip install locki` **refuses to install**
on Python 3.11, 3.12, or 3.13. This is a hard gate enforced at install time,
not a suggestion. Given that Python 3.14 was only released in October 2025,
many users (especially in enterprise or CI environments) are still on 3.12
or 3.13.

**Recommendation:** Widen to `>=3.11` (or `>=3.12` to stay within mainstream
support). Drop the `<3.15` upper bound -- it will break installs when 3.15
arrives and provides no protection since the dependencies already declare their
own bounds. If you want to keep the Ruff target version for lint checks, that
can stay at `py314` independently.

---

## Click (CLI framework) -- Good fit

**Alternatives considered:** Typer, argparse

Click is the correct choice for this project's specific patterns:

- **Command aliases** (`exec | x`, `remove | rm | delete`) are implemented via
  a custom `AliasGroup` subclass. Typer wraps Click but does not expose Group
  subclassing cleanly -- you'd need undocumented internal access (`typer.core`)
  that can break between minor versions.

- **Extra args passthrough** (`allow_extra_args=True, ignore_unknown_options=True`)
  is used by `exec_cmd` to forward arbitrary arguments into the sandbox. This
  is a first-class Click pattern via `ctx.args`. argparse has
  `parse_known_args()` but it's manual and less ergonomic.

- **Click 8.x is stable and maintained.** No blocking issues. It's the
  de facto standard for Python CLIs of this complexity.

Typer would add a layer of indirection for no benefit. argparse would add
boilerplate. Click is the pragmatic choice.

**Verdict: Keep Click.**

---

## Halo (terminal spinners) -- Replace

**Alternative: Rich**

Halo's last release was **November 2020** (v0.0.31). It is effectively
abandoned:

- **No Python 3.14 compatibility work.** It technically installs (declares
  `>=3.4`) but there's no active maintainer to fix anything that breaks.
- **Known `setDaemon()` DeprecationWarning** on Python 3.12+ -- the method was
  deprecated and will be removed. Other projects (e.g. mandiant/capa) have
  migrated away from Halo specifically because of this.
- **Thread safety concerns.** Halo runs the spinner on a background thread
  with no robust cleanup. In a tool that calls `os.execvp()` (as Locki does),
  an active spinner thread during exec is undefined behavior.
- **No Windows support** (falls back to a basic line spinner).

`rich` (by Textualize, actively maintained) provides `console.status()` as a
direct spinner replacement. It's a context manager, handles cleanup properly,
and works cross-platform. The migration is mechanical: replace `Halo(text=...,
spinner="dots")` with `Console().status(message)`.

Rich is a heavier dependency (~5MB vs Halo's ~50KB), but it also replaces the
need for manual ANSI escape code handling if you ever want richer output
(tables, syntax highlighting, progress bars). For a tool whose audience is
developers, Rich is standard infrastructure.

**Recommendation: Replace Halo with Rich.** This is the only dependency in the
stack with active maintenance/compatibility risk.

---

## Pydantic (config validation) -- Acceptable, but heavy for the use case

**Alternatives considered:** dataclasses + manual validation, msgspec, cattrs

Pydantic v2.12+ has explicit Python 3.14 support and works correctly. The
concern is proportionality: Locki's config model is a single class with one
field:

```python
class LockiConfig(pydantic.BaseModel):
    incus_image: dict[str, str] = Field({"x86_64": "images:fedora/43", ...})
```

Pydantic brings ~15MB of compiled Rust extensions (`pydantic-core`) for this.
A `dataclasses.dataclass` with 5 lines of manual validation would do the same
job. However:

- Pydantic's TOML-to-model validation (`model_validate()`) is genuinely
  convenient and handles edge cases (type coercion, nested structures,
  clear error messages) that would need manual code.
- If the config grows (and it likely will -- more container options, agent
  configurations, etc.), Pydantic pays off quickly.
- Install time cost is paid once and is negligible for a developer tool.

**Verdict: Keep Pydantic.** The weight is disproportionate today but reasonable
given likely config growth. Not worth replacing.

---

## uv + uv_build (package manager + build backend) -- Good fit

**Alternatives considered:** pip + setuptools, poetry, hatch

`uv` is the right package manager for this project:

- **Target audience alignment.** Locki's install instruction is
  `uv tool install locki`. If users didn't already have `uv`, this would be a
  chicken-and-egg problem. But the target audience (developers using AI coding
  agents) overwhelmingly already has `uv`.
- **`uv_build` as build backend** produces correct wheels and handles the
  platform-specific wheel repackaging in the build script. It's simpler than
  setuptools for this pattern.
- **`uv sync --all-extras --dev`** is fast and reproducible.

The `exclude-newer = "P7D"` setting in `[tool.uv]` is a nice touch for
reproducibility (only resolve packages published >7 days ago).

**Verdict: Keep uv + uv_build.**

---

## Ruff (linter/formatter) -- Good fit

No meaningful alternatives to consider. Ruff has replaced flake8, isort, black,
and pyupgrade for Python projects. It's fast, maintained, and the configured
rule set (`E, W, F, UP, I, B, N, C4, Q, SIM, RUF, TID, ASYNC`) is
comprehensive without being noisy.

**Verdict: Keep Ruff.**

---

## Mise (task runner + tool version manager) -- Good fit

**Alternatives considered:** Makefile, just, nox

Mise serves two roles:

1. **Inside sandboxes:** Tool version management (`mise use python@3.12`,
   `mise install nodejs@24`). This replaces nvm, rbenv, pyenv, etc. with a
   single tool. Given that sandboxes run arbitrary projects with arbitrary
   toolchains, this is exactly right.

2. **For Locki development:** Task runner (`mise run check`, `mise run build`).
   This is equivalent to a Makefile but with built-in dependency tracking
   (`sources`, `outputs`, `depends`) and tool version pinning.

Make would work fine for (2) but can't do (1). Nox is Python-specific and
can't manage Node, Go, etc. `just` is a good task runner but doesn't manage
tool versions.

**Verdict: Keep Mise.**

---

## OpenSSH (git/gh command proxy) -- Good fit

The SSH forced-command pattern for the git/gh proxy is clever and well-suited:

- **Mature, audited, ubiquitous.** sshd is already installed on virtually all
  Linux systems and macOS. No new dependency for most users.
- **Forced commands are a first-class security feature.** The
  `command="... locki safe-cmd"` in `authorized_keys` means every SSH
  connection is funneled through the allowlist validator. This is exactly what
  forced commands were designed for.
- **Key-based auth eliminates password management.** Auto-generated ed25519
  keypairs with no passphrase are appropriate for a local-only proxy
  (localhost:7890).

The only concern is port 7890 being hardcoded -- if something else uses it,
Locki breaks silently. A dynamic port (written to a file the container reads)
would be more robust, but this is a minor operational issue, not a technology
choice problem.

**Verdict: Keep OpenSSH for the proxy.**

---

## Fedora 43 (default container image) -- Good fit

**Alternatives considered:** Ubuntu, Alpine, Debian

Fedora is the right default for sandboxes running AI coding agents:

- **systemd included and working.** This is the headline feature -- agents need
  to run `systemctl`, Docker, and Kubernetes. Alpine doesn't have systemd.
  Ubuntu has it but Fedora's is more up-to-date.
- **Recent packages.** Fedora tracks upstream closely, so agents get recent
  compilers, libraries, and tools without needing PPAs or backports.
- **dnf with RPM Fusion** (auto-configured in `container-setup.sh`) covers
  virtually any package an agent might need.
- **Incus has first-class Fedora images** (`images:fedora/43`), so no custom
  image building needed for the default case.

Ubuntu would also work but Fedora's faster package cycle is a better fit for a
tool that wants "latest everything." Alpine would break too many assumptions
(musl libc, no systemd, missing packages).

**Verdict: Keep Fedora as default.**

---

## TOML (configuration format) -- Good fit

`locki.toml` and `pyproject.toml` both use TOML. Python 3.11+ has `tomllib`
in stdlib, so no external parser is needed. TOML is the standard config format
for Python tooling. No reason to use YAML, JSON, or INI.

**Verdict: Keep TOML.**

---

## Summary

| Choice | Verdict | Action needed? |
|---|---|---|
| Python (language) | Good fit | No |
| Python >=3.14,<3.15 | Too restrictive | Widen to `>=3.11` |
| Click | Good fit | No |
| **Halo** | **Replace** | **Migrate to Rich** |
| Pydantic | Acceptable | No |
| uv + uv_build | Good fit | No |
| Ruff | Good fit | No |
| Mise | Good fit | No |
| OpenSSH | Good fit | No |
| Fedora 43 | Good fit | No |
| TOML | Good fit | No |

**Two actionable items:**

1. **Replace Halo with Rich** -- the only dependency with active
   maintenance/compatibility risk. Halo is abandoned (5+ years) and has known
   deprecation warnings on modern Python.

2. **Widen `requires-python`** -- the codebase needs 3.11 at minimum, not 3.14.
   The current pin unnecessarily excludes the majority of Python users.

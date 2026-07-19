# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.

## The one hard rule: `m365_openai_proxy.py` is pure-stdlib, single-file

The entire *shipped* product is one file, **`m365_openai_proxy.py`**, and it
**must use only the Python 3 standard library — no third-party runtime
dependencies, ever, for any feature.** This is not a stylistic preference; it
is the product's whole pitch: *drop this one file onto any machine with a
Python 3 interpreter and run it — nothing to install.*

Concretely, inside `m365_openai_proxy.py`:

- **No `pip install` / no imports of any non-stdlib package.** Not `requests`,
  not `websockets`, not `cryptography`, not `msal`, not an MCP SDK — nothing.
- This constraint has already been honored in non-obvious places and must stay
  honored:
  - the WebSocket client is implemented directly on `socket` + `ssl`
    (`WebSocketClient`);
  - the SignalR framing is hand-rolled (`SignalRBuffer`);
  - **AES-256-GCM is implemented from scratch in pure Python**
    (`aes256_gcm_decrypt` and friends), because the MSAL encrypted-cache
    decrypt path needs it and pulling in `cryptography` was not an option.
    It is validated against the FIPS-197 test vector at startup.
- If a feature genuinely cannot be done in stdlib, that is a signal the feature
  does not belong *in the proxy* — see the next section.
- Keep it a single file. Do not split the proxy into a package/modules.
- The supported interpreter range is **Python 3.11–3.13** (see CI). Don't use
  syntax/stdlib newer than 3.11 supports.

If you are ever tempted to add a dependency to the proxy: **don't.** Either
implement it in stdlib, or move it out of the proxy (below).

## Everything *around* the proxy may use rich Python tooling

The single-file, stdlib-only rule applies **only to the shipped
`m365_openai_proxy.py`**. All the scaffolding around it is unrestricted:

- **The test suite** (`tests/…`, run with `pytest`) and **dev/investigation
  scripts** (`scripts/…`) may `uv`/`pip install` and import **any** third-party
  library — pytest, real MCP SDKs, HTTP clients, whatever helps. They are
  developer tools, are **not part of the shipped proxy**, and are never
  required to run it. They import the proxy as a module
  (`import m365_openai_proxy as proxy`) to exercise its internals, and may
  stub/monkeypatch it (see the offline suite under `tests/`). The dev tooling
  is managed with **uv** (`uv sync` / `uv run …`); see `pyproject.toml`'s
  `[dependency-groups] dev` and `uv.lock`.
- **CI and local dev tooling** (ruff, bandit, coverage, etc.) are installed
  freely — see `.github/workflows/ci.yml`.
- Prefer this split deliberately: when something useful needs a real library
  (e.g. a mock MCP server subprocess, a fuzzer, a load-test harness), build it
  under `tests/` (if it's an automated test) or `scripts/` (if it's a manual
  probe) with whatever deps it wants, and keep the proxy pristine.

Rule of thumb: **the proxy is the deliverable and stays dependency-free; the
tooling is a workshop and can use any tool in the shed.**

## What CI enforces (make it green locally before pushing)

`.github/workflows/ci.yml` runs, on every PR (dev tooling via **uv**):

1. **Lint** — `uv run ruff check .`
2. **Format** — `uv run ruff format --check .`
3. **Security** — `uv run bandit -c pyproject.toml m365_openai_proxy.py`
   (config + skip rationale live in `pyproject.toml`'s `[tool.bandit]`; suppress
   any true positive inline with `# nosec BXXX: <reason>`, not by widening the
   global skips).
4. **Tests** — `uv run pytest` on **Python 3.11–3.13**.
5. **Smoke test** — byte-compile + a `--help` run (which imports the module and
   so exercises the pure-Python AES self-check) on **Python 3.11–3.13** on
   Linux, and 3.11 + 3.13 on macOS and Windows. This job uses a bare
   interpreter (no uv, no `pip install`) to prove the proxy runs with nothing
   installed.

Run the same locally before pushing (this order — format, then lint, then
compile, then tests):

```bash
uv run ruff format .
uv run ruff check .
uv run bandit -c pyproject.toml m365_openai_proxy.py
python3 -m py_compile m365_openai_proxy.py
python3 m365_openai_proxy.py --help >/dev/null      # imports module + AES self-check
uv run pytest                                       # offline (no-network) suite
```

`CodeQL` also runs (`.github/workflows/codeql.yml`).

## Credentials — never touch them

The proxy authenticates from four plain-text credential files
(`m365_openai_proxy.<field>.conf`) that the **user** creates and maintains.
Agents/tooling must **never create, edit, or overwrite** the user's credential
files (or the other credential/token artifacts listed in `.gitignore`). Read
them if needed; only the user updates them. All such files are gitignored and
must never be committed. (The proxy itself rotates
`*.refresh_token.conf` at runtime — that is the proxy's job, not a tooling
edit.)

## Logging conventions (in the proxy)

The proxy logs **file-only** (`m365_openai_proxy.log`), never to the console
except a single non-technical fatal message when it truly can't continue.
**Never log secrets/tokens/passwords** — only lengths, ids, and non-secret
metadata. Preserve this when editing.

## Versioning & releases

Versions follow **[Semantic Versioning](https://semver.org/)** —
`MAJOR.MINOR.PATCH`, always three digits (e.g. `0.8.0`, not `0.8`) — tagged
`vMAJOR.MINOR.PATCH` (e.g. `v0.8.0`). Bump PATCH for fixes, MINOR for
backward-compatible features, MAJOR for breaking changes.

The version lives in **one canonical place — `PROXY_VERSION` in
`m365_openai_proxy.py`** (it's what the proxy reports in its startup banner and
its HTTP `Server:` header). Two other places must always agree with it:

- **`pyproject.toml`'s `version`** — the same `X.Y.Z`, without the `v` prefix.
- **the git tag + GitHub Release** — tag `vX.Y.Z` for `PROXY_VERSION` `X.Y.Z`.

There is no automated/derived versioning (setuptools-scm/hatch-vcs and friends
need a build backend, and this project deliberately has none —
`[tool.uv] package = false`, nothing to build), so the sync is a manual step.
**To cut a release:**

1. Bump `PROXY_VERSION` in `m365_openai_proxy.py` (a full `X.Y.Z`).
2. Set `version` in `pyproject.toml` to the same `X.Y.Z`.
3. Run the local CI gate (see above) so the release is green.
4. Commit, then tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. Create the GitHub Release for `vX.Y.Z` (`gh release create vX.Y.Z …`).

If you ever bump one of these, bump all of them in the same change — a mismatch
between the banner, `pyproject.toml`, and the tag/Release is a bug.

## Where things live

- `m365_openai_proxy.py` — the entire shipped proxy (stdlib-only). Its module
  docstring is the authoritative spec for behavior, credential formats, the
  AES-GCM/HKDF algorithm, and known limitations.
- `REVERSE_ENGINEERING.md` — the protocol reverse-engineering writeup
  (Chathub/SignalR wire format, FOCI/MSAL auth chain, cache-encryption, the
  Local MCP tool-calling bridge findings, etc.). Keep new protocol findings
  here.
- `tests/` — the offline (network-free) `pytest` suite (may use any deps);
  `conftest.py` holds the shared fixtures.
- `scripts/` — developer-only live probes needing real credentials/network
  (may use any deps); **not** run in CI.
- `README.md` — user-facing usage and the compatibility/limitations picture.
- `pyproject.toml` — project metadata (its `version` mirrors the proxy's
  `PROXY_VERSION` and the git tag/Release — see "Versioning & releases"), the
  uv-managed `[dependency-groups] dev` tooling, and the `pytest` + `bandit`
  config. There is no build system; the product is a single script, so
  `[tool.uv] package = false`.
- `uv.lock` — the pinned dev-tooling lockfile (committed).

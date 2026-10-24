# Cutting a JMFTS release

> **Review note, 2026-10-17.** This document was held back from the 0.5.1 history switchover
> until today and read from `origin/oldmaster`. It was last edited on 2026-09-12 and it had
> gone false in the ways below. Per this repository's convention the corrections are marked
> in place with a dated note, and the text they correct is kept where a citation still needs
> it. Section numbers are unchanged: around eleven source comments cite this file by step.
>
> * **Two wheels became three.** `jmfts-web` is the third distribution (`SPRINT_0_6_0.md`
>   Block F step 19). Step 1 said "one number, two wheels, one tag" and listed three version
>   locations; `./bump-version.sh` now writes **five** (read off its own output, 2026-10-17).
>   Corrected in step 1, in step 4, and in step 5.
> * **Step 3, "Replace the public tree", no longer happens.** Development has been in the
>   one repository since 0.5.1 (tag dated 2026-09-13): one history, two remotes, `origin` and
>   `github`, and a release is a tag. The "two repositories" section described a squash into
>   a second checkout; it is replaced by "The one repository". Step 3 keeps its number and is
>   a stub, so citations to steps 4, 4b and 5 still resolve. `docs/SPRINT_0_6_0.md` question
>   4.10 is the decision.
> * **`NOT_PUBLISHED` is gone** and `PUBLISHED` describes rather than drives
>   (`tests/test_readme_links.py`). The paragraph that told the releaser to subtract one
>   from the other is removed with step 3.
> * **The gate's scope names three paths, as before, but `jmfts-needle/`, `benchmarks/` and
>   `analysis/` are no longer in this tree**; `scripts/` is now published and is outside
>   the gate on purpose. Corrected in step 2.
> * **`jmfts-web` is on PyPI** at 0.6.0, 0.6.1 and 0.6.2, created by its pending publisher
>   with no hand upload. Recorded in step 5 under "The third name". A to-do for the
>   releaser of 0.7.0 is there too.
> * **Counts that go stale** (migrations, operations) are no longer quoted in step 4b; the
>   command that reads them is.
> * **Added from the 0.6.x series**, from lessons the `release` skill had already recorded
>   (an internal, untracked release checklist): the bump is gated on the BUMPED commit,
>   `jmfts-web` stays uninstalled from the gate venv, read CI of the commit before the bump
>   as well as the bump's own, and PyPI's JSON endpoint lags the upload. All in the steps
>   that carry them.
>
> Commands, file names and test names were read against the tree on 2026-10-17 where this
> note or an inline note says so; prose not marked was not re-derived. The release table directly below is dated 2026-09-12 and is left as a record of
> that date; the 0.6.x rows follow it.

Written 2026-08-21, while adding the packaging metadata that makes a release possible
at all. Steps 1 to 4 have been run and the values below were read off the artifacts they
describe.

**Where the releases stand, read 2026-09-12.** Every step of this document has now run at
least once, and 0.3.0 is the first release the pipeline published by itself:

| | 0.2.0 | 0.2.1 | 0.3.0 | 0.5.0 |
|---|---|---|---|---|
| public squash on `github` | `05bc9d9` | `9fcd47a` | `d6be716` | `099dd90` |
| tag pushed to `github` | `v0.2.0` | `v0.2.1` | `v0.3.0` | `v0.5.0` |
| on PyPI | both wheels, **uploaded by hand** | **nothing** | both wheels, **published by CI** | both wheels, **published by CI** |

`pip install jmfts` therefore got 0.5.0 on that date. There is no 0.4.0 — `CHANGELOG.md`'s
0.5.0 entry says why — and PyPI skips 0.2.1 entirely. That gap is not recoverable and is
not worth recovering — the `v0.2.1` tag push ran the publish workflow and the upload step
failed on a PyPI-side configuration left over from the hand bootstrap. Step 5 records what
failed and what fixed it, because the same trap is waiting for the next project this
runbook is copied to.

**Read 2026-10-17, the 0.6.x series.** Tags `v0.6.0`, `v0.6.1` and `v0.6.2` are all dated
2026-10-12 (`5e730c9`, `cab05f3`, `e0cc852`). All three published **three** wheels through
`publish.yml`, and `jmfts-web` first appeared on PyPI at 0.6.0 (the maintainer's account of
PyPI; this document's author cannot query it). There is no "public squash" row any more:
the tag is on the one history. Tags `v0.5.1` (2026-09-13) and `v0.5.2` (2026-10-03) exist;
what each uploaded was not re-read for this note.

> **Corrected 2026-10-17.** This paragraph read "**A release now cuts TWO distributions.**
> `jmfts-client/` holds the wire contracts and the generated HTTP client, and `jmfts`
> depends on it." That was true from 0.2.0 to 0.5.2.

**A release cuts THREE distributions.** `jmfts-client/` holds the wire contracts and the
generated HTTP client, and `jmfts` depends on it. `jmfts-web/` holds the browser console's
built static files and has no dependencies; `jmfts[web]` pins it. Every step below applies
to all three unless it says otherwise.

This runbook borrows its shape from the τ monorepo's `docs/RELEASING.md`
(`agent-harness-py`), because the two projects publish the same way.

## The one repository

> **Replaced 2026-10-17.** This section was "The two repositories": an internal tree on
> `origin`, a second checkout (`jmfts_release`) whose `github` remote received one squash
> commit per release on a `public/<version>` branch, and a warning that `git push origin
> master` from that checkout would put a squash on internal development. None of that
> runs. It is kept in `origin/oldmaster:docs/RELEASING.md` for anyone reading a citation
> older than 0.5.1.

There is one history. Remotes of this clone, read off `git remote -v`:

| remote | what it is |
|---|---|
| `origin` | the internal git host |
| `github` | `github.com/jmccardle/jmfts`, the public repository |

A commit goes to both as it is made, `master` is the trunk on both, and a release is a tag
on it. **Publication happens at push time, not at release time**: a commit subject, a test
fixture, a docstring and a `docs/` file are public the moment they reach `github`, and a
force-push is not a way to unsay it.

Trusted Publishing needs the identity `(owner, repository, workflow filename, environment)`
on the PUBLIC repository (`publish.yml`'s header), so a tag pushed only to `origin` fires
nothing. The tag goes to `github`.

What the world sees is defined by `tests/test_readme_links.py::PUBLISHED`. It is no longer
a copy list; it is still the definition the README-link tests are written against, and
adding a path is a decision about what is published (one-way, for a `docs/` file). The
leakage scan walks a separate list, `tests/test_no_host_addresses.py::SHIPPED`. When a path
joins `PUBLISHED`, check whether it joined `SHIPPED`; it has been missed before, and
`SHIPPED` once did not list `ROADMAP.md`, `CHANGELOG.md`, `docs/` or `jmfts-web/` when all
four were published.

## Steps

### 1. Bump

**The three distributions release in lockstep: one number, three wheels, one tag.**

> **Corrected 2026-10-17.** This read "one number, two wheels, one tag", with three
> locations.

```bash
./bump-version.sh 0.7.0
git commit -am "release: version 0.7.0"
```

The script is ported from the τ monorepo's script of the same name. It refuses a dirty
tree, validates the argument as a PEP 440 release number, writes **five** locations, and
re-reads each from disk afterwards. Its report, read by running it on 2026-10-17:

| location | what it is |
|---|---|
| `jmfts_core/__init__.py` | the server wheel's version |
| `jmfts-client/jmfts_client/__init__.py` | the client wheel's version |
| `jmfts-web/jmfts_web/__init__.py` | the front end wheel's version |
| `pyproject.toml`, `jmfts-client==` | the pin in the base dependencies |
| `pyproject.toml`, `jmfts-web==` | the pin in the `web` extra |

Read the list it prints rather than counting from this table. All three `pyproject.toml`
files read `__version__` through `[tool.setuptools.dynamic]`, so the three literals *are*
the wheels' versions. The pins are the copies this repo cannot make dynamic, because a
requirement string has nowhere to read a version from, and so they are the ones a manual
bump forgets. The script discovers them by pattern, not by path, and fails if it matched
none.

Do not add a `version = "..."` literal to any `pyproject.toml` to fix a mismatch.
`tests/test_packaging.py::test_the_version_has_exactly_one_home` and
`::test_the_client_version_has_exactly_one_home` forbid the second copy;
`tests/test_web_distribution.py::test_the_three_distributions_carry_one_version` and
`::test_the_web_extra_pins_the_exact_version` hold the third wheel and its pin;
`tests/test_packaging.py::test_the_server_and_client_release_in_lockstep` holds the server,
the client and the client pin (renamed 2026-10-17 from `..._the_two_distributions_...`, which predates the third wheel).

**The script does not touch the README.** `tests/test_doc_drift.py` holds the README's
quoted `jmfts-client==` pin to `pyproject.toml`. So after the bump run
`pytest tests/test_packaging.py tests/test_web_distribution.py tests/test_doc_drift.py`.

> **Added 2026-10-17, from the 0.6.0 recut** (the `release` skill, step 1). The first
> `v0.6.0` tag failed CI on exactly that README pin: the full suite had run BEFORE the bump,
> and the bump was gated on the first two test files only. **Run the full suite on the
> bumped commit, not on the one before it.**
>
> **Superseded 2026-10-24.** The 0.6.0 failure came from a targeted set that omitted
> `test_doc_drift.py`, not from the absence of a full run. The full suite now runs once
> locally, on the last code commit (step 2). The bump commit runs only the tests that read a
> version literal or the CHANGELOG: `pytest tests/test_packaging.py
> tests/test_web_distribution.py tests/test_doc_drift.py tests/test_readme_links.py`. Re-grep
> that list each release with `grep -lE "__version__|CHANGELOG" tests/`, plus
> `test_doc_drift.py`. The full run on the bumped commit is CI's: `publish.yml`'s `gate` job
> uses `ci.yml`, so a tag whose commit fails the suite cannot publish.

**Why lockstep rather than independent versions.** `jmfts-client/jmfts_client/_verbs.py`
is generated from the server's resolved route table. A client built from a different
release was generated from a different surface, and that mismatch does not fail at install
time — it fails at call time, as a verb that is missing or whose parameters moved. Pinning
`==` and releasing together is what makes them one product. `jmfts-web` is written against
that surface and its bundled client is generated from `/openapi.json`, so it is the same
argument. The cost, stated plainly: a wheel's version sometimes moves when nothing in it
changed. `bump-version.sh`'s header states the other cost avoided: versioning them apart
would have needed a tag namespace per distribution and a filter in `publish.yml`.

> **Added 2026-10-17.** Keep `jmfts-web` UNINSTALLED from the venv the gate runs in, as CI
> does. `tests/test_web_distribution.py::test_the_appliance_starts_with_no_front_end_installed`
> fails by design when it is installed, and `pip install -e ./jmfts-web` to "refresh" the
> version is how it gets installed by accident (that is what happened at 0.6.2).

### 2. Gate

> **2026-10-24:** run this ONCE per release, on the last commit that changes code, so it
> happens before step 1's bump rather than after it. On branches, run only the tests for
> what changed; the full suite is the release gate and nothing else. Run one gate on the
> machine at a time. Five concurrent runs took 2 h each instead of about 15 min alone.
>
> **Superseded later on 2026-10-24: the release gate is GitHub CI, not a local run.** The
> local full suite passed 3491/0 on `7c2534e`. CI's fresh install failed 32 tests on the
> same tree, because CI resolved `pymupdf` 2.0 and `ffwf-tau-llm` 0.14.1 and the local venv
> still held 1.28.2 and 0.12.0. CI had been red on every master push since 2026-10-18 without
> anyone reading it. A long-lived venv cannot answer "does `pip install jmfts` work today";
> a fresh install can. The procedure is now:
>
> 1. Bump and write the CHANGELOG heading (step 1). Run only the four version-reading test
>    files locally, then commit.
> 2. Push to both remotes. `ci.yml` runs on the push to `master`. Read it on THAT sha:
>    `gh run list -R jmccardle/jmfts --workflow CI -L 3`, then `gh run watch <id>`. Green on
>    the bumped sha is the gate. A red run is read with `gh run view <id> --log-failed`,
>    never re-run to green (rule 4 of the `release` skill).
> 3. Rehearse the build without uploading: `gh workflow run publish.yml -R jmccardle/jmfts
>    --ref master`. It runs CI again, then `build` with `twine check --strict`. Then
>    `gh run download <id> -R jmccardle/jmfts` fetches `dist-jmfts`, `dist-jmfts-client` and
>    `dist-jmfts-web`, kept for 7 days. Smoke-install those wheels (step 6). They are the
>    files the tag would upload, built by the same job.
> 4. Tag and push the tag (step 7). That is still the only irreversible step.
>
> A local full run is optional and costs about 17 min of this machine. Run it when a change
> needs a database-backed debugging loop, not as a gate. Read CI on every master push, not
> only at release time.

```bash
./scripts/run_tests_docker.sh -rf --tb=line         # the full suite, throwaway pgvector
black --line-length 100 --check jmfts_core jmfts-client tests
ruff check jmfts_core jmfts-client tests
```

Those three paths are the gate, not `.`, and the same three are what
`.github/workflows/ci.yml` and `.githooks/pre-commit` run. The scope is the wheels'
Python plus the suite that proves them. `scripts/` is published and outside the gate on
purpose — it is tooling and one-off research — so `ruff check .` reports findings there
and that is expected. `jmfts-web/` is outside this gate and carries its own
(`SPRINT_0_6_0.md`).

> **Corrected 2026-10-17.** This paragraph named `jmfts-needle/`, `scripts/`, `benchmarks/`
> and `analysis/` as research code "not in `PUBLISHED`". `scripts/` is in `PUBLISHED`;
> the other three are not in this tree. Also corrected: `-rf --tb=line`. A gate that says
> "something broke" and not "what" leaves only the re-run, which is how an intermittent
> failure gets rationalised into a pass.

Run the suite the way CI runs it. `./scripts/run_tests_docker.sh` provisions
`pgvector/pgvector:pg16` and an empty `jmfts_test`; a bare `pytest` needs
`ALTER ROLE jmfts CREATEDB` and silently SKIPS every database-backed test without it,
reporting a green CI will not reproduce. **`JMFTS_CI_PG_PORT` is a mutex as well as a
port**: the container name carries it, and the script removes any container of that name
at startup, so a second run on the same port destroys the first's database mid-flight. The
failures land in files the change never touched and look exactly like flakes. One run per
port.

Activate the hook once per clone, and the gate runs before the drift lands rather than
after:

```bash
git config core.hooksPath .githooks
```

The release-defect tests run inside that suite and need no database:

```bash
pytest tests/test_packaging.py tests/test_no_host_addresses.py \
       tests/test_readme_links.py tests/test_client_codegen.py
```

They are the ones that fail on a release defect rather than a code defect: an undeclared
runtime data file, a developer-machine path in a shipped file, a README link into a path
the public tree does not carry, a client that no longer matches the surface it was
generated from.

CI's "Release guards" job runs `tests/test_packaging.py`, `tests/test_office_packaging.py`,
`tests/test_readme_links.py` and `tests/test_no_host_addresses.py` on four interpreters.
The one difference from the list above is deliberate: it cannot run
`tests/test_client_codegen.py`, because that job is a BASE install and the generator
formats its output with `black`, which only the dev extra brings. The suite job covers that
file instead. Do not add it to the guard job to make the two lists match — it would fail on
a missing import rather than on a defect.

Four generated artefacts, and a stale one reaches a wheel looking exactly like a fresh one.
Their staleness tests run in the suite; run the commands too and read the output:

```bash
python -m scripts.generate_client                  # jmfts-client/jmfts_client/_verbs.py
python -m scripts.generate_reference --check       # docs/reference/
python -m scripts.generate_ts_client --check       # jmfts-web/jmfts_web/static/client/
```

and the OpenAPI document itself (`/openapi.json`), which the third is rendered from. If
`test_generated_client_is_current` fails, the exposed surface changed and the client was
not regenerated; fix it with `python -m scripts.generate_client`.

### 3. Replace the public tree — RETIRED 2026-10-17

> **Retired 2026-10-17; this step does not happen.** It branched `public/<version>` in a
> second checkout from the previous release, wiped and re-copied `PUBLISHED` minus
> `NOT_PUBLISHED`, committed one squash and tagged it. Development moved into the public
> repository at 0.5.1 (tag dated 2026-09-13), so there is no copy to make and no second
> checkout. The number stays so that a citation to step 4, 4b or 5 still lands on the step
> it named. The text of the retired step is in `origin/oldmaster:docs/RELEASING.md`.

What survives of it is one lesson, and it applies to the one history now.

**Read CI before the tag.** The old branch push was "the rehearsal": it fired `ci.yml` in
the public repository, which is the only place a release is checked against the tree it
ships — a public checkout, on four Python versions, with dependencies resolved fresh
rather than from the development venv — and it found a defect on every new release it ran
for. 0.2.1 found four tests reading paths the public tree does not carry. 0.5.0 found two
more that were not reproducible in the development tree:

* `conftest.IS_INTERNAL_TREE` was the presence of `docs/`, and 0.5.0 added `docs/reference/`
  to `PUBLISHED`, so the marker read True in the public tree. (That marker, and
  `NOT_PUBLISHED`, were retired on 2026-10-12; `tests/test_readme_links.py` says so.)
* `tests/test_doc_drift.py` counted operations off `app.routes`, which
  `iter_mounted_api_routes` forbids at `jmfts_core/rest/wiring.py`. The development venv
  pinned fastapi 0.135.3 and CI resolved 0.141.1, where `include_router` leaves a wrapper
  in that list: same app, 119 against 8.

The same CI now runs on every push to `master` (`ci.yml`: `push: branches: [master]`), and
`publish.yml` calls it as a job dependency, so a tag that has not passed it cannot publish.
That does not make a green push a rehearsed release. Push the bump commit to **both
remotes**, one `git push` per remote, both fast-forward; then print local, `origin` and
`github` and confirm all three are the same sha, because "the push printed no error" and
"the remotes agree" are different claims. Verify what the remote holds rather than what
the command printed:

```bash
git ls-remote --heads --tags github
```

> **Added 2026-10-17, from 0.6.2.** The tag went out after the merged CI run had failed:
> the failure was read only after `publish.yml` was green. **Before pushing the tag, read
> the CI result of the commit BEFORE the bump as well as the bump's own run.** A re-run that
> passes is not a refutation of a failure; record it, fix the test, and do not add a retry.

### 4. Build and check

Build **all three** distributions to a directory outside the repository. Build the client
first: `jmfts` depends on it, so a smoke install of the server needs it available.

> **Corrected 2026-10-17.** "Build **both** distributions", with no `jmfts-web` line.

```bash
DIST=$(mktemp -d)
python -m build --sdist --wheel --outdir "$DIST" ./jmfts-client
python -m build --sdist --wheel --outdir "$DIST" ./jmfts-web
python -m build --sdist --wheel --outdir "$DIST" .
twine check --strict "$DIST"/*
```

`twine check` reads metadata; it does not tell you the package works. Install the wheels
into a throwaway virtualenv and run the entry points:

```bash
python3 -m venv /tmp/smoke
/tmp/smoke/bin/pip install --find-links "$DIST" 'jmfts[web]'
/tmp/smoke/bin/pip list | grep -i jmfts     # all three, at the same version
/tmp/smoke/bin/jmfts-init-db --help
/tmp/smoke/bin/jmfts-server --help
/tmp/smoke/bin/jmfts-worker --help
/tmp/smoke/bin/jmfts-batch-worker --help
```

Do not pass `--no-index`: the JMFTS wheels are local, their dependencies are not.

> **Corrected 2026-10-17.** The install was `jmfts` and the check was "both", with two
> entry points. `[web]` is what pulls the third wheel; the console scripts are the four in
> `pyproject.toml`'s `[project.scripts]`. `--help` is checked for `jmfts-init-db` and
> `jmfts-server` by `tests/test_packaging.py`; `jmfts-worker --help` and `jmfts-batch-worker --help` were run from the development venv on
> 2026-10-17 and both print usage; not from a built wheel.

The `pip list` line is the check that lockstep actually resolved. `jmfts` pins
`jmfts-client==<version>` and `jmfts[web]` pins `jmfts-web==<version>`, so pip must find
those exact wheels in `$DIST`; two different numbers there means the bump missed a location
and the wheels were built from a tree that disagreed with itself.

`jmfts-server --help` did not always print help and exit — it ignored the flag and bound a
port. `tests/test_packaging.py::test_the_server_entry_point_answers_help_without_binding`
keeps it honest.

Check the client separately, in its OWN clean virtualenv. This is the check that catches a
contract module importing something the client does not declare — a source checkout hides
it, because the server's dependencies are sitting in the same environment:

```bash
python3 -m venv /tmp/smoke-client
/tmp/smoke-client/bin/pip install --find-links "$DIST" jmfts-client
/tmp/smoke-client/bin/pip list          # httpx, pydantic and what they pull; no sqlalchemy, no numpy
/tmp/smoke-client/bin/python -c "
from jmfts_client import RemoteJmftsClient
from jmfts_client.contracts import DocumentCreate
print(len([n for n in dir(RemoteJmftsClient) if not n.startswith('_')]), 'verbs')"
```

That last command must be run from a directory that is NOT the repository root. From the
root, `jmfts_core/` is on `sys.path` and the isolation you are testing is not real.

Check that the data files are inside the installed packages. A wheel missing them installs
cleanly and dies the first time anyone uses it. `jmfts` ships its DDL as package data and
`jmfts-web` ships `static/**/*`:

```python
import importlib.resources as r, jmfts_core.sql, jmfts_web
r.files(jmfts_core.sql).joinpath("schema.sql").is_file()
len(list(r.files(jmfts_core.sql).joinpath("migrations").iterdir()))
jmfts_web.static_dir()          # raises FileNotFoundError, by design, if the bundle is absent
```

> **Corrected 2026-10-17.** The old text quoted "`schema.sql` plus 11 migrations" at 0.1.1;
> that number is not repeated, and the `jmfts_web` line is new. `ls
> jmfts_core/sql/migrations/*.sql | wc -l` reads the migration count.

`./scripts/check_base_install.sh` separately asserts that a base install — no torch, no
office readers — still measures text and refuses to embed with a named error. It answers a
question the suite structurally cannot.

### 4b. Rehearse on TestPyPI

> **Read 2026-10-17: this step was run once, at 0.2.0, and is not run for each release.**
> `publish.yml` has published unaided since 0.3.0 and its `workflow_dispatch` run is the
> rehearsal for the build half. It is kept for the next time a name is bootstrapped some
> other way than a pending publisher, and the package list in it has been replaced, below,
> because the dependency list it carried had gone stale.

Do the whole bootstrap on `test.pypi.org` first. It is a separate account with separate
tokens and a separate namespace, so a mistake there costs a version number on an index
nobody installs from.

Create an **account-scoped** token — project-scoped is impossible when the project does
not exist yet — upload the client first, then the server, then **revoke the token**. It
is needed for one command and its whole value to an attacker is the window it stays alive.

```bash
export TWINE_USERNAME=__token__
read -rs TWINE_PASSWORD && export TWINE_PASSWORD   # not on the command line, not in history
twine upload --repository testpypi dist/jmfts_client-0.2.0*
twine upload --repository testpypi dist/jmfts-0.2.0*
unset TWINE_PASSWORD
```

**Verifying the upload is where this gets a trap.** The obvious command is wrong:

```bash
# WRONG. Do not use this.
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ jmfts
```

`--extra-index-url` does not mean "fall back to". pip pools both indexes and takes the
highest version it finds, and TestPyPI is full of squatted placeholders sitting on
version numbers no real project has reached. Measured 2026-08-22: TestPyPI's newest
`fastapi` is **1.0**, sdist-only, against real PyPI's 0.141.1. pip picked the placeholder,
tried to build it, and the install died in a subprocess with an error that reads like a
`fastapi` bug and is nothing of the kind.

Split the two questions instead — dependencies from the real index, the artifacts under
test from TestPyPI and nowhere else:

```bash
python -m venv /tmp/tp && /tmp/tp/bin/pip install --upgrade pip
# every Requires-Dist from the built wheel except the jmfts-* pins, read off the wheel:
unzip -p "$DIST"/jmfts-*.whl '*/METADATA' | grep '^Requires-Dist' | grep -v 'jmfts-'
# install those from https://pypi.org/simple/, then the artifacts under test alone:
/tmp/tp/bin/pip install --no-deps --index-url https://test.pypi.org/simple/ \
    jmfts-client jmfts-web jmfts
```

> **Corrected 2026-10-17.** The old text hard-coded the dependency list (it named
> `transformers` and `markdownify`, among others), which no longer matches
> `pyproject.toml`: the model stack is now the `embed` extra. Reading it off the wheel
> cannot go stale. `jmfts-web` was added to the install line.

Then run the step 4 checks against that venv: versions equal, entry points exit 0,
`schema.sql` plus every migration present, the operation count, and both
`ModelStackNotInstalled` and `OfficeStackNotInstalled` naming their extra. Read the counts
off the tree being released — a number written into a runbook goes stale silently:

```bash
ls jmfts_core/sql/migrations/*.sql | wc -l
python -c "import jmfts_core.rest.main, jmfts_core.registry as r; print(len(r.REGISTRY))"
```

At 0.2.0 all of that passed, and the projects went live at
`test.pypi.org/project/jmfts/0.2.0/` and `.../jmfts-client/0.2.0/`.

### 5. Publish

**This step has published at 0.3.0, 0.5.0 and the 0.6.x series, and failed once, at 0.2.1.**
Read the whole of it before pushing a tag: the tag push is what fires the workflow, and it
is the last moment at which a version number is still free. **It is the irreversible
step, and the only one.** A branch push can be re-pointed; a PyPI version can never be
reused. Say that plainly when asking for the go-ahead.

The mechanism is PyPI Trusted Publishing (OIDC) from a workflow in the public
repository, with no API token anywhere. `.github/workflows/publish.yml` triggers on a
`v*` tag push, requires the whole of `.github/workflows/ci.yml` first, builds each entry
in its `dist` matrix (three: `jmfts`, `jmfts-client`, `jmfts-web`), and uploads only what
`twine check --strict` passed. A `workflow_dispatch` run does everything up to the upload
and then stops, which is the rehearsal — and that rehearsal has passed (run
`32593677019`, 2026-08-22).

```bash
git tag -a v0.7.0 -m "0.7.0"
git push github v0.7.0        # THIS is what fires publish.yml
```

The tag goes to `github`. `publish.yml` compares the tag against `__version__` in every
matrix entry before building, so a tag that disagrees with any of the three fails before
anything is uploaded.

> **Corrected 2026-10-17.** "This step has run three times and published once, at 0.3.0",
> and "both distributions" throughout the passages below, which are kept as written for the
> events they describe.

#### What the bootstrap left behind, and what it costs now

Trusted Publishing cannot create a project that has never been published *by a tag* unless
the name is registered as a *pending* publisher. A pending publisher is registered under the
account, and "will create the project when used for the first time"
(docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/, read 2026-10-12). PyPI
allows exactly one pending row per configuration — the second distribution registered
against the same (owner, repository, workflow filename, environment) is refused with "A
pending trusted publisher matching this configuration has already been registered for a
different project name." **New names are therefore bootstrapped one release at a time.**

At 0.2.0 that was not known, and both names were uploaded by hand with an account-scoped
token, client first. That worked: `pypi.org/project/jmfts/0.2.0` and
`.../jmfts-client/0.2.0` are live.

**A pending publisher is consumed by the upload that creates the project, and a hand
upload does not consume it.** It stays pending, matching nothing, while the project it
names now exists — and a *pending* row cannot authorise an upload to an *existing*
project. That is what failed at 0.2.1. Run `32791276107`, job "Publish to PyPI",
2026-08-24: CI green on six jobs, both distributions built and `twine check --strict`
clean, and then

```
Trusted publishing exchange failure:
Token request failed: the server refused the request for the following reasons:
  * `invalid-pending-publisher`: valid token, but project already exists
```

The claims in that log are the ones the workflow is supposed to present, and they are
right: `repository: jmccardle/jmfts`, `workflow_ref: …/publish.yml@refs/tags/v0.2.1`,
`environment: pypi`. Nothing in this repository is misconfigured. The row on PyPI is on
the wrong side of the project's creation.

#### The fix, which is on PyPI and not here

For **each** of the then-two projects, at `pypi.org/manage/project/<name>/settings/publishing/`:

1. Add a publisher under the project — owner `jmccardle`, repository `jmfts`, workflow
   `publish.yml`, environment `pypi`. Those four are the identity; the environment field
   must be filled in, because `publish-pypi` declares `environment: name: pypi` and a
   blank field there is the classic rejection.
2. Delete the leftover *pending* publisher for that name, at
   `pypi.org/manage/account/publishing/`. It can no longer match anything, and leaving it
   is what makes the next failure read like the last one.

**That fix worked, and the `v0.3.0` tag push published both wheels** — run `33789142352`,
2026-09-03: six CI jobs green, both distributions built and checked, `Publish to PyPI` in
20s including digital attestations, and `pypi.org/simple/jmfts/` went to
`['0.2.0', '0.3.0']`.

There is no way to rehearse the token exchange short of a real tag. `workflow_dispatch`
stops at `build` by design, so it proves the version check, the build and `twine check`
and proves nothing about the identity. **The first tag after a publisher change is the
test.** That costs nothing if it fails: a failed exchange uploads no file and takes no
version number, and the tag can be deleted from `github` and re-pushed once the row is
right. Only a *successful* upload is irreversible.

#### The third name, `jmfts-web`

> **Added 2026-10-17, from the maintainer's account of PyPI** (this document's author
> could not query PyPI; `publish.yml`'s header and the tag dates are what the tree says).

`jmfts-web` was bootstrapped the way the section above says a new name should be, and it is
the first one that was. Its pending publisher was registered on pypi.org and test.pypi.org
on 2026-10-12 (`publish.yml`'s header), with no other pending row against this
configuration. The `v0.6.0` tag run created the project, and **`jmfts-web` is on PyPI at
0.6.0, 0.6.1 and 0.6.2** (tags dated 2026-10-12). No hand upload was made or needed.

This supersedes two statements that were true when written and have been stale since:

* The internal release checklist said `jmfts-web` "has never been published" and told the
  releaser to hand upload a new name; it was first published at 0.6.0 and nothing was
  hand uploaded.
* `publish.yml`'s header calls the bootstrap done "for `jmfts` and `jmfts-client`" and
  v0.6.0 "its first, creating, upload". The second is what happened; the first is now
  three names.

**The other half of that rule is not retired.** For any NEW distribution, the rule stands:
register a pending publisher, do not hand upload (a hand upload does not consume the pending
row), and register one new name per release, since PyPI allows one pending row per
(owner, repo, workflow, environment).

> **To-do for the releaser of 0.7.0, before the tag, and it cannot be checked from this
> repository:** the maintainer confirms at `pypi.org/manage/project/jmfts-web/settings/publishing/`
> that the `jmfts-web` publisher now shows as an ordinary trusted publisher under the
> project (owner `jmccardle`, repository `jmfts`, workflow `publish.yml`, environment
> `pypi`), and that `pypi.org/manage/account/publishing/` holds no leftover pending row for
> it. The pending row should have been consumed by the `v0.6.0` upload; **that has not been
> verified by anyone who wrote this document.** A release is the wrong moment to learn it
> was not.

#### After the upload

Two cache lags to expect, neither of which is a failed publish:

* `pypi.org/pypi/<name>/json` serves the old version list for minutes after the upload,
  while `pypi.org/simple/<name>/` with `Accept: application/vnd.pypi.simple.v1+json` is
  already current. At 0.6.1 the per-version page `/pypi/<name>/X.Y.Z/json` answered 200
  right after `publish.yml` went green while the project's `/json` still named the old
  version, and an image build running `pip install jmfts==0.6.1` straight after failed for
  the same reason. Ask the simple index or the per-version page, and tell anything that
  deploys right after a publish to expect the lag.
* `pip` keeps its own HTTP cache, so an install attempted *before* the upload poisons the
  next one with `Could not find a version that satisfies the requirement`. Re-run it with
  `--no-cache-dir` before believing that.

**0.2.1 is not recoverable as a PyPI release, and was not recovered.** Its tag is pushed,
its workflow run is spent, and re-running a completed run re-runs the same commit through
the same failed step. Deleting and re-pushing the tag would have published a superseded
tree, so PyPI jumps 0.2.0 → 0.3.0.

**The workflow's `dist` matrix carries all three**, which is what lockstep bought: one `v*`
tag names one number, the version check compares it against every entry, and every wheel is
built and uploaded from the same run. It also means they share one upload step, so a
publisher problem on any one project fails the exchange for the run: the 0.2.1 run died on
the first exchange and `jmfts-client` was never attempted.

**0.5.0 published on the first attempt of its tag run** (`34708873633`, 2026-09-12): gate
green including the flaky suite job, both wheels built and checked, `Publish to PyPI`
successful, and `pypi.org/simple/jmfts/` went to `['0.2.0', '0.3.0', '0.5.0']`. That the
gate passed first try is luck and not a reading — `CHANGELOG.md`'s 0.5.0 entry records 9
failures in 13 full-suite runs across two machines. When it does fail on an ANN test, the
recovery is "Re-run failed jobs" on that same tag run: `build` and `publish` then carry
forward, and nothing about the tag or the version needs redoing.

Wheels are not byte-reproducible across machines — a wheel embeds timestamps and build
paths — so the artifact on PyPI is whichever machine uploaded first. After the bootstrap,
that is CI rather than a laptop.

**"Fail Early" applies to the release itself.** There is no `continue-on-error`, no
`|| true` and no `skip-existing` in `publish.yml`, on purpose: a half-finished release must
fail loudly, because a PyPI version number can never be reused. Do not add one to get a
release through.

## Upgrading an existing appliance

Not a release step. This is what an operator does with the release, and it is written here
because nothing else in the tree said it and `jmfts-init-db` does not do it.

**`jmfts-init-db` loads `schema.sql` and never a delta.** `schema.sql` is the complete
current DDL, so a database it builds is already at the newest schema and has no delta
outstanding — its ledger says so, seeded from the fenced block. A database that already
holds data is the other case: the wheel moves, the schema does not, and the deltas in
`jmfts_core/sql/migrations/` are the difference.

Ask the target rather than remembering:

```bash
jmfts-init-db --pending        # exit 0 nothing outstanding, 1 some, 2 could not answer
```

It applies nothing. Migration names go to stdout and everything else to stderr, so
`jmfts-init-db --pending 2>/dev/null` is a bare list a script can read, and the exit code
is a `diff`-style three-way answer a deploy step can gate on without parsing anything.

Then apply what it names, in the order it names them, one at a time:

```bash
psql -h "$JMFTS_DB_HOST" -p "$JMFTS_DB_PORT" -U "$JMFTS_DB_USER" -d "$JMFTS_DB_NAME" \
     -v ON_ERROR_STOP=1 -f jmfts_core/sql/migrations/<name>
```

Each delta is one transaction and each records itself in `schema_migrations`, so re-running
`--pending` after each one is the check that it took.

**Three answers that are not "you are current", and none of them is exit 0:**

| what `--pending` says | what it means | what to do |
|---|---|---|
| `has no schema_migrations table` | the database predates migration `017` | apply `017_migration_ledger.sql` first — it creates the table, backfills `002`–`016`, and **refuses** rather than backfilling if `documents.produced_by` is missing, so it will tell you if the target is not at `016`. Then re-run. |
| `database '<name>' does not exist` | there is nothing to upgrade | run `jmfts-init-db` with no flags |
| `names migrations this package does not ship` | the database is ahead of the wheel | an older wheel is pointed at a newer database, or a migration file was renamed. This install cannot say what is outstanding and does not guess. |

**Why this section exists at all.** Every delta up to `020` is a schema change, and a
database missing one says so at the first query that needs the column. `022` changes an
*index*, and an appliance that upgrades its wheel and skips it keeps answering MaxSim
queries from an index whose k-means centroids were fitted to an empty table — 0.4542 of the
documents it ranked, no error, nothing in a log (`docs/ANN_INDEX_HEALTH.md` 5.8). The
appliance also sets `hnsw.iterative_scan`, which that database's IVFFlat index does not
read, so the setting succeeds and changes nothing. A silent failure needs something to ask,
and that is what `--pending` is.

> **Read 2026-10-17:** `jmfts_core/sql/migrations/` holds migrations through `025` at this
> date. This section's argument is unchanged by the later ones and was not re-derived.

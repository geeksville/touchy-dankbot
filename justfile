# dank-bot — dev tasks (Poetry project). See docs/plans/standalone.md.

# List available recipes.
default:
    @just --list

# Create/update the venv and install deps.
install:
    poetry install --no-interaction

# Run the pytest suite (network-free; DANKBOT_LIVE=1 enables the live smoke).
test: install
    poetry run pytest -q

# Format + lint.
lint:
    poetry run ruff format src tests
    poetry run ruff check --fix src tests

# Build the wheel + sdist into dist/.
build:
    poetry build

# Ask the LLM for a witty reply to each overheard line.
# Examples: `just reply -- --line "the bread is done baking"`
#           `just reply -- -f sim-overheard.txt`
reply *args: install
    poetry run dank-bot reply {{args}}

# Listen to the mic, transcribe, and scroll a witty reply per utterance on the pad.
# `--fake FILE` replays a text file instead (no mic, no model).
listen *args: install
    poetry run dank-bot listen {{args}}

# --- touchy-pad dependency ---------------------------------------------------
# The default dependency is the *published* touchy-pad package (see
# pyproject.toml). `use-touchy-local` clones/updates upstream and swaps in that
# checkout's host library while hacking on touchy-pad itself; `use-touchy-pypi`
# restores the published one. Both rewrite pyproject.toml + poetry.lock
# (committed files), so the way back is
# `git checkout pyproject.toml poetry.lock`.

# Clone or fast-forward https://github.com/geeksville/touchy-pad.git into
# reference/touchy-pad (git-ignored), generate the protobuf bindings upstream
# keeps out of git, then depend on that checkout's app/ host library — i.e.
# point the touchy-pad dependency at the latest upstream main.
use-touchy-local:
    #!/usr/bin/env bash
    set -euo pipefail
    checkout=reference/touchy-pad
    # Absolute: upstream's Justfile chdirs into the checkout before running.
    proto_py="$PWD/reference/.proto-venv/bin/python"
    if [ -d "$checkout/.git" ]; then
        if [ -n "$(git -C "$checkout" status --porcelain)" ]; then
            echo "use-touchy-local: $checkout has local changes; commit or stash them first" >&2
            exit 1
        fi
        git -C "$checkout" fetch --prune origin
        git -C "$checkout" checkout main
        git -C "$checkout" pull --ff-only
    else
        mkdir -p reference
        git clone https://github.com/geeksville/touchy-pad.git "$checkout"
    fi
    # An editable install from a clone needs the protoc-generated bindings
    # (app/src/touchy_pad/_proto/*_pb2.py), which upstream gitignores and only
    # generates for releases. Build them with upstream's own recipe, feeding it a
    # throwaway venv that has grpcio-tools (so dank-bot's dependencies stay lean).
    if [ ! -x "$proto_py" ]; then
        python3 -m venv reference/.proto-venv
    fi
    if ! "$proto_py" -c 'import grpc_tools' >/dev/null 2>&1; then
        "$proto_py" -m pip --quiet install grpcio-tools
    fi
    SYS_PYTHON="$proto_py" just --justfile "$checkout/Justfile" build-proto-py
    poetry add --no-interaction --editable "$checkout/app"
    poetry install --no-interaction

# Point the touchy-pad dependency back at the published PyPI package (the default).
# NB: the long-quip font step-down needs stage lb26 (`SetPropertyCmd.font_size_value`,
# the commit after v0.3.12). On an older host library that entry goes out as an
# `int_value`, which the lb26 firmware refuses — the quip still shows, at full size.
use-touchy-pypi:
    #!/usr/bin/env bash
    set -euo pipefail
    poetry add --no-interaction "touchy-pad@>=0.3.12"
    poetry install --no-interaction

# List the models the remote server has pulled.
models *args: install
    poetry run dank-bot models {{args}}

# Bake off the quip stage: every model/params in the packaged matrix against the
# packaged overheard corpus, writing a Markdown report plus a JSON sidecar
# (default jokemark.md + jokemark.json).
# Examples: `just jokemark`; `just jokemark -- --dry-run`;
#           `just jokemark -- --models mine.toml -o report.md`;
#           `just jokemark -- --playback jokemark.json` (no server; pad optional)
jokemark *args: install
    poetry run dank-bot jokemark {{args}}

# --- release -----------------------------------------------------------------

# Bump the version, commit, tag it "vX.Y.Z", and push both the commit and the
# tag to origin. Bumps the patch component of the semver in pyproject.toml and
# in src/dankbot/__init__.py, or use the given version (e.g. `just bump-version
# 0.2.0`). Requires a clean git index on the branch being pushed.
bump-version VERSION="":
    #!/usr/bin/env bash
    set -euo pipefail
    ver=$(sed -n 's/^version = "\([0-9][0-9.]*\)"/\1/p' pyproject.toml | head -n1)
    if [ -z "$ver" ]; then
        echo "bump-version: could not read version from pyproject.toml" >&2
        exit 1
    fi
    if [ -n "{{VERSION}}" ]; then
        new_ver="{{VERSION}}"
    else
        IFS='.' read -r major minor patch <<< "$ver"
        new_ver="$major.$minor.$((patch + 1))"
    fi
    # pyproject.toml (the [tool.poetry] version line only — not dependency specs)
    sed -i "s/^version = \"[0-9][0-9.]*\"/version = \"${new_ver}\"/" pyproject.toml
    # ... and the runtime __version__ (the CLI's --version reads this).
    sed -i "s/^__version__ = \"[0-9][0-9.]*\"/__version__ = \"${new_ver}\"/" src/dankbot/__init__.py
    echo "Bumped: $ver → $new_ver"
    git add pyproject.toml src/dankbot/__init__.py
    git commit -m "chore: bump version to v${new_ver}"
    git tag "v${new_ver}"
    git push
    git push origin "v${new_ver}"
    echo "✓ Tagged and pushed v${new_ver}"

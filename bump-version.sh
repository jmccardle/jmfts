#!/usr/bin/env bash
#
# bump-version.sh — set the JMFTS release version.
#
# Ported from the τ monorepo's script of the same name (agent-harness-py), because the two
# projects release the same way and this repo now has the same problem: one number written
# in more than one place, and no way to notice a missed copy until a wheel is on PyPI.
#
# THE THREE DISTRIBUTIONS RELEASE IN LOCKSTEP: one number, three wheels. That is a
# decision, and it is the reason this script takes a single argument. `jmfts-client` is
# GENERATED from the server's exposed surface and `jmfts-web` is WRITTEN against it, so the
# three are one product with one release; versioning them apart bought nothing and cost a
# tag namespace, a matrix filter in publish.yml, and a version check that could not be
# written honestly.
#
# The five places the number lives:
#   * jmfts_core/__init__.py            __version__  — the server wheel's version
#   * jmfts-client/…/__init__.py        __version__  — the client wheel's version
#   * jmfts-web/jmfts_web/__init__.py   __version__  — the front end wheel's version
#   * pyproject.toml                    the `jmfts-client==<version>` pin
#   * pyproject.toml                    the `jmfts-web==<version>` pin, in the `web` extra
#
# Both pyproject.toml files read their `__version__` through [tool.setuptools.dynamic], so
# those two literals ARE the wheels' versions. NEVER add a `version = "..."` literal to a
# pyproject.toml to fix a mismatch — tests/test_packaging.py forbids the second copy.
#
# The pin is the one this repo cannot make dynamic: a requirement string has nowhere to
# read a version from. It is also the one a manual bump forgets, which is why it is
# discovered by pattern here rather than listed.
#
# This script ONLY edits files. It does not add, commit, tag, or push: the release
# decision, and the commit message that records it, stay with a human.
#
# Usage:  ./bump-version.sh 0.2.0
#
set -euo pipefail

cd "$(dirname "$0")"

usage() {
    echo "usage: ./bump-version.sh <version>      e.g. ./bump-version.sh 0.2.0" >&2
    echo "       sets all three distribution versions and both in-repo pins." >&2
    echo "       Edits files only — no commit, no tag, no push." >&2
}

# -- the argument ----------------------------------------------------------
#
# An unvalidated argument reaches sed as a pattern and three files as content, so a typo
# ("0.2.0 " / "v0.2.0" / "--help") would be written into the tree as a version and only
# surface at build time. Require a PEP 440 release number, with the pre/post/dev suffixes
# a JMFTS release candidate would actually use.

if [ "$#" -ne 1 ]; then
    usage
    exit 2
fi

NEW="$1"
if ! [[ "$NEW" =~ ^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$ ]]; then
    echo "bump-version.sh: '$NEW' is not a version this repo releases." >&2
    echo "bump-version.sh: expected N.N.N, optionally aN/bN/rcN, .postN, .devN" >&2
    usage
    exit 2
fi

# -- the working tree ------------------------------------------------------
#
# In-place edits across three files are only safe because `git checkout` undoes them.
# That escape hatch stops working if the bump lands on top of edits nobody has committed
# yet, so refuse rather than mix the two.
#
# Untracked files are deliberately not counted: `git checkout` never touches them, so a
# bump cannot lose them.

if ! DIRTY="$(git status --porcelain --untracked-files=no 2>/dev/null)"; then
    echo "bump-version.sh: not a git repository (or git is unavailable)." >&2
    echo "bump-version.sh: the dirty-tree guard cannot run, so neither will the bump." >&2
    exit 1
fi
if [ -n "$DIRTY" ]; then
    echo "bump-version.sh: refusing to bump — the working tree has uncommitted changes:" >&2
    echo "$DIRTY" >&2
    echo "bump-version.sh: commit them first, so a bad bump is one 'git checkout' away." >&2
    exit 1
fi

# -- what gets edited ------------------------------------------------------
#
# The two version literals are STRUCTURAL and named here, because their absence is a fact
# about the tree this script must not guess at: a distribution that lost its __version__
# is a broken build, not a file to skip.

VERSION_FILES=(
    "jmfts_core/__init__.py"
    "jmfts-client/jmfts_client/__init__.py"
    "jmfts-web/jmfts_web/__init__.py"
)

# The pins, by contrast, are DISCOVERED. There are two today — `jmfts-client` in the base
# dependencies and `jmfts-web` in the `web` extra — and this pattern found the second one
# the day it was added without anybody editing this script, which is the property it was
# written for. A hardcoded list would have missed it silently.
#
# The name alternation is deliberate and is NOT `jmfts-[a-z]+`: a pin on some future
# third-party package beginning `jmfts-` is not necessarily part of this lockstep, and a
# pattern that swept it up would rewrite a version this repository does not own.
PIN_FILES=(pyproject.toml)
PIN_PATTERN='"jmfts-(client|web)(\[[a-z,]+\])?==[^"]+"'

# -- record the "before" ---------------------------------------------------
#
# Read the old value per file before touching anything, so the report shows a real
# transition and so a file with no version line is caught here rather than being silently
# left behind by a sed that matches nothing.

declare -A OLD_OF
for f in "${VERSION_FILES[@]}"; do
    old="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$f")"
    if [ -z "$old" ]; then
        echo "bump-version.sh: no '__version__ = \"…\"' line in $f" >&2
        exit 1
    fi
    OLD_OF["$f"]="$old"
done

# Count the pins now, so the post-edit sweep can prove it saw the same set. A pin that
# vanishes mid-run means the pattern changed under us, not that the bump worked.
PINS_BEFORE="$(grep -hoE "$PIN_PATTERN" "${PIN_FILES[@]}" | wc -l | tr -d ' ')"
if [ "$PINS_BEFORE" -eq 0 ]; then
    echo "bump-version.sh: found no in-repo pin matching $PIN_PATTERN" >&2
    echo "bump-version.sh: the pin syntax has changed; this script's pattern is stale." >&2
    exit 1
fi

# -- edit ------------------------------------------------------------------

for f in "${VERSION_FILES[@]}"; do
    sed -i "s/^__version__ = \"[^\"]*\"\$/__version__ = \"$NEW\"/" "$f"
done

# The extras bracket, where present, is part of the requirement name and must survive:
# "jmfts-client[vectors]==0.2.0", not "jmfts-client==0.2.0".
sed -i -E "s/(\"jmfts-(client|web)(\[[a-z,]+\])?)==[^\"]+\"/\1==$NEW\"/g" "${PIN_FILES[@]}"

# -- verify ----------------------------------------------------------------
#
# A sed that matches nothing exits 0. Every edit above is therefore re-read from disk and
# held against the new number; a bump that silently missed the pin is precisely the
# failure this script exists to prevent, so a miss is fatal and named rather than
# reported as success.

FAILED=()

for f in "${VERSION_FILES[@]}"; do
    grep -qx "__version__ = \"$NEW\"" "$f" || FAILED+=("$f (__version__)")
done

PINS_AFTER=0
while IFS= read -r pin; do
    PINS_AFTER=$((PINS_AFTER + 1))
    case "$pin" in
        *"==$NEW\"") ;;
        *) FAILED+=("stale pin: $pin") ;;
    esac
done < <(grep -hoE "$PIN_PATTERN" "${PIN_FILES[@]}")

if [ "$PINS_AFTER" -ne "$PINS_BEFORE" ]; then
    FAILED+=("pin count changed during the bump: $PINS_BEFORE before, $PINS_AFTER after")
fi

if [ "${#FAILED[@]}" -ne 0 ]; then
    echo "bump-version.sh: FAILED — these locations do not read $NEW:" >&2
    printf '  %s\n' "${FAILED[@]}" >&2
    echo "bump-version.sh: the tree is now half-bumped; 'git checkout -- .' to undo." >&2
    exit 1
fi

# -- report ----------------------------------------------------------------

TOTAL=$((${#VERSION_FILES[@]} + PINS_AFTER))
echo "bumped $TOTAL locations to $NEW"
echo
for f in "${VERSION_FILES[@]}"; do
    printf '  %-44s %s → %s\n' "$f" "${OLD_OF[$f]}" "$NEW"
done
grep -nE "$PIN_PATTERN" "${PIN_FILES[@]}" | while IFS= read -r line; do
    printf '  %s\n' "$line"
done
echo
echo "no commit, no tag, no push — that is yours to make. Verify with:"
echo "  pytest tests/test_packaging.py"

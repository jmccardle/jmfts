"""Tiers 1 and 2 of the fidelity table — what changed between two packages.

``docs/OFFICE_SPEC.md`` Part 10:

    | 1 | no-op repack, part set and bytes | stdlib | packaging bugs, member ordering, compression drift |
    | 2 | one-element edit: every other part byte-identical | stdlib | over-broad rewrites, normalizing readers |

    "Tier 2 is the specific test that holds Part 8's rule. If a one-word edit changes any
    part other than the one the anchor named, the implementation has started re-authoring,
    and the test says so before a user finds out."

This module is the comparison, and only the comparison. It does not repack anything and it
does not edit anything — the edit path is Part 11 step 10 and does not exist. The tests
that use this carry a five-line rewrite of their own so the harness has something to
measure, and that rewrite is deliberately the simplest thing that could work, because the
harness is what is under test here, not it.

### Why the diff is this detailed

"The bytes differ" is not a finding, it is the start of one. A repack that is wrong is
usually wrong in one specific way, and each way has a different cause:

* a part appeared or vanished — the writer is rebuilding the part list;
* a part's content changed — either the intended edit, or a reader that normalised
  something on the way through;
* the *order* changed — OPC does not require an order, but Word writes one, and a package
  whose members are alphabetised has been through a rebuild;
* the compression method changed — the tell of decompress-then-recompress, which is also
  where a stored-vs-deflate difference in the source is silently erased;
* member metadata changed — timestamps, unix mode, comments, the extra field.

:class:`PackageDiff` reports all five separately, so a failure says which one happened.
:func:`assert_only_changed` is the tier-2 assertion built on top: *these parts and no
others*.

### Duplicate members

Members are read by ``ZipInfo``, never by name, and kept in archive order. A package with
two ``word/document.xml`` entries is a corpus fixture, so a comparison that silently
collapsed them would be blind to exactly the file that was built to be looked at. That is
also why the diff is keyed by ``(name, ordinal)`` rather than by name.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass, field

#: A member's identity within one archive: its name, and which copy of that name it is.
MemberKey = tuple[str, int]


@dataclass(frozen=True)
class MemberView:
    """One member, reduced to everything that can differ between two packages."""

    key: MemberKey
    position: int
    content_sha256: str
    size: int
    compress_type: int
    date_time: tuple[int, int, int, int, int, int]
    external_attr: int
    internal_attr: int
    create_system: int
    flag_bits: int
    extra: bytes
    comment: bytes

    @property
    def name(self) -> str:
        return self.key[0]

    @property
    def metadata(self) -> tuple:
        """Everything except the content and the position."""
        return (
            self.compress_type,
            self.date_time,
            self.external_attr,
            self.internal_attr,
            self.create_system,
            self.flag_bits,
            self.extra,
            self.comment,
        )


class UnreadablePackage(Exception):
    """The bytes are not a ZIP that can be read member by member.

    Raised rather than returned. A corpus fixture that cannot be opened is either a
    ``must-reject`` fixture, in which case the caller wanted the exception, or a bug —
    and a diff function that answered "no differences" for two unreadable archives would
    report the bug as a pass.
    """


def read_members(data: bytes) -> list[MemberView]:
    """Every member of ``data``, in archive order, duplicates preserved."""
    seen: dict[str, int] = {}
    views: list[MemberView] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for position, info in enumerate(archive.infolist()):
                ordinal = seen.get(info.filename, 0)
                seen[info.filename] = ordinal + 1
                payload = archive.read(info)
                views.append(
                    MemberView(
                        key=(info.filename, ordinal),
                        position=position,
                        content_sha256=hashlib.sha256(payload).hexdigest(),
                        size=len(payload),
                        compress_type=info.compress_type,
                        date_time=info.date_time,
                        external_attr=info.external_attr,
                        internal_attr=info.internal_attr,
                        create_system=info.create_system,
                        flag_bits=info.flag_bits,
                        extra=info.extra,
                        comment=info.comment,
                    )
                )
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as exc:
        raise UnreadablePackage(str(exc)) from exc
    return views


@dataclass(frozen=True)
class PackageDiff:
    """The five ways two packages can differ, reported separately."""

    added: tuple[MemberKey, ...] = ()
    removed: tuple[MemberKey, ...] = ()
    content_changed: tuple[MemberKey, ...] = ()
    metadata_changed: tuple[MemberKey, ...] = ()
    order_changed: bool = False
    archive_comment_changed: bool = False
    bytes_identical: bool = False
    #: ``key -> (before, after)`` for every member whose metadata moved, so a failure
    #: message can say *what* moved rather than only that something did.
    metadata_detail: dict[MemberKey, tuple[tuple, tuple]] = field(default_factory=dict)

    @property
    def changed_parts(self) -> tuple[MemberKey, ...]:
        """Every key that is not present-and-identical in both packages."""
        return tuple(sorted(set(self.added) | set(self.removed) | set(self.content_changed)))

    @property
    def structurally_identical(self) -> bool:
        """Same parts, same content, same order, same member metadata.

        Strictly weaker than :attr:`bytes_identical`: two archives can satisfy this and
        still differ in bytes if one uses a data descriptor or a different zlib. Tier 1
        asserts both against fixtures this repository generates, where the difference
        cannot arise; an imported third-party file is where it can, and separating the two
        claims is what lets the report say which one an import failed.
        """
        return not (
            self.added
            or self.removed
            or self.content_changed
            or self.metadata_changed
            or self.order_changed
            or self.archive_comment_changed
        )

    def describe(self) -> str:
        """A human-readable summary, for an assertion message."""
        if self.bytes_identical:
            return "identical bytes"
        lines = []
        for label, keys in (
            ("added", self.added),
            ("removed", self.removed),
            ("content changed", self.content_changed),
            ("metadata changed", self.metadata_changed),
        ):
            if keys:
                lines.append(f"{label}: {[f'{n}#{o}' for n, o in keys]}")
        for key, (before, after) in sorted(self.metadata_detail.items()):
            lines.append(f"  {key[0]}#{key[1]} metadata {before} -> {after}")
        if self.order_changed:
            lines.append("member order changed")
        if self.archive_comment_changed:
            lines.append("archive comment changed")
        if not lines:
            lines.append("structurally identical, bytes differ")
        return "; ".join(lines)


def diff_packages(before: bytes, after: bytes) -> PackageDiff:
    """What changed between two ZIP packages. Raises if either will not open."""
    before_members = read_members(before)
    after_members = read_members(after)
    left = {view.key: view for view in before_members}
    right = {view.key: view for view in after_members}

    added = tuple(sorted(set(right) - set(left)))
    removed = tuple(sorted(set(left) - set(right)))
    shared = sorted(set(left) & set(right))

    content_changed = tuple(
        key for key in shared if left[key].content_sha256 != right[key].content_sha256
    )
    metadata_changed = tuple(key for key in shared if left[key].metadata != right[key].metadata)
    metadata_detail = {key: (left[key].metadata, right[key].metadata) for key in metadata_changed}
    order_changed = [view.key for view in before_members] != [view.key for view in after_members]

    with zipfile.ZipFile(io.BytesIO(before)) as archive:
        before_comment = archive.comment
    with zipfile.ZipFile(io.BytesIO(after)) as archive:
        after_comment = archive.comment

    return PackageDiff(
        added=added,
        removed=removed,
        content_changed=content_changed,
        metadata_changed=metadata_changed,
        order_changed=order_changed,
        archive_comment_changed=before_comment != after_comment,
        bytes_identical=before == after,
        metadata_detail=metadata_detail,
    )


def assert_only_changed(before: bytes, after: bytes, allowed: set[str]) -> PackageDiff:
    """Tier 2. ``allowed`` names the parts the edit was asked to touch — and no others.

    Everything else must be byte-identical *as a member*: same content, same compression,
    same timestamp, same mode, same position. An edit that reordered the archive or
    re-deflated an untouched part has re-authored the package, whatever its text says.

    Member metadata must be unchanged **everywhere, including on the named part**. Nothing
    in the metadata tuple is derived from the content — the size and the CRC are not in it
    — so an edit has no reason to move any of it, and a compression method that changed on
    the part that was edited is the same normalising writer the check is looking for.

    Returns the diff so a caller can assert further; raises ``AssertionError`` naming the
    parts that moved when it should not have.
    """
    diff = diff_packages(before, after)
    unexpected = sorted(
        {key for key in diff.changed_parts if key[0] not in allowed} | set(diff.metadata_changed)
    )
    if unexpected or diff.order_changed or diff.archive_comment_changed:
        raise AssertionError(
            f"the edit named {sorted(allowed)} and the package changed beyond it: "
            f"{diff.describe()}"
        )
    if not diff.changed_parts:
        raise AssertionError(
            f"the edit named {sorted(allowed)} and nothing changed; the rewrite did not "
            "take, and a tier-2 pass on an edit that did nothing is the failure this "
            "check exists to prevent"
        )
    return diff

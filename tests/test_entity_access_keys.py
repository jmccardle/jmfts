"""Entities are keyed by access, not by tree position (``SPRINT_0_3_0.md`` 7.5).

The defect this closes is 13.9, and it was live on master: a fact extracted from a
RESTRICTED document was world-readable. ``TripleRepository.query_triples`` scopes a fact by
the readability of its endpoints, and entity resolution ran over every entity node in the
store with no access filter — so a restricted document's subject and object resolved to
nodes that already existed as public, the triple was written between them, and everyone
could read it. The rule that was the whole of the protection (a NEW entity node goes under
its source document) never ran, because nothing was created.

``test_a_fact_from_a_restricted_document_is_not_world_readable`` is that scenario, and it
fails against the pre-7.5 resolver.
"""

from contextlib import contextmanager

import pytest

from jmfts_core.access import access_key, access_key_text, can_read
from jmfts_core.entity_roots import (
    ENTITIES_ROOT_USETYPE,
    ENTITY_USETYPE,
    get_or_create_entities_root,
)
from jmfts_core.fact_extraction import MENTIONS_LINK_TYPE, resolve_entity
from jmfts_core.graph_analysis import (
    RBAC_COREF_LINK_TYPE,
    SAME_AS_LINK_TYPE,
    resolve_coreferent_ids,
)
from jmfts_core.models.document import Document
from jmfts_core.models.entity_root import EntityRoot
from jmfts_core.models.principal import AccessGrant, Principal as PrincipalModel
from jmfts_core.principal_context import OWNER, CurrentPrincipal, reset_principal, set_principal
from jmfts_core.repositories.document import DocumentRepository
from jmfts_core.repositories.triple import TripleRepository

THRESHOLD = 0.8


@contextmanager
def _as(principal):
    token = set_principal(principal)
    try:
        yield
    finally:
        reset_principal(token)


def _principal(session, name):
    row = PrincipalModel(name=name)
    session.add(row)
    session.flush()
    return CurrentPrincipal(id=row.id, name=name, is_owner=False)


def _doc(session, title, content="body text for this node", parent_id=None):
    doc = DocumentRepository(session).create(
        title=title, content=content, parent_id=parent_id, auto_embed=False
    )
    session.flush()
    return doc


def _grant(session, doc, principal, level="read"):
    session.add(AccessGrant(document_id=doc.id, principal_id=principal.id, level=level))
    session.flush()


# ── piece 1: the access key ─────────────────────────────────────────────────


def test_access_key_of_an_ungoverned_document_is_empty(db_session):
    doc = _doc(db_session, "Public")
    assert access_key(db_session, doc) == ()
    assert access_key_text(()) == ""


def test_access_key_is_inherited_down_the_path(db_session):
    alice = _principal(db_session, "alice")
    root = _doc(db_session, "Restricted root")
    child = _doc(db_session, "Child", parent_id=root.id)
    _grant(db_session, root, alice, "read")

    assert access_key(db_session, child) == ((alice.id, "read"),)
    assert access_key_text(access_key(db_session, child)) == f"{alice.id}:read"


def test_access_key_takes_the_highest_level_per_principal(db_session):
    alice = _principal(db_session, "alice")
    root = _doc(db_session, "Root")
    child = _doc(db_session, "Child", parent_id=root.id)
    _grant(db_session, root, alice, "read")
    _grant(db_session, child, alice, "write")

    # Grants are additive and write implies read: one pair, the wider one.
    assert access_key(db_session, child) == ((alice.id, "write"),)


def test_access_key_is_canonical_regardless_of_grant_order(db_session):
    alice = _principal(db_session, "alice")
    bob = _principal(db_session, "bob")
    first = _doc(db_session, "First")
    second = _doc(db_session, "Second")
    _grant(db_session, first, alice, "read")
    _grant(db_session, first, bob, "write")
    _grant(db_session, second, bob, "write")
    _grant(db_session, second, alice, "read")

    assert access_key(db_session, first) == access_key(db_session, second)


# ── pieces 2 and 3: the entities root ───────────────────────────────────────


def test_one_root_per_access_not_per_access_control_root(db_session):
    """Two ACRs with identical grants share one entities root. That is the whole point."""
    alice = _principal(db_session, "alice")
    left = _doc(db_session, "Left ACR")
    right = _doc(db_session, "Right ACR")
    _grant(db_session, left, alice, "read")
    _grant(db_session, right, alice, "read")

    assert get_or_create_entities_root(db_session, left.id) == get_or_create_entities_root(
        db_session, right.id
    )


def test_a_different_access_gets_a_different_root(db_session):
    alice = _principal(db_session, "alice")
    bob = _principal(db_session, "bob")
    hers = _doc(db_session, "Hers")
    his = _doc(db_session, "His")
    public = _doc(db_session, "Public")
    _grant(db_session, hers, alice, "read")
    _grant(db_session, his, bob, "read")

    roots = {
        get_or_create_entities_root(db_session, hers.id),
        get_or_create_entities_root(db_session, his.id),
        get_or_create_entities_root(db_session, public.id),
    }
    assert len(roots) == 3


def test_the_public_root_carries_no_grants_and_is_public(db_session):
    outsider = _principal(db_session, "outsider")
    public = _doc(db_session, "Public")
    root_id = get_or_create_entities_root(db_session, public.id)
    root = db_session.get(Document, root_id)

    assert root.usetype == ENTITIES_ROOT_USETYPE
    assert root.parent_id is None
    assert db_session.query(AccessGrant).filter_by(document_id=root_id).count() == 0
    with _as(outsider):
        assert can_read(db_session, root) is True


def test_a_keyed_root_carries_exactly_the_key_as_grants(db_session):
    alice = _principal(db_session, "alice")
    outsider = _principal(db_session, "outsider")
    restricted = _doc(db_session, "Restricted")
    _grant(db_session, restricted, alice, "write")

    root_id = get_or_create_entities_root(db_session, restricted.id)
    root = db_session.get(Document, root_id)
    grants = db_session.query(AccessGrant).filter_by(document_id=root_id).all()

    assert [(g.principal_id, g.level) for g in grants] == [(alice.id, "write")]
    with _as(alice):
        assert can_read(db_session, root) is True
    with _as(outsider):
        assert can_read(db_session, root) is False


def test_get_or_create_is_idempotent(db_session):
    public = _doc(db_session, "Public")
    first = get_or_create_entities_root(db_session, public.id)
    second = get_or_create_entities_root(db_session, public.id)
    assert first == second
    assert db_session.query(EntityRoot).count() == 1


def test_a_lost_race_reuses_the_winners_root_and_leaves_no_orphan(db_session, monkeypatch):
    """Two workers reaching one new key: the loser rolls back to the winner's row.

    `entity_roots.access_key` is UNIQUE, so the second inserter gets an IntegrityError. The
    mint runs inside a SAVEPOINT precisely so the losing root document and its grants
    vanish with it — an orphan `entities` root with no registry row would be invisible to
    every later lookup and would never be reused.
    """
    import jmfts_core.entity_roots as entity_roots

    alice = _principal(db_session, "alice")
    restricted = _doc(db_session, "Restricted")
    _grant(db_session, restricted, alice, "read")
    winner = get_or_create_entities_root(db_session, restricted.id)
    docs_before = db_session.query(Document).count()

    real_lookup = entity_roots._root_id_for
    calls = {"n": 0}

    def blind_first_lookup(session, key_text):
        # Stand in for the winner's row not yet being visible to the loser.
        calls["n"] += 1
        return None if calls["n"] == 1 else real_lookup(session, key_text)

    monkeypatch.setattr(entity_roots, "_root_id_for", blind_first_lookup)
    assert get_or_create_entities_root(db_session, restricted.id) == winner
    assert calls["n"] == 2  # the blind read, then the re-read after the conflict
    assert db_session.query(Document).count() == docs_before
    assert db_session.query(EntityRoot).count() == 1


def test_an_entity_cannot_be_keyed_without_a_source_document(db_session):
    with pytest.raises(LookupError):
        get_or_create_entities_root(db_session, 10**9)


# ── pieces 6 and 7: resolution, rbac_coref, the mention link ────────────────


def test_resolution_does_not_cross_an_access_boundary(db_session):
    """The 13.9 mechanism, at the level of one name.

    A public "Acme Corp" exists. A restricted document mentions "Acme Corp". Resolution
    must NOT return the public node — it does not look there.
    """
    alice = _principal(db_session, "alice")
    public = _doc(db_session, "Public doc")
    restricted = _doc(db_session, "Restricted doc")
    _grant(db_session, restricted, alice, "read")

    public_id, created_public = resolve_entity(
        "Acme Corp", db_session, THRESHOLD, source_document_id=public.id
    )
    restricted_id, created_restricted = resolve_entity(
        "Acme Corp", db_session, THRESHOLD, source_document_id=restricted.id
    )

    assert created_public is True and created_restricted is True
    assert public_id != restricted_id
    assert db_session.get(Document, restricted_id).parent_id == get_or_create_entities_root(
        db_session, restricted.id
    )


def test_the_copies_are_joined_by_rbac_coref_not_same_as(db_session):
    alice = _principal(db_session, "alice")
    public = _doc(db_session, "Public doc")
    restricted = _doc(db_session, "Restricted doc")
    _grant(db_session, restricted, alice, "read")

    public_id, _ = resolve_entity("Acme Corp", db_session, THRESHOLD, source_document_id=public.id)
    restricted_id, _ = resolve_entity(
        "Acme Corp", db_session, THRESHOLD, source_document_id=restricted.id
    )

    repo = DocumentRepository(db_session)
    with _as(OWNER):
        links = repo.get_links(restricted_id)
    coref = [lk for lk in links if lk.link_type == RBAC_COREF_LINK_TYPE]
    assert [lk.target_id for lk in coref] == [public_id]
    assert not [lk for lk in links if lk.link_type == SAME_AS_LINK_TYPE]


def test_creation_order_does_not_matter(db_session):
    """The restricted copy first, the public copy last: still one cluster.

    Fact extraction runs in the worker, which binds no principal, so copy *k* sees every
    earlier copy regardless of who uploaded it and the walk runs in both directions.
    """
    alice = _principal(db_session, "alice")
    restricted = _doc(db_session, "Restricted doc")
    public = _doc(db_session, "Public doc")
    _grant(db_session, restricted, alice, "read")

    restricted_id, _ = resolve_entity(
        "Acme Corp", db_session, THRESHOLD, source_document_id=restricted.id
    )
    public_id, _ = resolve_entity("Acme Corp", db_session, THRESHOLD, source_document_id=public.id)

    with _as(OWNER):
        assert set(resolve_coreferent_ids(db_session, restricted_id)) == {
            restricted_id,
            public_id,
        }


def test_coreference_hides_the_copies_a_principal_cannot_read(db_session):
    """What leaks without the filter is CARDINALITY: "four copies, and I can read one"."""
    alice = _principal(db_session, "alice")
    outsider = _principal(db_session, "outsider")
    public = _doc(db_session, "Public doc")
    restricted = _doc(db_session, "Restricted doc")
    _grant(db_session, restricted, alice, "read")

    public_id, _ = resolve_entity("Acme Corp", db_session, THRESHOLD, source_document_id=public.id)
    restricted_id, _ = resolve_entity(
        "Acme Corp", db_session, THRESHOLD, source_document_id=restricted.id
    )

    with _as(alice):
        assert set(resolve_coreferent_ids(db_session, public_id)) == {public_id, restricted_id}
    with _as(outsider):
        assert resolve_coreferent_ids(db_session, public_id) == [public_id]


def test_a_mention_is_recorded_as_a_link(db_session):
    public = _doc(db_session, "Public doc")
    entity_id, _ = resolve_entity("Acme Corp", db_session, THRESHOLD, source_document_id=public.id)

    repo = DocumentRepository(db_session)
    with _as(OWNER):
        links = repo.get_links(public.id, direction="outgoing", link_type=MENTIONS_LINK_TYPE)
    assert [lk.target_id for lk in links] == [entity_id]

    # Extracting the same document twice must not abort on the unique constraint.
    resolve_entity("Acme Corp", db_session, THRESHOLD, source_document_id=public.id)
    with _as(OWNER):
        links = repo.get_links(public.id, direction="outgoing", link_type=MENTIONS_LINK_TYPE)
    assert len(links) == 1


def test_the_entity_node_is_governed_like_its_source(db_session):
    alice = _principal(db_session, "alice")
    outsider = _principal(db_session, "outsider")
    restricted = _doc(db_session, "Restricted doc")
    _grant(db_session, restricted, alice, "read")

    entity_id, _ = resolve_entity(
        "Acme Corp", db_session, THRESHOLD, source_document_id=restricted.id
    )
    entity = db_session.get(Document, entity_id)
    assert entity.usetype == ENTITY_USETYPE
    with _as(alice):
        assert can_read(db_session, entity) is True
    with _as(outsider):
        assert can_read(db_session, entity) is False


# ── 13.9, end to end ────────────────────────────────────────────────────────


def test_a_fact_from_a_restricted_document_is_not_world_readable(db_session):
    """The defect. A restricted document's fact must not be readable by an outsider.

    Both entity names already exist as PUBLIC nodes, which is the whole trap: the old
    resolver returned those nodes, both endpoints were readable, and the fact was too.
    """
    alice = _principal(db_session, "alice")
    outsider = _principal(db_session, "outsider")
    public = _doc(db_session, "Public doc", content="Alice works at Acme Corp.")
    restricted = _doc(db_session, "Restricted doc", content="Alice earns a great deal at Acme.")
    _grant(db_session, restricted, alice, "read")

    # The public corpus already knows both names.
    public_subject_id, _ = resolve_entity(
        "Alice Smith", db_session, THRESHOLD, source_document_id=public.id
    )
    resolve_entity("Acme Corp", db_session, THRESHOLD, source_document_id=public.id)

    subject_id, _ = resolve_entity(
        "Alice Smith", db_session, THRESHOLD, source_document_id=restricted.id
    )
    object_id, _ = resolve_entity(
        "Acme Corp", db_session, THRESHOLD, source_document_id=restricted.id
    )

    triples = TripleRepository(db_session)
    predicate = triples.create_predicate(name="paid_by")
    secret = triples.create_triple(
        subject_id=subject_id,
        predicate_id=predicate.id,
        object_id=object_id,
        source_document_id=restricted.id,
    )
    db_session.flush()

    with _as(alice):
        assert secret.id in {t.id for t in triples.query_triples()}
    with _as(outsider):
        assert secret.id not in {t.id for t in triples.query_triples()}
        # And not through the coreference expansion either. The outsider can read the
        # public copy of "Alice Smith", and asking for its cluster must not walk into the
        # restricted copy that IS this fact's endpoint.
        cluster = resolve_coreferent_ids(db_session, public_subject_id)
        assert cluster == [public_subject_id]
        assert secret.id not in {t.id for t in triples.query_triples(entity_ids=cluster)}

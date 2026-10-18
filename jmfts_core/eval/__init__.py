"""Retrieval evaluation against public benchmarks. ``jmfts-eval`` is the entry point.

The README publishes nDCG@10 on four BEIR datasets. Before 0.8.0 nothing in this tree could
reproduce those numbers: the harness that produced them lived in ``benchmarks/beir/`` on
the history before 0.1.0, and the 0.1.0 tree was re-rooted without it. This package is that
harness restored, and it lives under ``jmfts_core`` rather than at the repository root
because a console script in an installed wheel can only import what the wheel carries
(``[tool.setuptools.packages.find]`` in ``pyproject.toml``) — a numbers claim that needs a
source checkout to check is a weaker claim than one that runs from a release tag.

Four modules, split on what each one needs:

- ``beir_config`` — the dataset table and where the data lives. No database, no model.
- ``metrics`` — nDCG / MAP / recall / precision / MRR. Pure functions over lists and dicts.
- ``beir_loader`` — read, download, ingest, wipe. Writes through ``DocumentRepository``,
  so what is measured is what the appliance writes.
- ``beir_evaluate`` — the method grammar (``vector→crossenc@100``) and the query loop.

Nothing here imports torch at module scope, and nothing in the application imports this
package: it is reached only through ``jmfts-eval``.
"""

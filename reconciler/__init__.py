"""KnoKeep reconciler — a READ-ONLY drift observer (contract v1.4 §6, T2).

Runs on the shipped local core. Observes drift between the store, project
files, GitHub, and a tracker; REPORTS the drift, never writes any of them.

Public surface:
    sources.StoreSource     — concrete, reads keys from a StoreBackend (.read()/.list() only)
    sources.FilesSource     — concrete, reads files from a project directory (read-only)
    sources.GitHubSource    — typing.Protocol{fetch_state() -> dict}, inject a mock in tests
    sources.TrackerSource   — typing.Protocol{fetch_state() -> dict}, inject a mock in tests
    reconcile.reconcile(sources, ...) -> DriftReport
    reconcile.DriftReport / FactDrift / FactSpec

Safety invariants (see design/store_backend_contract.md §6 and
claude/knokeep-solution-design.md "6. Reconciler"):
  1. Read-only: no source type in this package exposes a write/create/update/
     delete method, and reconcile() never calls anything on a source besides
     `.fetch_state()`.
  2. Every string pulled from an external source is passed through
     `store.gate.secret_scan` BEFORE it can enter a DriftReport; a hit is
     quarantined to a label-only placeholder.
  3. Fetched text is treated as DATA, never interpreted as instructions —
     reconcile() never evals/execs/parses fetched text as code or as a
     directive to itself.
"""
from __future__ import annotations

from .reconcile import DriftReport, FactDrift, FactSpec, DEFAULT_FACT_SPECS, reconcile
from .sources import FilesSource, GitHubSource, StoreSource, TrackerSource

__all__ = [
    "DriftReport",
    "FactDrift",
    "FactSpec",
    "DEFAULT_FACT_SPECS",
    "reconcile",
    "FilesSource",
    "GitHubSource",
    "StoreSource",
    "TrackerSource",
]

#!/usr/bin/env python3
"""Bounded byte-manifest, source-copy, receipt, and disposable-restore check."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

BUNDLE = Path(__file__).resolve().parent
STORE = BUNDLE / "store"
MANIFEST = BUNDLE / "MANIFEST.sha256"
PROJECT = "q0-codex-cloud-synthetic"
RECEIPTS = {
    "data/q0-codex-cloud-synthetic/system_state": "87164697d7b3b601b59df5c38c30dc4ea3dacc336f122c864231bc37c990f53f",
    "data/q0-codex-cloud-synthetic/session_log": "fa80e5a2262bca138a5ff159cb548e30c84a654c361a09acea0b42fc6d83384e",
    "data/q0-codex-cloud-synthetic/mcp_probe": "a6f9d7a732af6adb07cc99e59691da0f771c69d71d3177824bff610377a2b48b",
}
OMITTED = ("locks/advisory.lock", "locks/cas.lock")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entries() -> list[tuple[str, int, str]]:
    result = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        sha, size, rel = line.split("  ", 2)
        result.append((sha, int(size), rel))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        help="Optional original store root for byte-for-byte source comparison.",
    )
    args = parser.parse_args()

    for sha, size, rel in entries():
        exported = BUNDLE / rel
        if not exported.is_file() or exported.stat().st_size != size or digest(exported) != sha:
            raise SystemExit(f"manifest mismatch: {rel}")
        if args.source is not None and rel.startswith("store/"):
            original = args.source / rel.removeprefix("store/")
            if not original.is_file() or original.read_bytes() != exported.read_bytes():
                raise SystemExit(f"source/copy mismatch: {rel}")

    for rel, expected in RECEIPTS.items():
        if digest(STORE / rel) != expected:
            raise SystemExit(f"receipt mismatch: {rel}")
    if any((STORE / rel).exists() for rel in OMITTED):
        raise SystemExit("transient process lock was exported")

    repo = BUNDLE.parents[3]
    with tempfile.TemporaryDirectory(prefix="knokeep-q0-verify-") as tmp:
        restored = Path(tmp) / "store"
        shutil.copytree(STORE, restored)
        proc = subprocess.run(
            [sys.executable, "skill/knokeep_state.py", "bootstrap", "--store", str(restored), "--project", PROJECT],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        payload = json.loads(proc.stdout)
        if payload.get("version_hash") != RECEIPTS[f"data/{PROJECT}/system_state"]:
            raise SystemExit("restored state receipt mismatch")
        if payload.get("log_hash") != RECEIPTS[f"data/{PROJECT}/session_log"]:
            raise SystemExit("restored log receipt mismatch")

    mode = "manifest+source+receipts+restore" if args.source else "manifest+receipts+restore"
    print(f"PASS {mode}: {len(entries())} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

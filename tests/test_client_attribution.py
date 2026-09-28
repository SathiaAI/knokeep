"""Caller-declared writer labels must survive real CLI writes and read-back."""
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill"))
import knokeep_state as ks


def cli(store, *args):
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "skill/knokeep_state.py"),
         "--store", str(store), "--project", "writer-test", *args],
        capture_output=True, text=True,
    )


def document(store, kind):
    backend = ks._backend(str(store))
    try:
        blob = backend.read(ks._key("writer-test", kind))
        assert blob is not None
        return blob, ks.parse(blob.body.decode("utf-8"))[0]
    finally:
        backend.close()


@pytest.mark.parametrize("client", ["codex-cloud", "hermes", "cursor"])
def test_init_sets_both_labels_and_noop_init_preserves_bytes(tmp_path, client):
    store = tmp_path / "store"
    assert cli(store, "init", "--client", client).returncode == 0
    before = {}
    for kind in ("state", "log"):
        blob, fm = document(store, kind)
        assert fm["client"] == client
        before[kind] = blob.body
    assert cli(store, "init", "--client", "claude-code").returncode == 0
    assert all(document(store, kind)[0].body == before[kind] for kind in before)
    resumed = json.loads(cli(store, "bootstrap").stdout)
    assert resumed["state_client"] == resumed["log_client"] == client
    assert resumed["client_labels_verified"] is False


@pytest.mark.parametrize("kind", ["state", "log"])
@pytest.mark.parametrize("section", [None, "Active State"])
def test_cli_create_update_and_stale_writer_attribution(tmp_path, kind, section):
    store = tmp_path / "store"
    body = tmp_path / "body.txt"
    body.write_text("## Active State\noriginal\n\n## Next Step\ncontinue\n", encoding="utf-8")
    created = cli(store, "flush-" + kind, "--client", "codex-cloud", "--body-file", str(body))
    assert created.returncode == 0, created.stderr
    first = json.loads(created.stdout)
    assert document(store, kind)[1]["client"] == "codex-cloud"
    body.write_text("updated" if section else "## Active State\nupdated\n", encoding="utf-8")
    scope = ["--section", section] if section else []
    updated = cli(store, "flush-" + kind, "--client", "cursor", "--body-file", str(body),
                  "--expect-hash", first["version_hash"], *scope)
    assert updated.returncode == 0, updated.stderr
    winner, fm = document(store, kind)
    assert fm["client"] == "cursor"
    assert winner.version_hash == json.loads(updated.stdout)["version_hash"]
    stale = cli(store, "flush-" + kind, "--client", "devin", "--body-file", str(body),
                "--expect-hash", first["version_hash"], *scope)
    assert stale.returncode != 0 and "conflict_parked" in stale.stderr
    after, fm = document(store, kind)
    assert after.body == winner.body
    assert fm["client"] == "cursor"
    resumed = json.loads(cli(store, "bootstrap").stdout)
    assert resumed[kind + "_client"] == "cursor"


@pytest.mark.parametrize("command", ["init", "flush-state", "flush-log"])
def test_invalid_client_cannot_create_documents(tmp_path, command):
    store = tmp_path / "store"
    body = tmp_path / "body.txt"
    body.write_text("normal content", encoding="utf-8")
    extra = [] if command == "init" else ["--body-file", str(body)]
    result = cli(store, command, "--client", "bad/identity", *extra)
    assert result.returncode != 0 and "invalid client" in result.stderr
    assert not (store / "data").exists()
    assert not (store / "journal").exists()


def test_default_and_missing_legacy_labels_are_not_invented(tmp_path):
    store = tmp_path / "store"
    empty = json.loads(cli(store, "bootstrap").stdout)
    assert empty["state_client"] is None and empty["log_client"] is None
    ks.init(str(store), "writer-test")
    assert document(store, "state")[1]["client"] == "cowork"
    blob, fm = document(store, "log")
    _, body = ks.parse(blob.body.decode("utf-8"))
    fm.pop("client")
    backend = ks._backend(str(store))
    try:
        key = ks._key("writer-test", "log")
        with ks._hold(backend, key) as lease:
            result = ks._persist(backend, key, "log", ks._bytes(fm, body), blob.version_hash, lease=lease)
        assert isinstance(result, ks.OK)
    finally:
        backend.close()
    legacy, _ = document(store, "log")
    resumed = json.loads(cli(store, "bootstrap").stdout)
    assert resumed["state_client"] == "cowork"
    assert resumed["log_client"] is None
    assert resumed["client_labels_verified"] is False
    assert document(store, "log")[0].body == legacy.body
    ks.flush_log(str(store), "writer-test", "new log", expect_hash=legacy.version_hash, client="hermes")
    assert document(store, "log")[1]["client"] == "hermes"

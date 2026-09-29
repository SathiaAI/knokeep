"""Source retrieval structural tests (synthetic fixtures only)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from application.raw_sessions import append_raw_session
from application.source_retrieval import SourceRetrievalError, retrieve_sources
from store.fake import FakeBackend
from store.types import Blob

PROJ = "demo"


def manifest_bytes(refs, manifest_id="man1", project=PROJ):
    body = {
        "schema_version": 1,
        "manifest_id": manifest_id,
        "project": project,
        "refs": refs,
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def ref_entry(
    ref_id,
    session_id,
    operation_id,
    receipt,
    document_sha256,
):
    return {
        "ref_id": ref_id,
        "session_id": session_id,
        "operation_id": operation_id,
        "document_sha256": document_sha256,
        "record_id": receipt.record_id,
        "source_sha256": receipt.source_sha256,
    }


def seed_raw(fake, session, op, text: bytes, captured="2020-01-01T00:00:00.000000Z"):
    return append_raw_session(fake, PROJ, session, "cowork", op, text, captured=captured)


class TrackingBackend:
    def __init__(self, inner):
        self.inner = inner
        self.read_keys: list[str] = []
        self.writes = 0

    def read(self, key):
        self.read_keys.append(key)
        return self.inner.read(key)

    def lock(self, *a, **k):
        return self.inner.lock(*a, **k)

    def unlock(self, *a, **k):
        return self.inner.unlock(*a, **k)

    def write(self, *a, **k):
        self.writes += 1
        return self.inner.write(*a, **k)


class SourceRetrievalTests(unittest.TestCase):
    def test_manifest_mismatch_no_io(self):
        fake = FakeBackend()
        wrapped = TrackingBackend(fake)
        mb = manifest_bytes([])
        # empty refs invalid anyway; use minimal invalid after hash check
        mb = manifest_bytes(
            [
                {
                    "ref_id": "r1",
                    "session_id": "s1",
                    "operation_id": "o1",
                    "document_sha256": "0" * 64,
                    "record_id": "1" * 64,
                    "source_sha256": "2" * 64,
                }
            ]
        )
        pin = "f" * 64
        with self.assertRaises(SourceRetrievalError) as ctx:
            retrieve_sources(wrapped, PROJ, mb, pin, "cafe")
        self.assertEqual(ctx.exception.reason, "manifest_mismatch")
        self.assertEqual(wrapped.read_keys, [])

    def test_duplicate_ref_id_invalid_no_io(self):
        fake = FakeBackend()
        wrapped = TrackingBackend(fake)
        rec = seed_raw(fake, "s1", "o1", b"cafe text")
        r = ref_entry("r1", "s1", "o1", rec, rec.current_version_hash)
        mb = manifest_bytes([r, dict(r)])
        pin = hashlib.sha256(mb).hexdigest()
        with self.assertRaises(SourceRetrievalError) as ctx:
            retrieve_sources(wrapped, PROJ, mb, pin, "cafe")
        self.assertEqual(ctx.exception.reason, "invalid_request")
        self.assertEqual(wrapped.read_keys, [])

    def test_cross_project_manifest_invalid_no_io(self):
        fake = FakeBackend()
        wrapped = TrackingBackend(fake)
        rec = seed_raw(fake, "s1", "o1", b"x")
        r = ref_entry("r1", "s1", "o1", rec, rec.current_version_hash)
        body = {
            "schema_version": 1,
            "manifest_id": "man1",
            "project": "other",
            "refs": [r],
        }
        mb = json.dumps(body, separators=(",", ":")).encode()
        pin = hashlib.sha256(mb).hexdigest()
        with self.assertRaises(SourceRetrievalError) as ctx:
            retrieve_sources(wrapped, PROJ, mb, pin, "x")
        self.assertEqual(ctx.exception.reason, "invalid_request")
        self.assertEqual(wrapped.read_keys, [])

    def test_third_token_rejected_before_io(self):
        fake = FakeBackend()
        wrapped = TrackingBackend(fake)
        rec = seed_raw(fake, "s1", "o1", b"x")
        r = ref_entry("r1", "s1", "o1", rec, rec.current_version_hash)
        mb = manifest_bytes([r])
        pin = hashlib.sha256(mb).hexdigest()
        query = " ".join(f"t{i}" for i in range(33))
        with self.assertRaises(SourceRetrievalError) as ctx:
            retrieve_sources(wrapped, PROJ, mb, pin, query)
        self.assertEqual(ctx.exception.reason, "invalid_request")
        self.assertEqual(wrapped.read_keys, [])

    def test_lexical_match_and_no_match(self):
        fake = FakeBackend()
        rec = seed_raw(fake, "s1", "o1", b"hello CAFE world")
        r = ref_entry("r1", "s1", "o1", rec, rec.current_version_hash)
        mb = manifest_bytes([r])
        pin = hashlib.sha256(mb).hexdigest()
        out = retrieve_sources(fake, PROJ, mb, pin, "cafe")
        self.assertEqual(out["outcome"], "matches")
        self.assertEqual(out["hits"][0]["score"], 1)
        self.assertEqual(out["hits"][0]["matched_query_tokens"], ["cafe"])
        out2 = retrieve_sources(fake, PROJ, mb, pin, "missing")
        self.assertEqual(out2["outcome"], "no_match")

    def test_decomposed_cafe_matches_cafe_query(self):
        fake = FakeBackend()
        # e + combining acute splits isalnum tokens; casefold on e matches cafe query token
        text = "caf\u0065\u0301".encode("utf-8")
        rec = seed_raw(fake, "s1", "o1", text)
        r = ref_entry("r1", "s1", "o1", rec, rec.current_version_hash)
        mb = manifest_bytes([r])
        pin = hashlib.sha256(mb).hexdigest()
        out = retrieve_sources(fake, PROJ, mb, pin, "cafe")
        self.assertEqual(out["outcome"], "matches")

    def test_precomposed_cafe_distinct_from_cafe_query(self):
        fake = FakeBackend()
        rec = seed_raw(fake, "s1", "o1", "caf\u00e9 only".encode("utf-8"))
        r = ref_entry("r1", "s1", "o1", rec, rec.current_version_hash)
        mb = manifest_bytes([r])
        pin = hashlib.sha256(mb).hexdigest()
        out = retrieve_sources(fake, PROJ, mb, pin, "cafe")
        self.assertEqual(out["outcome"], "no_match")

    def test_pin_mismatch(self):
        fake = FakeBackend()
        rec = seed_raw(fake, "s1", "o1", b"cafe")
        r = ref_entry("r1", "s1", "o1", rec, "0" * 64)
        mb = manifest_bytes([r])
        pin = hashlib.sha256(mb).hexdigest()
        with self.assertRaises(SourceRetrievalError) as ctx:
            retrieve_sources(fake, PROJ, mb, pin, "cafe")
        self.assertEqual(ctx.exception.reason, "evidence_pin_mismatch")

    def test_evidence_missing(self):
        fake = FakeBackend()
        mb = manifest_bytes(
            [
                {
                    "ref_id": "r1",
                    "session_id": "s1",
                    "operation_id": "o1",
                    "document_sha256": "a" * 64,
                    "record_id": "b" * 64,
                    "source_sha256": "c" * 64,
                }
            ]
        )
        pin = hashlib.sha256(mb).hexdigest()
        with self.assertRaises(SourceRetrievalError) as ctx:
            retrieve_sources(fake, PROJ, mb, pin, "cafe")
        self.assertEqual(ctx.exception.reason, "evidence_missing")

    def test_deterministic_tie_break(self):
        fake = FakeBackend()
        rec1 = seed_raw(fake, "s1", "o1", b"term")
        rec2 = seed_raw(fake, "s2", "o2", b"term")
        r1 = ref_entry("b", "s1", "o1", rec1, rec1.current_version_hash)
        r2 = ref_entry("a", "s2", "o2", rec2, rec2.current_version_hash)
        mb = manifest_bytes([r1, r2])
        pin = hashlib.sha256(mb).hexdigest()
        out = retrieve_sources(fake, PROJ, mb, pin, "term", top_k=2)
        self.assertEqual(out["hits"][0]["ref_id"], "a")
        self.assertEqual(out["hits"][1]["ref_id"], "b")

    def test_recheck_detects_body_mutation(self):
        fake = FakeBackend()
        rec = seed_raw(fake, "s1", "o1", b"find token")
        r = ref_entry("r1", "s1", "o1", rec, rec.current_version_hash)
        mb = manifest_bytes([r])
        pin = hashlib.sha256(mb).hexdigest()
        key = f"{PROJ}/raw-sessions/s1"

        class MutateOnSecondRead:
            def __init__(self, inner):
                self.inner = inner
                self.n = 0

            def read(self, key_):
                self.n += 1
                blob = self.inner.read(key_)
                if self.n > 1 and blob is not None:
                    mutated = blob.body + b"x"
                    return Blob(body=mutated, version_hash=blob.version_hash)
                return blob

            def lock(self, *a, **k):
                return self.inner.lock(*a, **k)

            def unlock(self, *a, **k):
                return self.inner.unlock(*a, **k)

            def write(self, *a, **k):
                return self.inner.write(*a, **k)

        with self.assertRaises(SourceRetrievalError) as ctx:
            retrieve_sources(MutateOnSecondRead(fake), PROJ, mb, pin, "token")
        self.assertEqual(ctx.exception.reason, "snapshot_changed")

    def test_backend_spoof_exception_redacted(self):
        fake = FakeBackend()
        rec = seed_raw(fake, "s1", "o1", b"token")
        r = ref_entry("r1", "s1", "o1", rec, rec.current_version_hash)
        mb = manifest_bytes([r])
        pin = hashlib.sha256(mb).hexdigest()

        class SpoofBackend:
            def read(self, key):
                raise SourceRetrievalError(
                    "content_blocked", ref_index=0, evil="secret"
                )

            def lock(self, *a, **k):
                return fake.lock(*a, **k)

            def unlock(self, *a, **k):
                return fake.unlock(*a, **k)

            def write(self, *a, **k):
                return fake.write(*a, **k)

        with self.assertRaises(SourceRetrievalError) as ctx:
            retrieve_sources(SpoofBackend(), PROJ, mb, pin, "token")
        self.assertEqual(ctx.exception.reason, "operational_error")
        self.assertEqual(ctx.exception.detail, {})

    def test_qualifications_and_coverage(self):
        fake = FakeBackend()
        rec = seed_raw(fake, "s1", "o1", b"alpha")
        r = ref_entry("r1", "s1", "o1", rec, rec.current_version_hash)
        mb = manifest_bytes([r])
        pin = hashlib.sha256(mb).hexdigest()
        out = retrieve_sources(fake, PROJ, mb, pin, "alpha")
        q = out["qualifications"]
        self.assertTrue(q["content_is_untrusted_data"])
        self.assertFalse(q["full_product_qualified"])
        self.assertEqual(out["coverage"]["kind"], "declared_manifest_only")
        self.assertEqual(out["coverage"]["refs_checked"], out["coverage"]["refs_declared"])


if __name__ == "__main__":
    unittest.main()


# Coordinator regression cases use development fixtures, never held-out data.
class ContractRegressions(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBackend()
        receipt = seed_raw(self.fake, "s1", "o1", "zero Straße token end".encode())
        self.ref = ref_entry("r1", "s1", "o1", receipt, receipt.current_version_hash)
        self.mb = manifest_bytes([self.ref])

    def call(self, backend=None, data=None, query="token", **kwargs):
        data = self.mb if data is None else data
        return retrieve_sources(self.fake if backend is None else backend, PROJ, data,
                                hashlib.sha256(data).hexdigest(), query, **kwargs)

    def test_invalid_request_matrix_zero_reads(self):
        invalid = [b'{', b'{"x":NaN}', b'{"x":1e999}', b'{"x":' + b'9'*100 + b'}',
                   self.mb + b' false', b'\xef\xbb\xbf' + self.mb, self.mb.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
                   self.mb.replace(b'"schema_version":1', b'"schema_version":true'), bytearray(self.mb), b' '*32769]
        for data in invalid:
            with self.subTest(data_type=type(data).__name__, length=len(data)):
                backend = TrackingBackend(self.fake)
                with self.assertRaises(SourceRetrievalError) as caught: self.call(backend, data)
                self.assertEqual(caught.exception.reason, "invalid_request")
                self.assertEqual(backend.read_keys, [])

    def test_invalid_query_and_top_k_zero_reads(self):
        for query in ["", "!!!", "x"*513, "x"*129, "\x00x", "\ud800", "x "*33]:
            backend = TrackingBackend(self.fake)
            with self.subTest(query_length=len(query)):
                with self.assertRaises(SourceRetrievalError) as caught: self.call(backend, query=query)
                self.assertEqual(caught.exception.reason, "invalid_request"); self.assertFalse(backend.read_keys)
        for top_k in [True, 0, 11, 1.0, "1"]:
            backend = TrackingBackend(self.fake)
            with self.assertRaises(SourceRetrievalError): self.call(backend, top_k=top_k)
            self.assertFalse(backend.read_keys)

    def test_duplicate_operation_and_inconsistent_document_pins(self):
        for second in [dict(self.ref, ref_id="r2"), dict(self.ref, ref_id="r2", operation_id="o2", document_sha256="0"*64)]:
            backend = TrackingBackend(self.fake)
            with self.assertRaises(SourceRetrievalError) as caught:self.call(backend, manifest_bytes([self.ref,second]))
            self.assertEqual(caught.exception.reason, "invalid_request"); self.assertFalse(backend.read_keys)

    def test_original_unicode_offsets_and_whole_token(self):
        hit=self.call(query="STRASSE")["hits"][0]
        original="zero Straße token end".encode()
        self.assertEqual(original[hit["byte_start"]:hit["byte_end"]],hit["excerpt_utf8"].encode())
        self.assertEqual(self.call(query="STRASS")["hits"],[])

    def test_budget_checks_long_token_and_separators(self):
        from unittest.mock import patch
        from application import source_retrieval as sr
        for text in ["x"*20000, " "*20000, "abc "*5000]:
            with patch.object(sr.time,"monotonic",side_effect=[0,20]):
                with self.assertRaises(SourceRetrievalError) as caught:
                    sr._score_source(text,["absent"],10,[0])
                self.assertEqual(caught.exception.reason,"budget_exceeded")

    def test_repeated_matches_compute_original_offset_once(self):
        from unittest.mock import patch
        from application import source_retrieval as sr
        with patch.object(sr,"_char_span_to_byte_span",wraps=sr._char_span_to_byte_span) as calc:
            score,_,b0,b1=sr._score_source("a "*10000,["a"],sr.time.monotonic()+10,[0])
        self.assertEqual((score,b0,b1),(1,0,1)); self.assertEqual(calc.call_count,1)

    def test_backend_error_chain_redacted(self):
        import traceback
        class Bad:
            def read(self,key): raise RuntimeError("private-backend-sentinel")
        try:self.call(Bad())
        except SourceRetrievalError as exc:
            self.assertEqual(exc.reason,"operational_error"); self.assertEqual(exc.detail,{})
            self.assertNotIn("private-backend-sentinel", ''.join(traceback.format_exception_only(type(exc),exc)))
            self.assertTrue(exc.__suppress_context__)
        else:self.fail("expected closed error")

    def test_caps_fail_closed(self):
        from unittest.mock import patch
        from application import source_retrieval as sr
        for constant in ["MAX_DOCUMENT_BYTES","MAX_CACHED_BODY_BYTES","MAX_DECODED_SOURCE_BYTES","MAX_OUTPUT_JSON_BYTES","MAX_EXCERPT_AGGREGATE_BYTES"]:
            with self.subTest(cap=constant),patch.object(sr,constant,1):
                with self.assertRaises(SourceRetrievalError) as caught:self.call()
                self.assertEqual(caught.exception.reason,"limit_exceeded")
        with patch.object(sr,"MAX_UNDERLYING_READS",1):
            with self.assertRaises(SourceRetrievalError):self.call()

    def test_scan_failure_is_operational(self):
        from unittest.mock import patch
        from application import source_retrieval as sr
        from store.types import ERROR,ErrorKind
        with patch.object(sr.gate,"validate_write",return_value=ERROR(ErrorKind.SCAN_FAILURE)):
            with self.assertRaises(SourceRetrievalError) as caught:self.call()
        self.assertEqual(caught.exception.reason,"operational_error")

    def test_source_pin_and_record_pin_are_required(self):
        for key in ["source_sha256","record_id"]:
            bad=dict(self.ref);bad[key]="0"*64
            with self.assertRaises(SourceRetrievalError) as caught:self.call(data=manifest_bytes([bad]))
            self.assertEqual(caught.exception.reason,"evidence_pin_mismatch")

    def test_application_only_reads_and_rechecks(self):
        fake=self.fake
        class ReadOnly:
            def __init__(self):self.keys=[]
            def read(self,key):self.keys.append(key);return fake.read(key)
            def __getattr__(self,name):raise AssertionError("forbidden backend call")
        backend=ReadOnly();self.call(backend)
        self.assertEqual(backend.keys,["demo/raw-sessions/s1"]*2)

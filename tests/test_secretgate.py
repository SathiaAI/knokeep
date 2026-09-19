#!/usr/bin/env python3
"""Secret-gate v3 tests: evasions (must block) + benign corpus (must allow). Run: python tests/test_secretgate.py"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill"))
from knokeep_secretgate import scan_text

MUST_BLOCK = {
    "short pw colon": "db password: Kx9$vLp2Qz",
    "env short PASSWORD=": "PASSWORD=Sup3rSecret1",
    "json password": '{"password": "s3cretValue12"}',
    "odbc Pwd=": "Driver=ODBC;Server=y;Pwd=Sup3rSecretPass;",
    "openai proj key": "OPENAI=sk-proj-EXAMPLE0000000000000",
    "stripe live": "STRIPE=sk_live_EXAMPLE0000000000000",
    "stripe whsec": "whsec_EXAMPLE00000000000000000000",
    "hf token": "HF=hf_EXAMPLE0000000000000000000",
    "github ghs": "ghs_EXAMPLE0000000000000000000000",
    "jwt none-alg": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhIn0.sig",
    "google aiza": "key AIzaSyEXAMPLE0000000000000000000000000000",
    "gitlab pat": "glpat-EXAMPLE0000000000000000",
    "putty ppk": "PuTTY-User-Key-File-3: ssh-rsa",
    "zero-width split": "sk-ant​-api03-AAAAAAAAAAAAAAAAAAAA",
    "aws akia": "AKIAEXAMPLE000000000",
    "cred uri": "postgres://demo:hunter2@localhost:5432/db",
    "getenv trick": "password=os.getenvREALSECRETVALUE",
    "bearer header": "Authorization: Bearer abcdef1234567890abcd",
    "quoted passphrase": 'password="my secret phrase"',
    "process.envabc bypass": "password=process.envabc",
}
MUST_ALLOW = {
    "region": "region: us-east-2",
    "flag true": "token: true",
    "reference form": "OPENROUTER_API_KEY (in .env, not stored)",
    "next step": "Next Step: build SKILL.md and wire handshake",
    "arch note": "Architecture: Python 3.11 stdlib CLI, no external deps",
    "getenv code": "api_key = os.getenv('API_KEY')",
    "env ref": "reads DATABASE_URL from ${DATABASE_URL}",
    "git sha": "fixed at commit abc1234def5678abc1234def5678abc1234def56",
    "placeholder": 'password: <YOUR_PASSWORD_HERE>',
    "process.env ref": "apiKey: process.env.API_KEY",
    "prose keyword": 'note: "password rotation done"',
    "basic auth prose": "Use basic authentication for the admin panel",
    "basic config prose": "basic configuration and basic understanding required",
}

fails = 0
for name, s in MUST_BLOCK.items():
    ok = bool(scan_text(s)); print(("PASS " if ok else "FAIL ") + "block: " + name + ("" if ok else "  <-- LEAK")); fails += 0 if ok else 1
for name, s in MUST_ALLOW.items():
    ok = not bool(scan_text(s)); print(("PASS " if ok else "FAIL ") + "allow: " + name + ("" if ok else "  <-- FP " + str(scan_text(s)))); fails += 0 if ok else 1

total = len(MUST_BLOCK) + len(MUST_ALLOW)
print(f"\n{total - fails}/{total} passed")
sys.exit(1 if fails else 0)

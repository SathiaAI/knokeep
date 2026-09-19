#!/usr/bin/env python3
"""KnoKeep secret gate v3 - fail-closed pre-write scanner (stdlib only).
Blocks content carrying secret VALUES; KnoKeep stores references, never values.
NOTE: best-effort defense-in-depth, NOT a guarantee that arbitrary prose is secret-free.
Usage: python knokeep_secretgate.py <path>   # dir or file; exits 1 if any secret/unscannable."""
import sys, os, re, json, math, unicodedata
from collections import Counter

_ZW = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
def normalize(t):
    return unicodedata.normalize("NFKC", t).translate(_ZW)

PREFIXES = [(re.compile(p), l) for p, l in [
    (r"sk-ant-[A-Za-z0-9_\-]{6,}", "Anthropic key"),
    (r"sk-(?:proj|svcacct)-[A-Za-z0-9_\-]{6,}", "OpenAI project/service key"),
    (r"sk-or-[A-Za-z0-9_\-]{6,}", "OpenRouter key"),
    (r"sk-[A-Za-z0-9]{16,}", "OpenAI-style sk- key"),
    (r"sk_(?:live|test)_[A-Za-z0-9]{10,}", "Stripe secret key"),
    (r"rk_(?:live|test)_[A-Za-z0-9]{10,}", "Stripe restricted key"),
    (r"whsec_[A-Za-z0-9]{16,}", "Stripe webhook secret"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub PAT"),
    (r"glpat-[A-Za-z0-9_\-]{16,}", "GitLab PAT"),
    (r"npm_[A-Za-z0-9]{30,}", "npm token"),
    (r"hf_[A-Za-z0-9]{20,}", "HuggingFace token"),
    (r"AKIA[A-Z0-9]{12,}", "AWS access key"),
    (r"ASIA[A-Z0-9]{12,}", "AWS temp key"),
    (r"AIza[A-Za-z0-9_\-]{20,}", "Google API key"),
    (r"ya29\.[A-Za-z0-9_\-]{10,}", "Google OAuth token"),
    (r"xox[baprse]-[A-Za-z0-9\-]{8,}", "Slack token"),
    (r"pypi-[A-Za-z0-9_\-]{16,}", "PyPI token"),
    (r"eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]*", "JWT"),
]]
PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|PuTTY-User-Key-File")
CRED_URI = re.compile(r"[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s:/@]+@", re.I)
# key (optionally quoted) : or = value (optionally quoted) - matches env, JSON/YAML, ODBC Pwd=
ASSIGN = re.compile(r"(?i)[\"']?(pass(?:word|wd|phrase)?|pwd|secret|api[_\- ]?key|access[_\- ]?key|"
                    r"secret[_\- ]?key|auth[_\- ]?token|token|credential|client[_\- ]?secret|private[_\- ]?key)"
                    r"[\"']?\s*[:=]\s*(?:\"([^\"\n]{3,})\"|'([^'\n]{3,})'|([^\s\"';,}]{3,}))")
PLACEHOLDER = re.compile(r"(?i)^(?:<[^>]*>|\{\{?[^}]*\}?\}|x{3,}|\*{3,}|none|null|nil|true|false|yes|no|"
                         r"enabled|disabled|tbd|todo|changeme|example|redacted|value|placeholder|"
                         r"your[_\-]?\w+|\.\.\.)$")
ENV_REF = re.compile(r"^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|%[A-Za-z_][A-Za-z0-9_]*%)$")  # value IS a var reference
GETENV = re.compile(r"(?i)^(?:os\.getenv\(|getenv\(|process\.env\b|import\.meta\.env\b)")  # value STARTS with a ref call
BEARER = re.compile(r"(?i)authoriz(?:ation|e)\s*[:=]\s*(?:bearer|basic)\s+[A-Za-z0-9+/=._\-]{8,}")  # header, not prose "basic ..."
HEXONLY = re.compile(r"(?i)^[0-9a-f]+$")
TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{20,}")
ALLOWED_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".lock", ".tmp", ".log", ""}  # a memory store holds only these
IGNORE_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".gitignore", ".gitkeep"}  # benign OS/tooling files: skip, do not flag

def shannon(s):
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values()) if n else 0.0

def scan_text(raw):
    text = normalize(raw)
    hits = []
    for pat, label in PREFIXES:
        if pat.search(text): hits.append(label)
    if PEM.search(text): hits.append("private key block")
    if CRED_URI.search(text): hits.append("credential-bearing URI")
    if BEARER.search(text): hits.append("authorization header token")
    for m in ASSIGN.finditer(text):
        val = m.group(2) or m.group(3) or m.group(4)
        if not val or PLACEHOLDER.match(val) or ENV_REF.match(val) or GETENV.search(val):
            continue
        hits.append("secret assignment (" + m.group(1).lower().replace(" ", "") + ")"); break
    for tok in TOKEN.findall(text):
        if len(tok) >= 20 and not HEXONLY.match(tok) and not tok.isdigit() and shannon(tok) > 3.8:
            hits.append("high-entropy token"); break
    return sorted(set(hits))

def scan_file(fp):
    """Fail-closed: non-text / unreadable / undecodable / oversize -> a finding, never a silent skip."""
    name = os.path.basename(fp).lower()
    if name in IGNORE_NAMES or name.endswith("~") or name.endswith(".swp"):
        return []                                          # benign OS/editor junk, not store content
    ext = os.path.splitext(fp)[1].lower()
    if ext not in ALLOWED_EXT:
        return ["non-text file in store (" + (ext or "no-ext") + ")"]
    try:
        if os.path.getsize(fp) > 1_000_000:
            return ["oversize file"]
        raw = open(fp, "rb").read(1_000_000)
    except Exception as e:
        return ["unreadable file (" + type(e).__name__ + ")"]
    for enc in ("utf-8", "utf-16"):
        try:
            return scan_text(raw.decode(enc))
        except Exception:
            continue
    return ["undecodable/binary content"]

def scan(path):
    findings = []
    if not os.path.exists(path):
        return [{"file": path, "reason": "path not found"}]
    def _onerr(e):                                          # fail-closed: an unreadable dir is a finding
        findings.append({"file": getattr(e, "filename", "?"), "reason": "unreadable directory"})
    base = path if os.path.isdir(path) else os.path.dirname(path)
    files = [path] if os.path.isfile(path) else [os.path.join(r, f) for r, _, fs in os.walk(path, onerror=_onerr) for f in fs]
    for fp in files:
        for reason in scan_file(fp):
            findings.append({"file": os.path.relpath(fp, base), "reason": reason})
    return findings

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: knokeep_secretgate.py <path>"); sys.exit(2)
    f = scan(sys.argv[1])
    print(json.dumps({"blocked": bool(f), "count": len(f), "findings": f}, indent=1))
    sys.exit(1 if f else 0)

"""Coordinator fixture correction, original Devin A2 bytes stay untouched.

On Windows the test's own comparison cannot read cas.lock via a second handle
while its simulated writer holds the byte lock. Capture the tested refusal
under lock, release that simulated writer, THEN compare all input bytes.
This changes test ordering only; analyze_and_export is unchanged.
"""
from pathlib import Path

path = Path(__file__).parent / 'recovery_probe_a2.py'
source = path.read_text(encoding='utf-8')
old = '''    try: check("concurrent-writer-holds-lock", analyze_and_export(s, out), "REFUSE_BUSY_WRITER_ACTIVE", None, s, out, before=before)
    finally: unlock(held)'''
new = '''    try: held_result = analyze_and_export(s, out)
    finally: unlock(held)
    check("concurrent-writer-holds-lock", held_result, "REFUSE_BUSY_WRITER_ACTIVE", None, s, out, before=before)'''
assert source.count(old) == 1, 'fixture source differs; inspect instead of silently patching'
source = source.replace(old, new)
exec(compile(source, str(path), 'exec'), {'__name__': '__main__', '__file__': str(path)})

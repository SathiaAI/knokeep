#!/usr/bin/env python3
"""Read-only source checkpoint and independent task verification; no store API calls.

Snapshots all ordinary files and store bytes into a NEW directory. Then executes
the predetermined verification command on another project copy. A report match
alone does not prove the source verified, captured or ordered its actions honestly.
"""
import argparse
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import struct
import sys


def manifest(root):
    root = Path(root)
    found = {}
    if not root.exists():
        return found
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            raise ValueError('Symlink is not an eligible fixture artifact')
        if p.is_file():
            body = p.read_bytes()
            found[p.relative_to(root).as_posix()] = {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
    return found


def snapshot(source, target):
    before = manifest(source)
    if Path(source).exists():
        shutil.copytree(source, target)
    else:
        Path(target).mkdir()
    after, copied = manifest(source), manifest(target)
    if before != after or before != copied:
        raise ValueError('Source changed or copy differs; checkpoint invalid')
    return {'existed': Path(source).exists(), 'files': copied}


def journal_versions(store):
    """Parse the existing length-prefixed journal without opening a backend.

    Includes historical bodies for receipt checks. A torn/invalid tail is exposed.
    """
    p = Path(store) / 'journal' / 'journal.log'
    raw = p.read_bytes() if p.exists() else b''
    stream, rows, error = io.BytesIO(raw), [], None
    while stream.tell() < len(raw):
        start = stream.tell()
        try:
            header = stream.read(4)
            is_v2 = header == b'KKJ2'
            if is_v2:
                header = stream.read(4)
            klen = struct.unpack('>I', header)[0]
            if not 0 < klen <= 1024:
                raise ValueError('invalid key length')
            key = stream.read(klen).decode('utf-8')
            blen = struct.unpack('>Q', stream.read(8))[0]
            if blen > len(raw) - stream.tell():
                raise ValueError('incomplete body')
            body, digest = stream.read(blen), stream.read(32)
            if len(body) != blen or len(digest) != 32 or hashlib.sha256(body).digest() != digest:
                raise ValueError('incomplete or invalid body/hash')
            fence = struct.unpack('>Q', stream.read(8))[0] if is_v2 else 0
            rows.append({'key': key, 'sha256': digest.hex(), 'bytes': len(body), 'offset': start, 'fence': fence})
        except (ValueError, struct.error, UnicodeDecodeError) as exc:
            error = {'offset': start, 'reason': str(exc)}
            break
    return {'versions': rows, 'tail_error': error}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--spec', required=True)
    a = ap.parse_args()
    s = json.loads(Path(a.spec).read_text(encoding='utf-8'))
    out = Path(s['output_dir'])
    out.mkdir(parents=True, exist_ok=False)
    report = {'job': s['job'], 'milestone': s['milestone']}
    report['project_snapshot'] = snapshot(s['project_dir'], out / 'project')
    report['store_snapshot'] = snapshot(s['store_dir'], out / 'store')
    report['journal'] = journal_versions(out / 'store')
    verify = out / 'verify-project'
    shutil.copytree(out / 'project', verify)
    # Remove only the copied expected output so stale files cannot pass execution.
    expected_file = verify / 'packing-report.csv'
    if expected_file.exists():
        expected_file.unlink()
    module = importlib.util.spec_from_file_location('bounded', Path(s['supervisor_dir']) / 'run_bounded.py')
    rb = importlib.util.module_from_spec(module)
    module.loader.exec_module(rb)
    rec = rb.run([s['python'], 'pack.py', 'orders.csv', 'packing-report.csv'], 45,
                 str(out / 'verification-process'), cwd=str(verify))
    rows = list(csv.reader(expected_file.open(newline='', encoding='utf-8'))) if expected_file.exists() else None
    report['verification'] = {
        'outcome': rec['outcome'], 'os_exit_code': rec['os_exit_code'],
        'rows': rows, 'expected_rows': [['sku', 'units', 'cartons']] + s['expected_rows'],
        'coordinator_task_match': rec['outcome'] == 'exited' and rec['os_exit_code'] == 0
            and rows == [['sku', 'units', 'cartons']] + s['expected_rows'],
        'source_verification_trace': 'PENDING_REVIEW', 'semantic_capture': 'PENDING_REVIEW',
        'receipt_match': 'PENDING_REVIEW', 'chronology': 'PENDING_REVIEW',
    }
    if s['milestone'] == 'm4':
        report['verification']['future_item_unimplemented'] = not (out / 'project' / 'review-queue.csv').exists()
    if manifest(s['project_dir']) != report['project_snapshot']['files'] or manifest(s['store_dir']) != report['store_snapshot']['files']:
        raise ValueError('Live source changed during independent verification')
    (out / 'checkpoint.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'milestone': s['milestone'], 'task_match': report['verification']['coordinator_task_match'],
                      'store_files': len(report['store_snapshot']['files']), 'review': 'PENDING'}))
    return 0 if report['verification']['coordinator_task_match'] else 1


if __name__ == '__main__':
    sys.exit(main())

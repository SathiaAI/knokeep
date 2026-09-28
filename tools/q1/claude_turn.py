#!/usr/bin/env python3
"""One real Claude Code turn. Private paths/prompts stay in the run directory.

The JSON spec supplies executable, python_dir, product_dir, project_dir,
store_dir, prompt_file, output_dir, supervisor_dir, session_id, model, timeout_s
and resume (boolean). Existing output is refused. No retries or model fallback.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


EXPECTED_TOOLS = ['Bash', 'Edit', 'Glob', 'Grep', 'Read', 'Write']


def native_executable(path):
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.is_file():
        raise ValueError('An existing absolute native executable path is required')
    if candidate.suffix.lower() in ('.cmd', '.bat', '.ps1') or (os.name == 'nt' and candidate.suffix.lower() != '.exe'):
        raise ValueError('Shell shims can truncate multiline prompts; use the native executable')
    return hashlib.sha256(candidate.read_bytes()).hexdigest()


def product_integrity(directory, expected_sha):
    prefix = ['git', '-c', 'safe.directory=' + str(Path(directory).resolve())]
    def query(args):
        return subprocess.run(prefix + args, cwd=directory, capture_output=True, text=True, timeout=15)
    head = query(['rev-parse', 'HEAD'])
    diff = query(['diff', '--no-ext-diff', '--quiet', 'HEAD', '--'])
    extra = query(['ls-files', '--others', '--exclude-standard'])
    return head.returncode == 0 and head.stdout.strip() == expected_sha and diff.returncode == 0 and extra.returncode == 0 and not extra.stdout.strip()


def load_supervisor(directory):
    spec = importlib.util.spec_from_file_location('bounded', Path(directory) / 'run_bounded.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def environment(python_dir):
    env = os.environ.copy()
    for key in list(env):
        upper = key.upper()
        if (any(p in upper for p in ('API_KEY', 'TOKEN', 'AUTH', 'SECRET', 'BEARER', 'PASSWORD', 'BASE_URL', 'ENDPOINT'))
                or upper.startswith(('ANTHROPIC_', 'OPENROUTER_', 'HONCHO_', 'HERMES_', 'CLAUDE_CODE_USE_'))):
            env.pop(key, None)
    env['PATH'] = str(python_dir) + os.pathsep + env.get('PATH', '')
    return env


def inspect_stream(raw, expected_session, expected_model):
    events, errors = [], []
    for number, line in enumerate(raw.decode('utf-8', errors='replace').splitlines(), 1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            errors.append(number)
    inits = [e for e in events if e.get('type') == 'system' and e.get('subtype') == 'init']
    results = [e for e in events if e.get('type') == 'result']
    sessions = sorted({e['session_id'] for e in events if e.get('session_id')})
    observed_models = sorted({e.get('model') for e in inits if e.get('model')})
    return {
        'malformed_lines': errors, 'init_count': len(inits), 'result_count': len(results),
        'session_continuity': sessions == [expected_session],
        'model_matches': observed_models == [expected_model],
        'models': observed_models,
        'tool_names': sorted({t for e in inits for t in e.get('tools', [])}),
        'mcp_servers': [e.get('mcp_servers', []) for e in inits],
        'plugins': [[p.get('name') for p in e.get('plugins', [])] for e in inits],
        'api_key_sources': sorted({e.get('apiKeySource', 'missing') for e in inits}),
        'result_is_error': results[-1].get('is_error', True) if results else True,
        'result_subtype': results[-1].get('subtype') if results else None,
        'usage': results[-1].get('usage') if results else None,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--spec', required=True)
    args = ap.parse_args()
    spec = json.loads(Path(args.spec).read_text(encoding='utf-8'))
    uuid.UUID(spec['session_id'])
    executable_sha256 = native_executable(spec['executable'])
    if executable_sha256 != spec['executable_sha256']:
        raise SystemExit('Native executable fingerprint changed; no model launched')
    out = Path(spec['output_dir']).resolve()
    if out.exists():
        raise SystemExit('Output exists; no process launched')
    prompt = Path(spec['prompt_file']).read_text(encoding='utf-8')
    if not 0 < spec['timeout_s'] <= 360:
        raise SystemExit('Turn timeout must be within 360 seconds')
    for name in ('product_dir', 'project_dir', 'supervisor_dir'):
        if not Path(spec[name]).is_dir():
            raise SystemExit('Missing directory: ' + name)
    if not product_integrity(spec['product_dir'], spec['product_sha']):
        raise SystemExit('Product commit or clean-tree mismatch; no model launched')
    env = environment(spec['python_dir'])
    auth = subprocess.run([spec['executable'], 'auth', 'status', '--json'], env=env,
                          capture_output=True, text=True, timeout=30,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        route = json.loads(auth.stdout)
    except ValueError:
        raise SystemExit('Subscription auth could not be verified')
    if auth.returncode or route.get('authMethod') != 'claude.ai' or route.get('subscriptionType') != 'max':
        raise SystemExit('Expected claude.ai Max route not verified; no model launched')
    tools = 'Read,Write,Edit,Glob,Grep,Bash'
    cmd = [spec['executable'], '-p', '--safe-mode', '--restricted', '--strict-mcp-config',
           '--no-chrome', '--disable-slash-commands', '--model', spec['model'],
           '--output-format', 'stream-json', '--verbose', '--permission-mode', 'dontAsk',
           '--permission-prompts', 'none', '--tools', tools,
           '--allowedTools', 'Read,Write,Edit,Glob,Grep,Bash',
           '--add-dir', spec['product_dir'], spec['store_dir'],
           '--resume' if spec.get('resume') else '--session-id', spec['session_id'],
           '--', prompt]
    rb = load_supervisor(spec['supervisor_dir'])
    previous_env = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        rec = rb.run(cmd, spec['timeout_s'], str(out), cwd=spec['project_dir'])
    finally:
        os.environ.clear()
        os.environ.update(previous_env)
    parsed = inspect_stream((out / 'stdout.bin').read_bytes(), spec['session_id'], spec['model'])
    parsed.update(auth_route='claude.ai Max', resume_requested=bool(spec.get('resume')),
                  executable_sha256=executable_sha256,
                  product_unchanged=product_integrity(spec['product_dir'], spec['product_sha']),
                  prompt_sha256=hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
                  product_sha=spec['product_sha'], process_outcome=rec['outcome'],
                  os_exit_code=rec['os_exit_code'])
    parsed['transport_pass'] = (rec['outcome'] == 'exited' and rec['os_exit_code'] == 0
        and not parsed['malformed_lines'] and parsed['init_count'] == 1
        and parsed['result_count'] == 1 and parsed['session_continuity']
        and parsed['model_matches'] and not parsed['result_is_error']
        and parsed['api_key_sources'] == [spec['expected_api_key_source']]
        and parsed['tool_names'] == EXPECTED_TOOLS and parsed['product_unchanged']
        and all(not m for m in parsed['mcp_servers']))
    (out / 'turn-summary.json').write_text(json.dumps(parsed, indent=2), encoding='utf-8')
    print(json.dumps(parsed), flush=True)
    return 0 if parsed['transport_pass'] else 1


if __name__ == '__main__':
    sys.exit(main())

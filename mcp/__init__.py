"""KnoKeep MCP server package — the access layer built LAST, over the
finished store socket (store/backend.py + store/gate.py) and the read-only
reconciler (reconciler/).

JUDGMENT CALL: this package is named `mcp`, per the task's explicit
instruction to implement `mcp/server.py`. A third-party PyPI package also
named `mcp` (the official Anthropic MCP Python SDK) may be installed in this
environment — `pip show mcp` on this host resolves to it. This package does
NOT depend on it and does NOT import it anywhere; everything here is
hand-rolled from the stdlib (`json`, `sys`, `argparse`, `dataclasses`,
`typing`) per the task's "no third-party MCP lib" requirement. When this
repository's root is on `sys.path` (true for `python3 -m mcp.server` run
from `/home/claude/knokeep-build`, and true for pytest collected from that
same root), Python resolves `import mcp` to *this* local package rather than
the installed one, because a same-named entry earlier on `sys.path` (the
current/script directory) shadows one later on it (site-packages). This is
standard, well-defined Python import resolution — not a hack — but it means
this package must only ever be run/imported with that root on `sys.path`,
never installed alongside the real `mcp` SDK into the same environment for
unrelated use.
"""

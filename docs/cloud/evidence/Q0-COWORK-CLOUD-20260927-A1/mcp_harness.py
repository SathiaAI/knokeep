"""Shell-created MCP stdio harness. NOT native MCP enrollment: this task spawns
mcp/server.py itself with an explicit persistent LocalBackend (never the default fake).
Usage: python3 mcp_harness.py STORE PROJECT NOTE_FILE"""
import json, subprocess, sys

store, project, note_file = sys.argv[1:4]
note = open(note_file, encoding="utf-8").read()
srv = subprocess.Popen([sys.executable, "-m", "mcp.server", "--backend", "local", "--root", store],
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
reqs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "q0-cowork-cloud-shell-harness", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "knokeep_read", "arguments": {"key": f"{project}/system_state"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "knokeep_write", "arguments": {"key": f"{project}/sessions/q0-a1-mcp-harness",
                                                        "body": note, "doc_type": "journal"}}},
    {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
     "params": {"name": "knokeep_list", "arguments": {"prefix": project + "/"}}},
]
out = []
for r in reqs:
    print("REQ " + json.dumps(r, separators=(",", ":")))
    srv.stdin.write(json.dumps(r, separators=(",", ":")) + "\n"); srv.stdin.flush()
    if "id" in r:
        line = srv.stdout.readline()
        print("RESP " + line.rstrip("\n"))
srv.stdin.close()
rc = srv.wait(timeout=10)
err = srv.stderr.read()
print("SERVER_EXIT %d" % rc)
if err:
    print("SERVER_STDERR " + err.replace("\n", "\\n"))

"""Issue #20: resume parsing of '## Active State' / '## Next Step'.

The read-only resume view (_section / bootstrap) must use the same heading
rules as the section writer: exact, line-anchored level-2 headings, CRLF
tolerant, section ends at the next level-2 heading. Empty sections read as
"", never as the next heading's text. Duplicated headings are ambiguous and
read as "" with an explicit warning.
"""
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill"
if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))

import knokeep_state as ks  # noqa: E402

TEMPLATE = "## Completed & Verified\n\n## Active State\n\n## Next Step\n"


def both(body):
    w = []
    return ks._section(body, "Active State", w), ks._section(body, "Next Step", w), w


@pytest.mark.parametrize("body", [TEMPLATE, TEMPLATE.replace("\n", "\r\n")])
def test_empty_template_sections_are_empty(body):
    assert both(body) == ("", "", [])


@pytest.mark.parametrize("nl", ["\n", "\r\n"])
def test_adjacent_and_populated_sections(nl):
    adjacent = nl.join(["## Active State", "## Next Step", "do B", ""])
    assert both(adjacent) == ("", "do B", [])
    populated = nl.join(["## Active State", "doing A", "line 2", "", "## Next Step", "do B", ""])
    assert both(populated) == ("doing A\nline 2", "do B", [])


def test_next_step_is_last_section_without_trailing_newline():
    assert both("## Active State\nA\n## Next Step\nB") == ("A", "B", [])


def test_nested_and_level1_headings_are_content_not_boundaries():
    body = "## Active State\nA\n### Detail\nsub\n# Title\nx\n## Next Step\nB\n"
    assert both(body)[0] == "A\n### Detail\nsub\n# Title\nx"


def test_level3_heading_is_not_selected_as_the_section():
    assert both("### Active State\nnested\n## Next Step\nB\n") == ("", "B", [])


def test_prose_is_not_a_heading_or_boundary():
    body = "## Active State\nsee ## Next Step later\n  not ##a heading\n## Next Step\nB\n"
    assert both(body)[0] == "see ## Next Step later\n  not ##a heading"
    # no space after '##' is not an ATX heading (CommonMark), so it is not matched
    assert both("##Active State\nA\n") == ("", "", [])


def test_heading_name_must_match_exactly():
    assert both("## Active States\nA\n## Next Steps\nB\n") == ("", "", [])


def test_heading_indent_and_trailing_space():
    assert both("   ## Active State  \nA\n## Next Step\t\nB\n") == ("A", "B", [])
    # 4-space indent is a code block in CommonMark, not a heading
    assert both("    ## Active State\nA\n") == ("", "", [])


def test_duplicate_heading_is_ambiguous_and_reported():
    active, nxt, w = both("## Active State\nA1\n## Active State\nA2\n## Next Step\nB\n")
    assert (active, nxt) == ("", "B")
    assert w == ["duplicate heading: Active State"]


def test_resume_view_truncation_is_explicit():
    active, _, warnings = both("## Active State\n" + "x" * 500 + "\n")
    assert len(active) == 400
    assert warnings == ["truncated preview: Active State; read the full section before acting"]


def _cli(store, *args, stdin=None):
    p = subprocess.run([sys.executable, str(SKILL / "knokeep_state.py"), "--store", str(store),
                        "--project", "p", *args], capture_output=True, text=True, input=stdin)
    return p.returncode, p.stdout, p.stderr


def test_bootstrap_after_init_reports_empty_sections(tmp_path):
    assert _cli(tmp_path, "init")[0] == 0
    rc, out, _ = _cli(tmp_path, "bootstrap")
    res = json.loads(out)
    assert rc == 0 and res["active"] == "" and res["next"] == ""
    assert res["resume_line"].startswith("resuming: (none) / next: (none) /")
    assert "section_warnings" not in res


def test_bootstrap_duplicate_heading_warns_in_resume_line(tmp_path):
    assert _cli(tmp_path, "init")[0] == 0
    lh = json.loads(_cli(tmp_path, "bootstrap")[1])["log_hash"]
    body = tmp_path / "log.md"
    body.write_text("## Active State\nA1\n\n## Active State\nA2\n\n## Next Step\nB\n", encoding="utf-8")
    rc, out, err = _cli(tmp_path, "flush-log", "--body-file", str(body), "--expect-hash", lh)
    assert rc == 0, err
    res = json.loads(_cli(tmp_path, "bootstrap")[1])
    assert res["active"] == "" and res["next"] == "B"
    assert res["section_warnings"] == ["duplicate heading: Active State"]
    assert "[!] duplicate heading: Active State" in res["resume_line"]
    assert res["active_full"] == "" and res["next_full"] == "B"


@pytest.mark.parametrize("size", [399, 400, 401])
def test_bootstrap_preserves_full_decision_and_marks_preview_boundary(tmp_path, size):
    assert _cli(tmp_path, "init")[0] == 0
    lh = json.loads(_cli(tmp_path, "bootstrap")[1])["log_hash"]
    # Characters, not UTF-8 bytes; exclusion after a long prefix must survive.
    active = "é" * size
    next_step = "Continue the review. " * 24 + "Never approve a held order."
    body = tmp_path / "long-log.md"
    body.write_bytes((f"## Active State\r\n{active}\r\n## Next Step\r\n{next_step}\r\n").encode())
    assert _cli(tmp_path, "flush-log", "--body-file", str(body), "--expect-hash", lh)[0] == 0
    rc, output, _ = _cli(tmp_path, "bootstrap")
    res = json.loads(output)
    assert rc == 0
    assert res["active_full"] == active and res["next_full"] == next_step
    assert res["active"] == active[:400] and res["next"] == next_step[:400]
    assert res["truncated_fields"] == (["active", "next"] if size > 400 else ["next"])
    assert "read next_full before acting" in res["resume_line"]
    assert ("read active_full before acting" in res["resume_line"]) == (size > 400)

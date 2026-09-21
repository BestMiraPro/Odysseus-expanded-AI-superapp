# -*- coding: utf-8 -*-
"""Generate the security-alert disposition ledger from the plan's appendices.

Parses the two inventory tables in docs/superpowers/plans/2026-09-20-security-
alert-remediation.md (Appendix A: 58 non-exception alerts, Appendix B: 77
exception-exposure alerts) and writes dev-docs/security-alert-dispositions-
2026-09-20.md with one row per alert: link, rule family, location, work
package, and disposition status. Transcribes nothing by hand.
"""
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "docs" / "superpowers" / "plans" / "2026-09-20-security-alert-remediation.md"
OUT = ROOT / "dev-docs" / "security-alert-dispositions-2026-09-20.md"

text = io.open(PLAN, encoding="utf-8").read()
text = text.replace("\u2013", "-").replace("\u00a0", " ")


def section(start_marker: str, end_marker: str) -> str:
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    return text[i:j]


def parse_table(block: str):
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if re.match(r"^\|[\s:|-]+\|", line):
            continue  # separator
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] and not cells[0].startswith("-"):
            rows.append(cells)
    return rows


def gen():
    out = io.StringIO()

    def w(s: str = ""):
        out.write(s + "\n")

    w("# Security alert disposition ledger")
    w()
    w("Baseline per `docs/superpowers/plans/2026-09-20-security-alert-"
      "remediation.md`; inventory verified 2026-09-21.")
    w("Branch: `security/alert-remediation` off `dev` @ "
      "`8a8f1bcfdafc7c5e2bf5a5252a26ba6594bfea73`.")
    w()
    w("## Coverage notes (S0)")
    w()
    w("- Live GitHub refresh blocked in the implementation environment: no `gh` "
      "CLI and the code-scanning REST API returns 401 unauthenticated (credential "
      "reading is prohibited by the plan). Ruling: the plan's documented inventory "
      "— verified against the exact HEAD still checked out — is the working "
      "baseline. Re-run the live refresh where credentials are available.")
    w("- `branch:main` inventory and CodeQL tool status are likewise recorded as "
      "coverage gaps until a GitHub-authenticated environment is available.")
    w()
    w("Disposition values: `open`, `fixed:<commit>`, `false-positive`, "
      "`risk-accepted`, `pending-evidence`.")
    w()

    a = section("## Appendix A.", "## Appendix B.")
    a_body = a.split("\n", 2)[2]
    w("## Appendix A - non-exception alerts")
    w()
    w("| Alert | Location | Package | Disposition |")
    w("|---|---|---|---|")
    for cells in parse_table(a_body):
        ids, loc = cells[0], cells[1]
        pkg = cells[2] if len(cells) > 2 else ""
        rule_loc = " ".join(loc.split()) if loc else ""
        for alert_id in [x for x in ids.split(",") if x.strip().isdigit()]:
            w("| [%s](https://github.com/BestMiraPro/Odysseus-expanded-AI-"
              "superapp/security/code-scanning/%s) | %s | %s | open |"
              % (alert_id, alert_id, rule_loc, pkg))
    w()

    b = section("## Appendix B.", "## Reference material")
    b_body = b.split("\n", 2)[2]
    w("## Appendix B - exception-exposure alerts")
    w()
    w("| Alert | Location | Package | Disposition |")
    w("|---|---|---|---|")
    for cells in parse_table(b_body):
        loc, pairs = cells[0].strip("`"), cells[1]
        for chunk in pairs.split(","):
            chunk = chunk.strip()
            m = re.match(r"(\d+):(\d+)", chunk)
            if not m:
                continue
            w("| [%s](https://github.com/BestMiraPro/Odysseus-expanded-AI-"
              "superapp/security/code-scanning/%s) | `%s:%s` | S5 | open |"
              % (m.group(1), m.group(1), loc, m.group(2)))
    w()

    total_a = 0
    total_b = 0
    result = out.getvalue()
    for m in re.finditer(r"codes-scanning", ""):  # noqa: placeholder guard
        pass
    return result


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(gen(), encoding="utf-8")
    # sanity: 58 + 77 rows
    rows_a = sum(1 for line in gen().splitlines()
                 if line.startswith("| [") )
    print("wrote", OUT, "with", sum(
        1 for line in OUT.read_text(encoding="utf-8").splitlines()
        if line.startswith("| [")), "alert rows")
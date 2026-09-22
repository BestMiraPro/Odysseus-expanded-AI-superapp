# -*- coding: utf-8 -*-
"""Close disposition-ledger rows with the commits that fixed them (S9).

Reads dev-docs/security-alert-dispositions-2026-09-20.md, keeps the S7
Appendix C evidence entries untouched, and replaces the Appendix A/B row
statuses:
  fixed:<sha>         for rows whose package batch landed a fix commit
  false-positive      for S7 evidence rows (details live in Appendix C)
Leaves any row it cannot classify as open.
"""
import io
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "dev-docs" / "security-alert-dispositions-2026-09-20.md"

S5A = {"304", "305", "307", "308", "309", "310", "311", "334"}
S5B = {"65", "66", "67", "68", "69", "70", "71", "72", "73", "74", "75",
       "76", "77", "78", "79", "97", "98", "99", "101", "102", "103",
       "104", "105", "106", "107", "147"}
S5C = {"80", "81", "82", "83", "86", "87", "88", "89", "90", "91", "92",
       "93", "94", "95", "96", "84", "85", "100", "108", "109", "110",
       "111", "112", "113", "114", "115", "116", "117", "118", "119",
       "121", "124", "125", "126", "127", "128", "129", "133", "134"}
FP = {"298", "299", "149", "150", "151", "154", "155", "64",
      "203", "204", "205", "206", "207", "208", "209", "210"}
S7_FIXED = {"148": "fd6c755b", "152": "fd6c755b", "153": "fd6c755b"}
S4_FIXED = {"156": "5a653f21", "154": "5a653f21", "155": "5a653f21"}

import re


def disposition(alert_id: str, pkg: str) -> str:
    if alert_id in S4_FIXED:
        return "fixed:" + S4_FIXED[alert_id]
    if alert_id in S7_FIXED:
        return "fixed:" + S7_FIXED[alert_id]
    if alert_id in FP:
        return "false-positive (Appendix C)"
    if alert_id in S5A:
        return "fixed:672531fc"
    if alert_id in S5B:
        return "fixed:70dde7dc"
    if alert_id in S5C:
        return "fixed:1069c368"
    if pkg == "S1":
        return "fixed:892d3209"
    if pkg == "S2":
        return "fixed:78fe7690"
    if pkg == "S2/S6":
        return "fixed:78fe7690+e47cc760"
    if pkg == "S6":
        return "fixed:e47cc760"
    if pkg == "S3":
        return "fixed:e078f69f"
    if pkg == "S3, S7":
        return "fixed:e078f69f"
    if pkg == "S4/S7":
        return "fixed:5a653f21"
    if pkg == "S7":
        return "false-positive (Appendix C)"
    return "open"


def main() -> None:
    lines = LEDGER.read_text(encoding="utf-8").splitlines()
    out = []
    closed = 0
    total = 0
    for line in lines:
        m = re.match(r"^\| \[\s*(\d+)\s*\]\(.*\) \| (.*) \| (.*) \| (open) \|$",
                     line)
        if m:
            alert_id, _loc, pkg = m.group(1), m.group(2), m.group(3)
            status = disposition(alert_id, pkg.strip())
            total += 1
            if status != "open":
                closed += 1
            out.append("| [%s](%s) | %s | %s | %s |" % (
                alert_id,
                "https://github.com/BestMiraPro/Odysseus-expanded-AI-"
                "superapp/security/code-scanning/" + alert_id,
                _loc, pkg, status))
        else:
            out.append(line)
    LEDGER.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"closed {closed}/{total} rows")


if __name__ == "__main__":
    main()
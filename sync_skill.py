"""Copy gapfinder.py into SKILL.md between the gapfinder.py markers. Run after editing gapfinder.py.

Dev tool only; it is not part of the published skill.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BEGIN = "<!-- gapfinder.py begin -->\n```python\n"
END = "```\n<!-- gapfinder.py end -->"

skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
script = (ROOT / "gapfinder.py").read_text(encoding="utf-8")
head, rest = skill.split(BEGIN, 1)
_, tail = rest.split(END, 1)
with open(ROOT / "SKILL.md", "w", encoding="utf-8", newline="\n") as f:
    f.write(head + BEGIN + script + END + tail)
print("SKILL.md now embeds gapfinder.py (%d lines)" % script.count("\n"))

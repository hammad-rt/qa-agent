"""Guards the prompt rule lists against spliced edits (a new rule inserted mid-sentence, orphaned continuations)."""
import re
import unittest

from agent_tester.agents import prompts

NAMES = [n for n in dir(prompts) if n.isupper() and isinstance(getattr(prompts, n), str)]
_END = re.compile(r"""[.:;!?)"'`\]}]$|\{\{|\}\}$""")


def problems(name: str, text: str) -> list[str]:
    out, lines = [], text.splitlines()
    seen = set()
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line.startswith("- "):
            rule = line.strip()
            if rule in seen:
                out.append(f"{name}:{i+1} duplicate rule: {rule[:60]}")
            seen.add(rule)
        in_rule = line.startswith("- ") or (line.startswith("  ") and line.strip())
        if in_rule and nxt.startswith("- ") and not _END.search(line.rstrip()):
            out.append(f"{name}:{i+1} rule stops mid-sentence before a new rule: {line.strip()[-60:]!r}")
        if line.strip()[:1] in "{[\"]}":
            continue                                  # JSON examples are not rules
        if line.startswith("  ") and line.strip() and i and not (lines[i-1].startswith(("- ", "  ")) or not lines[i-1].strip()):
            out.append(f"{name}:{i+1} indented continuation with no rule above it")
    return out


class PromptLint(unittest.TestCase):
    def test_no_spliced_rules(self):
        found = [p for n in NAMES for p in problems(n, getattr(prompts, n))]
        self.assertEqual(found, [], "\n".join(found))

    def test_lint_catches_a_splice(self):
        bad = "- Never guess a flow. Read the page and act only on\n- If a click fails, try another.\n  what is there.\n"
        self.assertTrue(problems("X", bad))

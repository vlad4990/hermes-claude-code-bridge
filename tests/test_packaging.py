"""Hermes copies SKILL.md plus only the support files SKILL.md references (see
tools/skills_hub.py in hermes-agent). Every shipped file must therefore be mentioned."""
import re
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent / "skills" / "cc-bridge"
LINK_RE = re.compile(r"(?:\]\(|`|(?:^|[\s\"']))((?:references|templates|scripts|assets|examples)/[^\s)`\"'<>]+)", re.M)


class Packaging(unittest.TestCase):
    def test_every_support_file_is_referenced(self):
        text = (SKILL / "SKILL.md").read_text()
        referenced = {m.group(1).rstrip(".,;:") for m in LINK_RE.finditer(text)}
        shipped = {str(p.relative_to(SKILL)) for d in ("scripts", "templates", "references")
                   for p in (SKILL / d).rglob("*") if p.is_file() and not p.name.startswith(".")}
        self.assertTrue(shipped, "no support files found")
        self.assertEqual(shipped - referenced, set(), "files not referenced in SKILL.md would not be installed")

    def test_frontmatter(self):
        text = (SKILL / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: cc-bridge\n"))
        self.assertIn("requires_toolsets: [terminal]", text)

    def test_python_39_syntax(self):
        import ast
        for f in (SKILL / "scripts").glob("*.py"):
            ast.parse(f.read_text(), filename=str(f), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()

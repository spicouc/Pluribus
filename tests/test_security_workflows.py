"""Static guardrails for repository security workflows."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
SHA_ACTION = re.compile(r"uses:\s+[^\s@]+@([0-9a-f]{40})(?:\s+#.*)?$")


class SecurityWorkflowTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def _assert_actions_are_sha_pinned(self, content: str) -> None:
        uses_lines = [line.strip() for line in content.splitlines() if line.strip().startswith("uses:")]
        self.assertTrue(uses_lines)
        for line in uses_lines:
            self.assertRegex(line, SHA_ACTION, msg=f"Action no pinnejada per SHA: {line}")
        self.assertNotRegex(content, r"uses:\s+[^\s]+@v\d")

    def test_codeql_is_pinned_and_least_privilege(self) -> None:
        content = self._read("codeql.yml")
        self._assert_actions_are_sha_pinned(content)
        self.assertIn("security-events: write", content)
        self.assertIn("contents: read", content)
        self.assertIn("languages: python", content)
        self.assertIn("build-mode: none", content)
        self.assertIn("queries: security-extended", content)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", content)

    def test_dependency_review_is_pinned_and_read_only(self) -> None:
        content = self._read("dependency-review.yml")
        self._assert_actions_are_sha_pinned(content)
        self.assertIn("contents: read", content)
        self.assertNotIn("security-events: write", content)
        self.assertNotIn("pull-requests: write", content)
        self.assertIn("fail-on-severity: moderate", content)
        self.assertIn("vulnerability-check: true", content)
        self.assertIn("license-check: true", content)


if __name__ == "__main__":
    unittest.main()

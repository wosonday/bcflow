"""Execute the documented Bash/CI guards in isolated Git fixtures."""

import os
import re
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
BASH = str(GIT_BASH) if os.name == "nt" and GIT_BASH.is_file() else shutil.which("bash")
GIT = shutil.which("git")


def examples() -> list[tuple[str, str]]:
    scripts = []
    for relative in ("docs/ESLINT-CI-GUARDRAILS.md",):
        text = (ROOT / relative).read_text(encoding="utf-8")
        bash = re.findall(r"```bash\n(.*?)\n```", text, re.DOTALL)
        if len(bash) != 1:
            raise AssertionError(f"{relative}: expected one Bash bypass guard")
        scripts.append((relative + ":bash", bash[0]))
        ci = text.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0]
        lines = textwrap.dedent(ci).splitlines()
        if not lines[0].startswith("BASE_REF="):
            raise AssertionError(f"{relative}: expected CI base assignment")
        scripts.append((relative + ":ci", "\n".join(['BASE_REF="$1"', *lines[1:]])))
    return scripts


@unittest.skipUnless(
    BASH and GIT, "Git and Bash are needed to run the documented shell examples"
)
class GuardrailExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="harness-guard-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Harness Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Harness Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        for name in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_CONFIG",
            "GIT_CONFIG_COUNT",
        ):
            self.env.pop(name, None)
        self.git("init", "--quiet", "--initial-branch=main")
        self.write("README.txt", "fixture\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args):
        result = subprocess.run(
            [
                GIT,
                "-c",
                "core.hooksPath=" + str(self.root / "no-hooks"),
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.autocrlf=false",
                *args,
            ],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture")

    def check(self, expected, *, base=None, ci_only=False):
        for name, script in examples():
            if ci_only and not name.endswith(":ci"):
                continue
            with self.subTest(example=name):
                result = subprocess.run(
                    [BASH, "-c", script, "guard-fixture", base or self.base],
                    cwd=self.root,
                    env=self.env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=20,
                )
                if expected == 0:
                    self.assertEqual(
                        result.returncode, 0, result.stderr + result.stdout
                    )
                elif base is not None:
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("fatal:", result.stderr)
                else:
                    self.assertEqual(
                        result.returncode, 1, result.stderr + result.stdout
                    )
                    self.assertRegex(
                        result.stdout + result.stderr,
                        r"CI GATE ERROR|LỖI CỔNG CI|::error::",
                    )

    def test_clean_diff_passes(self):
        self.check(0)

    def test_normal_code_change_passes(self):
        self.write("src/normal.ts", "export const count = 1;\n")
        self.commit()
        self.check(0)

    def test_invalid_base_fails_closed(self):
        self.check(1, base="nonexistent-base")

    def test_typescript_bypass_rejected(self):
        self.write("src/bad.ts", "// @ts-ignore\nexport const n = 1;\n")
        self.commit()
        self.check(1)

    def test_mjs_bypass_rejected(self):
        self.write("src/bad.mjs", "/* eslint-disable */\nexport const n = 1;\n")
        self.commit()
        self.check(1)

    def test_mts_bypass_rejected(self):
        self.write("src/bad.mts", "// @ts-nocheck\nexport const n = 1;\n")
        self.commit()
        self.check(1)

    def test_cts_bypass_rejected(self):
        self.write("src/bad.cts", "// @ts-ignore\nexport const n = 1;\n")
        self.commit()
        self.check(1)

    def test_nested_eslint_config_change_rejected_by_ci(self):
        self.write("packages/web/eslint.config.mjs", "export default [];\n")
        self.commit()
        self.check(1, ci_only=True)

    def test_nested_legacy_eslint_config_rejected_by_ci(self):
        self.write("packages/web/.eslintrc.json", "{}\n")
        self.commit()
        self.check(1, ci_only=True)

    def test_bypass_keyword_in_filename_not_false_positive(self):
        self.write("src/eslint-disable.ts", "export const n = 1;\n")
        self.commit()
        self.check(0)

    def run_skill_dispatch(self, selected_skills):
        text = (ROOT / "HERDR-CORE-BOT.md").read_text(encoding="utf-8")
        section = text.split("### 1.5.", 1)[1]
        script = re.search(r"```bash\n(.*?)\n```", section, re.DOTALL)[1]
        self.write(".agents/bot/dispatch.sh", script)
        self.write(
            ".agents/bot.env",
            f'PROJECT_DIR="{self.root.as_posix()}"\nWT_DIR=.wt\nBASE_BRANCH=main\nMANAGER=fixture\n',
        )
        self.write(
            ".agents/tasks/skill-test.md", f"tier: nhanh\nskills: {selected_skills}\n"
        )
        (self.root / ".wt/skill-test").mkdir(parents=True)
        # Intercept only the external agent launcher; execute the documented script.
        invocation = 'agm() { printf "%s\\n" "$@"; }; export -f agm; bash .agents/bot/dispatch.sh skill-test'
        return subprocess.run(
            [BASH, "-c", invocation],
            check=False,
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )

    def test_dispatch_accepts_explicit_third_required_skill(self):
        selected = (
            "systematic-debugging, rust-best-practices, verification-before-completion"
        )
        result = self.run_skill_dispatch(selected)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for skill in selected.split(", "):
            self.assertIn(skill, result.stdout)

    def test_dispatch_allows_mechanical_task_without_workflow_skills(self):
        result = self.run_skill_dispatch("")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("--task", result.stdout)

    def test_dispatch_accepts_explicit_four_skill_phase_exception(self):
        selected = "receiving-code-review, systematic-debugging, rust-best-practices, verification-before-completion"
        result = self.run_skill_dispatch(selected)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for skill in selected.split(", "):
            self.assertIn(skill, result.stdout)


if __name__ == "__main__":
    unittest.main()

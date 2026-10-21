import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REAL_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "release-public.sh"


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


class ReleasePublicScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            REAL_SCRIPT.chmod(0o755)
        except OSError:
            pass

    def _make_fixture(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        src = tmp / "src"
        dst = tmp / "dst"
        denylist = tmp / "release-denylist.txt"

        denylist.write_text("personal-secret-identifier\n", encoding="utf-8")

        src.mkdir(parents=True)
        subprocess.run(["git", "init", str(src)], check=True, capture_output=True)
        _git(src, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(src, "config", "user.name", "Private Dev")
        _git(src, "config", "user.email", "dev@example.com")
        _git(src, "config", "commit.gpgsign", "false")
        _git(src, "config", "tag.gpgsign", "false")

        (src / "README.md").write_text("# PromptBridge\n", encoding="utf-8")
        (src / "old_feature.txt").write_text("will be removed\n", encoding="utf-8")
        script_copy = src / "ai-orchestrator" / "deploy" / "release-public.sh"
        script_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REAL_SCRIPT, script_copy)
        script_copy.chmod(0o755)

        _git(src, "add", "-A")
        _git(src, "commit", "-m", "Initial private commit")

        dst.mkdir(parents=True)
        subprocess.run(["git", "init", str(dst)], check=True, capture_output=True)
        _git(dst, "symbolic-ref", "HEAD", "refs/heads/main")
        _git(dst, "config", "user.name", "Public Release")
        _git(dst, "config", "user.email", "123456+promptbridge@users.noreply.github.com")
        _git(dst, "config", "commit.gpgsign", "false")
        _git(dst, "config", "tag.gpgsign", "false")

        (dst / "README.md").write_text("# Old Public README\n", encoding="utf-8")
        (dst / "old_feature.txt").write_text("present in previous public tree\n", encoding="utf-8")
        _git(dst, "add", "-A")
        _git(dst, "commit", "-m", "Initial public commit")

        return src, dst, denylist, script_copy

    def _run_release(
        self,
        script_copy: Path,
        dst: Path,
        denylist: Path,
        version: str,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PB_PUBLIC_TREE"] = str(dst)
        env["PB_RELEASE_DENYLIST"] = str(denylist)
        return subprocess.run(
            [str(script_copy), version],
            env=env,
            capture_output=True,
            text=True,
        )

    def test_1_successful_release_updates_tree_commits_and_tags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src, dst, denylist, script_copy = self._make_fixture(Path(td))

            # Delete a tracked file in SRC and verify it is removed from DST on release.
            _git(src, "rm", "-q", "old_feature.txt")
            _git(src, "commit", "-m", "Remove old_feature.txt")
            src_sha = _git(src, "rev-parse", "HEAD")

            res = self._run_release(script_copy, dst, denylist, "v0.2.0")
            self.assertEqual(res.returncode, 0, msg=res.stderr)

            self.assertEqual(_git(dst, "log", "-1", "--pretty=%B"), "Release v0.2.0")
            self.assertNotIn(src_sha, _git(dst, "log", "-1", "--pretty=%B"))
            self.assertIn("v0.2.0", _git(dst, "tag", "-l").splitlines())
            self.assertEqual(_git(dst, "tag", "-l", "-n1", "v0.2.0").split(maxsplit=1)[1], "PromptBridge v0.2.0")
            self.assertFalse((dst / "old_feature.txt").exists())
            self.assertNotIn("old_feature.txt", _git(dst, "ls-files").splitlines())
            self.assertIn(
                f"To publish (needs the owner's approval): git -C {dst} push origin main && git -C {dst} push origin v0.2.0",
                res.stdout,
            )

    def test_2_denylist_match_fails_and_rolls_back_dst(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src, dst, denylist, script_copy = self._make_fixture(Path(td))
            dst_head_before = _git(dst, "rev-parse", "HEAD")

            denylist.write_text("secret-name\n", encoding="utf-8")
            (src / "README.md").write_text("# PromptBridge\nAuthor: secret-name\n", encoding="utf-8")
            _git(src, "add", "README.md")
            _git(src, "commit", "-m", "Add author name")

            res = self._run_release(script_copy, dst, denylist, "v0.2.0")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("secret-name", res.stdout + res.stderr)
            self.assertEqual(_git(dst, "rev-parse", "HEAD"), dst_head_before)
            self.assertEqual(_git(dst, "tag", "-l"), "")
            self.assertEqual(_git(dst, "status", "--porcelain"), "")

    def test_3_forbidden_isbirligi_path_fails_and_rolls_back_dst(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src, dst, denylist, script_copy = self._make_fixture(Path(td))
            dst_head_before = _git(dst, "rev-parse", "HEAD")

            collab_file = src / "_isbirligi" / "x.md"
            collab_file.parent.mkdir(parents=True, exist_ok=True)
            collab_file.write_text("internal notes\n", encoding="utf-8")
            _git(src, "add", "_isbirligi/x.md")
            _git(src, "commit", "-m", "Track internal file")

            res = self._run_release(script_copy, dst, denylist, "v0.2.0")
            self.assertNotEqual(res.returncode, 0)
            self.assertEqual(_git(dst, "rev-parse", "HEAD"), dst_head_before)
            self.assertEqual(_git(dst, "tag", "-l"), "")
            self.assertEqual(_git(dst, "status", "--porcelain"), "")

    def test_4_precondition_failures(self) -> None:
        # 4a: dirty SRC
        with self.subTest("dirty_src"), tempfile.TemporaryDirectory() as td:
            src, dst, denylist, script_copy = self._make_fixture(Path(td))
            (src / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            res = self._run_release(script_copy, dst, denylist, "v0.2.0")
            self.assertNotEqual(res.returncode, 0)

        # 4b: invalid version
        with self.subTest("invalid_version"), tempfile.TemporaryDirectory() as td:
            src, dst, denylist, script_copy = self._make_fixture(Path(td))
            for bad_version in ("0.2.0", "v0.2", "v0.2.0-rc1", ""):
                res = self._run_release(script_copy, dst, denylist, bad_version)
                self.assertNotEqual(res.returncode, 0, msg=f"version={bad_version!r}")

        # 4c: existing tag in DST
        with self.subTest("existing_tag"), tempfile.TemporaryDirectory() as td:
            src, dst, denylist, script_copy = self._make_fixture(Path(td))
            _git(dst, "tag", "v0.2.0")
            res = self._run_release(script_copy, dst, denylist, "v0.2.0")
            self.assertNotEqual(res.returncode, 0)

        # 4d: non-noreply DST user.email
        with self.subTest("non_noreply_email"), tempfile.TemporaryDirectory() as td:
            src, dst, denylist, script_copy = self._make_fixture(Path(td))
            _git(dst, "config", "user.email", "owner@example.com")
            res = self._run_release(script_copy, dst, denylist, "v0.2.0")
            self.assertNotEqual(res.returncode, 0)

        # 4e: missing or empty denylist
        with self.subTest("missing_denylist"), tempfile.TemporaryDirectory() as td:
            src, dst, denylist, script_copy = self._make_fixture(Path(td))
            denylist.unlink()
            res = self._run_release(script_copy, dst, denylist, "v0.2.0")
            self.assertNotEqual(res.returncode, 0)

            denylist.write_text("", encoding="utf-8")
            res_empty = self._run_release(script_copy, dst, denylist, "v0.2.0")
            self.assertNotEqual(res_empty.returncode, 0)

    def test_5_no_changes_exits_zero_without_new_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src, dst, denylist, script_copy = self._make_fixture(Path(td))

            first = self._run_release(script_copy, dst, denylist, "v0.2.0")
            self.assertEqual(first.returncode, 0, msg=first.stderr)
            head_after_first = _git(dst, "rev-parse", "HEAD")

            second = self._run_release(script_copy, dst, denylist, "v0.2.1")
            self.assertEqual(second.returncode, 0, msg=second.stderr)
            self.assertIn("nothing to release", second.stdout)
            self.assertEqual(_git(dst, "rev-parse", "HEAD"), head_after_first)
            self.assertNotIn("v0.2.1", _git(dst, "tag", "-l").splitlines())


if __name__ == "__main__":
    unittest.main()

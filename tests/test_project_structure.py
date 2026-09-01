import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUIDE_PATH = PROJECT_ROOT / "PROJECT_STRUCTURE.md"
TRANSIENT_ROOT_ENTRIES = {".git", ".idea", ".pytest_cache", "__pycache__"}


class ProjectStructureGuideTests(unittest.TestCase):
    def test_workspace_hierarchy_is_documented(self):
        guide = GUIDE_PATH.read_text(encoding="utf-8")

        for entry in PROJECT_ROOT.iterdir():
            if entry.name in TRANSIENT_ROOT_ENTRIES:
                continue
            marker = f"`{entry.name}/`" if entry.is_dir() else f"`{entry.name}`"
            with self.subTest(root_entry=entry.name):
                self.assertIn(marker, guide)

        source_root = PROJECT_ROOT / "src" / "auto_test"
        for directory in source_root.iterdir():
            if not directory.is_dir() or directory.name == "__pycache__":
                continue
            marker = f"`src/auto_test/{directory.name}/`"
            with self.subTest(source_directory=directory.name):
                self.assertIn(marker, guide)

        for area in (PROJECT_ROOT / "deploy", PROJECT_ROOT / "scripts", PROJECT_ROOT / "docs"):
            for directory in area.iterdir():
                if not directory.is_dir() or directory.name == "__pycache__":
                    continue
                marker = f"`{directory.relative_to(PROJECT_ROOT).as_posix()}/`"
                with self.subTest(managed_directory=marker):
                    self.assertIn(marker, guide)


if __name__ == "__main__":
    unittest.main()

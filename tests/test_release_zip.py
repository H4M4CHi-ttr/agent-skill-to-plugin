from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_skill_zip.py"
_SPEC = importlib.util.spec_from_file_location("build_skill_zip", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import machinery failure
    raise RuntimeError("could not load build_skill_zip.py")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_skill_zip = _MODULE.build_skill_zip


class ReleaseZipTests(unittest.TestCase):
    def test_release_zip_is_deterministic_single_root_and_excludes_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "agent-skill-to-plugin"
            (source / "agents").mkdir(parents=True)
            (source / "scripts" / "__pycache__").mkdir(parents=True)
            (source / "build").mkdir()
            for relative in ("SKILL.md", "README.md", "README.ja.md", "LICENSE"):
                (source / relative).write_text(relative, encoding="utf-8")
            (source / "agents" / "openai.yaml").write_text("interface: {}\n", encoding="utf-8")
            (source / "scripts" / "porter.py").write_text("pass\n", encoding="utf-8")
            (source / "scripts" / "__pycache__" / "porter.pyc").write_bytes(b"cache")
            (source / "build" / "temporary.txt").write_text("temporary", encoding="utf-8")

            first = build_skill_zip(source, base / "first.zip")
            second = build_skill_zip(source, base / "second.zip")

            self.assertEqual(first["zip_sha256"], second["zip_sha256"])
            with zipfile.ZipFile(base / "first.zip") as archive:
                names = archive.namelist()
            self.assertTrue(names)
            self.assertTrue(all(name.startswith("agent-skill-to-plugin/") for name in names))
            self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))
            self.assertFalse(any("/build/" in name for name in names))

    def test_output_inside_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "agent-skill-to-plugin"
            (source / "agents").mkdir(parents=True)
            for relative in ("SKILL.md", "README.md", "README.ja.md", "LICENSE"):
                (source / relative).write_text(relative, encoding="utf-8")
            (source / "agents" / "openai.yaml").write_text("interface: {}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_skill_zip(source, source / "release.zip")


if __name__ == "__main__":
    unittest.main()

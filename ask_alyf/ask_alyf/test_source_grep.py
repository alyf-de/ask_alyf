import os
import shutil
import subprocess
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest import skipUnless
from unittest.mock import patch

from frappe.tests import UnitTestCase

from ask_alyf.ask_alyf.deep_agent_backend import ReadOnlySourceBackend
from ask_alyf.ask_alyf.source_grep import SourceGrep
from ask_alyf.ask_alyf.tools import get_installed_app_roots


class UnitTestSourceGrep(UnitTestCase):
	def setUp(self):
		self.directory = TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.bench = Path(self.directory.name).resolve()
		self.root = self.bench / "apps" / "sample"
		self.root.mkdir(parents=True)
		self.backend = ReadOnlySourceBackend({"sample": self.root})
		self.enterContext(patch("ask_alyf.ask_alyf.tools.get_bench_path", return_value=str(self.bench)))

	def write(self, path, content="needle\n"):
		file = self.root / path
		file.parent.mkdir(parents=True, exist_ok=True)
		file.write_text(content, encoding="utf-8")
		return file

	def matches(self, pattern="needle", **kwargs):
		result = self.backend.grep(pattern, **kwargs)
		self.assertIsNone(result.error)
		return result.matches

	def test_paths_globs_and_line_numbers(self):
		self.write("src/main.py", "first\nneedle = 1\nneedle = 2\n")
		self.write("src/main.js")
		self.write("other.py")
		expected = [
			{"path": "/sample/src/main.py", "line": 2, "text": "needle = 1"},
			{"path": "/sample/src/main.py", "line": 3, "text": "needle = 2"},
		]
		for path in ["/", "/sample", "/sample/src", "/sample/src/main.py"]:
			with self.subTest(path=path):
				self.assertEqual(self.matches(path=path, glob="sample/src/**/*.py"), expected)
		self.assertEqual(len(self.matches(glob="**/*.{py,js}")), 4)
		self.assertEqual(self.matches(glob="*.py"), [])

	def test_dependencies_and_build_output_are_excluded_without_git_metadata(self):
		self.write("source.py")
		for path in [
			"node_modules/pkg/index.js",
			"frontend/node_modules/pkg/index.js",
			"sample/public/dist/bundle.js",
			"sample/public/frontend/assets/bundle.js",
			"sample/public/source.js.map",
			"__pycache__/cached.py",
		]:
			self.write(path)
			with self.subTest(path=path):
				self.assertEqual(self.matches(path=f"/sample/{path}"), [])
		scandir = os.scandir

		def scoped_scandir(path):
			self.assertNotIn("node_modules", Path(path).parts)
			return scandir(path)

		with patch("ask_alyf.ask_alyf.source_files.os.scandir", side_effect=scoped_scandir):
			self.assertEqual([match["path"] for match in self.matches()], ["/sample/source.py"])

	def test_search_across_apps_respects_the_selected_app(self):
		self.write("main.py")
		other_root = self.bench / "apps" / "other"
		other_root.mkdir()
		(other_root / "main.py").write_text("needle\n")
		self.backend = ReadOnlySourceBackend({"sample": self.root, "other": other_root})
		self.assertEqual([match["path"] for match in self.matches()], ["/other/main.py", "/sample/main.py"])
		self.assertEqual([match["path"] for match in self.matches(path="/other")], ["/other/main.py"])

	def test_gitignore_rules_include_nested_overrides_and_parent_rules(self):
		self.write(".gitignore", "generated/\n*.log\n/root_only.py\n")
		self.write("src/.gitignore", "!keep.log\nlocal.py\n")
		for path in ["generated/result.py", "debug.log", "root_only.py", "src/debug.log", "src/local.py"]:
			self.write(path)
		self.write("src/keep.log")
		self.write("src/root_only.py")
		expected = ["/sample/src/keep.log", "/sample/src/root_only.py"]
		for path in ["/", "/sample/src"]:
			with self.subTest(path=path):
				self.assertEqual(sorted(match["path"] for match in self.matches(path=path)), expected)
		self.assertEqual(self.matches(path="/sample/generated"), [])
		self.assertEqual(self.matches(path="/sample/src/debug.log"), [])

	def test_long_lines_are_truncated_after_matching(self):
		self.write("minified.js", "é" * 1000 + "needle\n")
		match = self.matches()[0]
		self.assertEqual(match["line"], 1)
		self.assertEqual(len(match["text"]), 300)
		self.assertTrue(match["text"].endswith("…"))

	def test_hidden_files_and_external_symlinks_are_excluded(self):
		self.write(".secret.py")
		self.write(".hidden/source.py")
		outside = self.bench / "private.py"
		outside.write_text("needle\n")
		(self.root / "linked.py").symlink_to(outside)
		for path in ["/", "/sample/.secret.py", "/sample/.hidden"]:
			with self.subTest(path=path):
				self.assertEqual(self.matches(path=path), [])
		for path in ["/sample/linked.py", "/sample/../../private.py", "/uninstalled"]:
			with self.subTest(path=path):
				self.assertTrue(self.backend.grep("needle", path=path).error)

	def test_binary_files_are_excluded(self):
		(self.root / "binary").write_bytes(b"needle\x00binary\n")
		(self.root / "invalid").write_bytes(b"needle\xff\n")
		(self.root / "invalid_later").write_bytes(b"needle\n\xff\n")
		self.assertEqual(self.matches(), [])

	def test_invalid_regex_returns_an_error(self):
		self.assertIn("Invalid regex", self.backend.grep("[").error)

	def test_python_fallback_has_the_same_results_as_ripgrep(self):
		if not shutil.which("rg"):
			self.skipTest("ripgrep is not installed")
		self.write("src/main.py", "needle = 1\nneedle = 2\nother\n")
		self.write("src/a:b\nc.js", "needle\n")
		self.write("src/long.py", "x" * 1000 + "needle\n")
		self.write("src/unicode.py", "İı \u017f \u212a\nx\u0301needle\n")
		(self.root / "crlf.py").write_bytes(b"needle\r\nneedle\r\n")
		(self.root / "cr.py").write_bytes(b"needle\rneedle\r")
		self.write(".gitignore", "ignored/\n")
		self.write("ignored/result.py")
		for pattern in [
			"needle",
			r"needle = \d",
			"^other$",
			"absent",
			r"(?<=needle) = 1",
			r"\bneedle",
			"(?i)i",
			"^needle$",
			r"\Aneedle",
			"",
			"^$",
		]:
			for path, glob in [("/", None), ("/sample/src", "**/*.py"), ("/sample/src/main.py", None)]:
				with self.subTest(pattern=pattern, path=path, glob=glob):
					native = self.matches(pattern, path=path, glob=glob)
					with patch("ask_alyf.ask_alyf.source_grep.shutil.which", return_value=None):
						fallback = self.matches(pattern, path=path, glob=glob)
					self.assertEqual(native, fallback)

	def test_native_search_runs_without_python_fallback(self):
		if not shutil.which("rg"):
			self.skipTest("ripgrep is not installed")
		self.write("src/main.py", "first\nneedle = 1\n")
		self.write("src/long.py", "é" * 1000 + "needle\n")
		self.write("src/a:b\nc.js")
		with patch.object(SourceGrep, "_python_matches", side_effect=AssertionError("Unexpected fallback")):
			matches = self.matches(glob="**/*.{py,js}")
			self.assertEqual(len(matches), 3)
			self.assertEqual(matches[-1], {"path": "/sample/src/main.py", "line": 2, "text": "needle = 1"})
			self.assertEqual(self.matches("absent"), [])

	def test_native_failures_fall_back_to_python(self):
		self.write("main.py")
		for failure in [FileNotFoundError(), subprocess.TimeoutExpired("rg", 30)]:
			with (
				self.subTest(failure=failure),
				patch("ask_alyf.ask_alyf.source_grep.shutil.which", return_value="rg"),
				patch("ask_alyf.ask_alyf.source_grep.subprocess.run", side_effect=failure),
			):
				self.assertEqual(len(self.matches()), 1)
		with (
			patch("ask_alyf.ask_alyf.source_grep.shutil.which", return_value="rg"),
			patch(
				"ask_alyf.ask_alyf.source_grep.subprocess.run",
				return_value=subprocess.CompletedProcess([], 2),
			),
		):
			self.assertEqual(len(self.matches()), 1)


class UnitTestSourceGrepBenchmark(UnitTestCase):
	@skipUnless(os.environ.get("ASK_ALYF_BENCHMARK_SOURCE_GREP"), "Opt-in source grep benchmark")
	def test_installed_app_search_timings(self):
		backend = ReadOnlySourceBackend(get_installed_app_roots())
		for engine in ["auto", "python"]:
			with (
				patch("ask_alyf.ask_alyf.source_grep.shutil.which", return_value=None)
				if engine == "python"
				else nullcontext()
			):
				self.measure_searches(backend, engine)

	def measure_searches(self, backend, engine):
		for path, glob in [("/", "**/*"), ("/", "**/*.py"), ("/ask_alyf", "**/*")]:
			with self.subTest(path=path, glob=glob):
				file_count = 0
				collect_files = backend._collect_files

				def counted_files(path):
					nonlocal file_count
					files = collect_files(path)
					file_count = len(files)
					return files

				with patch.object(backend, "_collect_files", side_effect=counted_files):
					start = perf_counter()
					result = backend.grep(r"frappe\.whitelist", path=path, glob=glob)
					elapsed = perf_counter() - start
				self.assertIsNone(result.error)
				print(
					f"Source grep: engine={engine}, path={path}, glob={glob}, files={file_count}, "
					f"matches={len(result.matches)}, seconds={elapsed:.3f}",
					flush=True,
				)

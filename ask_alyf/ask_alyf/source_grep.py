from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from deepagents.backends.protocol import GrepMatch

MAX_MATCH_LINE_CHARS = 300
# These expressions have different line or Unicode semantics in Rust regex.
PYTHON_ONLY_REGEX = re.compile(r"\\[AbBsSwWZzrn]|\(\?[aiLmsux-]|\[\[|[\^$]")


class SourceGrep:
	"""Search the same scoped files with ripgrep or the portable Python fallback."""

	def __init__(self, files: dict[Path, str], regex: re.Pattern):
		self.files = files
		self.regex = regex

	def matches(self) -> list[GrepMatch]:
		if not self.files:
			return []
		matches = None
		ripgrep = shutil.which("rg")
		# Python searches each line separately, including the final empty line.
		if ripgrep and not self.regex.search("") and not PYTHON_ONLY_REGEX.search(self.regex.pattern):
			matches = self._native_matches(ripgrep)
		if matches is None:
			matches = self._python_matches()
		return sorted(matches, key=lambda match: (match["path"], match["line"]))

	def _native_matches(self, ripgrep: str) -> list[GrepMatch] | None:
		matches = []
		base = Path(os.path.commonpath(self.files)).parent
		paths = {"/".join(file.parts[len(base.parts) :]): file for file in self.files}
		# Explicit paths preserve our virtual-path glob rules and avoid argument limits.
		for files in _path_batches(paths):
			try:
				result = subprocess.run(
					[
						ripgrep,
						"--no-config",
						"--json",
						"--threads=4",
						"--encoding=none",
						"--crlf",
						"--regexp",
						self.regex.pattern,
						"--",
						*files,
					],
					cwd=base,
					stdout=subprocess.PIPE,
					stderr=subprocess.DEVNULL,
					timeout=15,
					check=False,
				)
			except OSError, subprocess.TimeoutExpired:
				return None
			# Unsupported regex syntax (for example lookbehind) uses Python as well.
			if result.returncode not in (0, 1):
				return None
			matches.extend(self._parse_matches(result.stdout, paths))
		return matches

	def _parse_matches(self, output: bytes, paths: dict[str, Path]) -> list[GrepMatch]:
		matches = []
		file_matches = []
		invalid_text = False
		for raw in output.splitlines():
			event = json.loads(raw)
			data = event["data"]
			if event["type"] == "begin":
				file_matches = []
				invalid_text = False
			elif event["type"] == "match":
				try:
					path = paths.get(_decode_field(data["path"]))
					line = _decode_field(data["lines"]).removesuffix("\n").removesuffix("\r")
				except UnicodeError:
					invalid_text = True
					continue
				if path in self.files and self.regex.search(line):
					file_matches.append(self._match(path, data["line_number"], line))
			elif event["type"] == "end" and not invalid_text and data["binary_offset"] is None:
				if file_matches:
					# Invalid bytes can occur after the last match. Validate the whole file,
					# as the Python fallback does, and normalize legacy CR line endings.
					content = self._read_text(path)
					if content is not None:
						if "\r" in content:
							file_matches = self._match_lines(path, content)
						matches.extend(file_matches)
		return matches

	def _python_matches(self) -> list[GrepMatch]:
		matches = []
		for file in self.files:
			content = self._read_text(file)
			if content is not None:
				matches.extend(self._match_lines(file, content))
		return matches

	def _read_text(self, file: Path) -> str | None:
		try:
			content = file.read_bytes().decode("utf-8")
		except OSError, UnicodeError:
			return None
		return None if "\0" in content else content

	def _match_lines(self, file: Path, content: str) -> list[GrepMatch]:
		lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
		return [
			self._match(file, number, line) for number, line in enumerate(lines, 1) if self.regex.search(line)
		]

	def _match(self, file: Path, line_number: int, line: str) -> GrepMatch:
		if len(line) > MAX_MATCH_LINE_CHARS:
			line = line[: MAX_MATCH_LINE_CHARS - 1] + "…"
		return GrepMatch(path=self.files[file], line=line_number, text=line)


def _decode_field(field: dict[str, str]) -> str:
	if "text" in field:
		return field["text"]
	return base64.b64decode(field["bytes"]).decode("utf-8")


def _path_batches(paths: dict[str, Path]):
	batch = []
	size = 0
	for path in paths:
		path_size = len(os.fsencode(path)) + 1
		if batch and size + path_size > 96_000:
			yield batch
			batch = []
			size = 0
		batch.append(path)
		size += path_size
	if batch:
		yield batch

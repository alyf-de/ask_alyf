from __future__ import annotations

import os
from pathlib import Path

from pathspec import GitIgnoreSpec


class SourceTree:
	"""Discover source files without descending into dependencies or build output.

	Ignore files are read directly, so source archives need neither Git nor .git.
	Discovery skips symlinks; explicit reads still use the backend's confinement check.
	"""

	def __init__(self, app_root: Path, *, include_hidden: bool = False):
		self.root = app_root.resolve()
		self.include_hidden = include_hidden

	def entries(self, target: Path, *, recursive: bool) -> list[Path]:
		target = target.resolve()
		rules = []
		current = self.root
		for part in target.relative_to(self.root).parts:
			rules = self._directory_rules(current, rules)
			current /= part
			if self._excluded(current, current.is_dir(), rules):
				return []
		if target.is_file():
			return [target]

		results = []
		pending = [(target, rules)]
		while pending:
			directory, parent_rules = pending.pop()
			rules = self._directory_rules(directory, parent_rules)
			try:
				with os.scandir(directory) as entries:
					children = sorted(entries, key=lambda entry: entry.name.lower())
			except OSError:
				continue
			for entry in children:
				if entry.is_symlink():
					continue
				is_dir = entry.is_dir(follow_symlinks=False)
				if not is_dir and not entry.is_file(follow_symlinks=False):
					continue
				child = directory / entry.name
				if self._excluded(child, is_dir, rules):
					continue
				results.append(child)
				if recursive and is_dir:
					pending.append((child, rules))
		return results

	def _excluded(self, path: Path, is_dir: bool, rules: list[tuple[Path, GitIgnoreSpec]]) -> bool:
		if not self.include_hidden and path.name.startswith("."):
			return True
		if path.name in {"node_modules", "__pycache__"} or path.suffix == ".map":
			return True
		parts = path.parts[len(self.root.parts) :]
		if parts[-2:] == ("public", "dist") or parts[-3:] == ("public", "frontend", "assets"):
			return True
		for base, spec in reversed(rules):
			relative = "/".join(path.parts[len(base.parts) :]) + ("/" if is_dir else "")
			match = spec.check_file(relative)
			if match.include is not None:
				return match.include
		return False

	def _directory_rules(
		self, directory: Path, rules: list[tuple[Path, GitIgnoreSpec]]
	) -> list[tuple[Path, GitIgnoreSpec]]:
		ignore_file = directory / ".gitignore"
		if ignore_file.is_symlink():
			return rules
		try:
			lines = ignore_file.read_text(encoding="utf-8").splitlines()
		except (OSError, UnicodeError):
			return rules
		return [*rules, (directory, GitIgnoreSpec.from_lines(lines))]

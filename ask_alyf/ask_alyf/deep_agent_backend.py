from __future__ import annotations

import asyncio
import base64
import fnmatch
from pathlib import Path
from typing import Any

import frappe
import wcmatch.glob as wcglob
from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import (
	BackendProtocol,
	EditResult,
	FileData,
	FileInfo,
	GlobResult,
	GrepMatch,
	GrepResult,
	LsResult,
	ReadResult,
	WriteResult,
)
from frappe import _

from ask_alyf.ask_alyf.tools import (
	_get_accessible_file_doc,
	build_code_path_entry,
	ensure_code_search_enabled,
	get_installed_app_roots,
	get_path_parts,
	is_hidden_path,
	is_path_within,
	iter_scoped_entries,
	iter_scoped_files,
	resolve_installed_app_path,
	to_app_relative_path,
	to_bench_relative_path,
)

SOURCE_READ_ONLY_ERROR = _("Source code is read-only")
ATTACHMENT_READ_ONLY_ERROR = _("Attachments are read-only")


def _bench_relative_to_source_path(bench_relative: str) -> str:
	"""Convert a bench-relative path (`apps/frappe/...`) to a source virtual path (`/frappe/...`)."""
	parts = bench_relative.split("/")
	if parts and parts[0] == "apps":
		parts = parts[1:]
	return "/" + "/".join(parts)


def _decode_file_bytes(raw: bytes) -> tuple[str, str]:
	"""Decode bytes into `(content, encoding)` for `FileData`."""
	try:
		return raw.decode("utf-8"), "utf-8"
	except UnicodeDecodeError:
		return base64.b64encode(raw).decode("ascii"), "base64"


def _slice_text(content: str, offset: int, limit: int) -> str | None:
	"""Return the requested line window or `None` if offset exceeds file length."""
	if not content or content.strip() == "":
		return content
	lines = content.splitlines(keepends=True)
	if offset >= len(lines):
		return None
	end = min(offset + limit, len(lines))
	return "".join(lines[offset:end]).replace("\r\n", "\n").replace("\r", "\n")


class ReadOnlySourceBackend(BackendProtocol):
	"""Read-only backend exposing installed app source code under `/source/`.

	The composite router strips the `/source/` prefix before dispatching, so
	methods here receive paths like `/frappe/frappe/__init__.py`. The first
	segment is the installed app name; the remainder (including any same-named
	package directory) is resolved within that app root via
	`resolve_installed_app_path`, which confines the target to the app root
	and rejects traversal.
	"""

	def _split_app(self, path: str) -> tuple[str, str]:
		"""Split a backend path into `(app_name, full_relative)`.

		`full_relative` retains the leading app-name segment so that
		`resolve_installed_app_path` strips exactly one app-name prefix.
		"""
		relative = path.lstrip("/")
		parts = get_path_parts(relative)
		if not parts:
			frappe.throw(_("Path must start with an installed app name."))
		return parts[0], relative

	def _resolve(self, path: str) -> tuple[Path, Path]:
		app_name, full_relative = self._split_app(path)
		return resolve_installed_app_path(app_name, full_relative)

	def _collect_files(self, path: str | None) -> list[tuple[Path, Path]]:
		relative = (path or "").lstrip("/")
		if not relative:
			ensure_code_search_enabled()
			files: list[tuple[Path, Path]] = []
			for _app_name, app_root in get_installed_app_roots().items():
				for entry in iter_scoped_files(app_root, app_root, include_hidden=False):
					files.append((app_root, entry))
			return files
		app_root, target = self._resolve(relative)
		return [(app_root, fp) for fp in iter_scoped_files(app_root, target, include_hidden=False)]

	def ls(self, path: str) -> LsResult:
		try:
			relative = path.lstrip("/")
			if not relative:
				ensure_code_search_enabled()
				app_roots = get_installed_app_roots()
				entries = [
					FileInfo(path=f"/{name}", is_dir=True, size=0, modified_at="")
					for name in sorted(app_roots)
				]
				return LsResult(entries=entries)

			app_root, target = self._resolve(relative)
			entries: list[FileInfo] = []
			if target.is_file():
				entry = build_code_path_entry(app_root, target)
				entries.append(
					FileInfo(
						path=_bench_relative_to_source_path(entry["path"]),
						is_dir=False,
						size=int(entry.get("size") or 0),
						modified_at="",
					)
				)
			else:
				for child in iter_scoped_entries(app_root, target, recursive=False, include_hidden=False):
					entry = build_code_path_entry(app_root, child)
					entries.append(
						FileInfo(
							path=_bench_relative_to_source_path(entry["path"]),
							is_dir=entry["type"] == "directory",
							size=int(entry.get("size") or 0),
							modified_at="",
						)
					)
			entries.sort(key=lambda info: info.get("path", ""))
			return LsResult(entries=entries)
		except Exception as exc:
			return LsResult(error=str(exc))

	def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
		try:
			relative = file_path.lstrip("/")
			if not relative:
				return ReadResult(error=_("Source file path is required"))
			app_root, target = self._resolve(relative)
			if is_hidden_path(app_root, target):
				return ReadResult(error=_("Access to hidden paths is not allowed"))
			if not target.is_file():
				return ReadResult(error=_("Path is not a file: {0}").format(file_path))
			raw = target.read_bytes()
			content, encoding = _decode_file_bytes(raw)
			if encoding == "base64":
				return ReadResult(file_data=FileData(content=content, encoding=encoding))
			sliced = _slice_text(content, offset, limit)
			if sliced is None:
				return ReadResult(error=_("Line offset {0} exceeds file length").format(offset))
			return ReadResult(file_data=FileData(content=sliced, encoding=encoding))
		except Exception as exc:
			return ReadResult(error=str(exc))

	def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
		try:
			files = self._collect_files(path)
			matches: list[GrepMatch] = []
			for _app_root, file_path in files:
				if glob and not fnmatch.fnmatch(file_path.name, glob):
					continue
				try:
					content = file_path.read_text(encoding="utf-8")
				except Exception:
					continue
				source_path = _bench_relative_to_source_path(to_bench_relative_path(file_path))
				for line_num, line in enumerate(content.split("\n"), 1):
					if pattern in line:
						matches.append(GrepMatch(path=source_path, line=int(line_num), text=line))
			return GrepResult(matches=matches)
		except Exception as exc:
			return GrepResult(error=str(exc))

	def glob(self, pattern: str, path: str | None = None) -> GlobResult:
		try:
			files = self._collect_files(path)
			bare_pattern = pattern.lstrip("/")
			matches: list[FileInfo] = []
			for _app_root, file_path in files:
				source_path = _bench_relative_to_source_path(to_bench_relative_path(file_path))
				relative = source_path.lstrip("/")
				if wcglob.globmatch(relative, bare_pattern, flags=wcglob.BRACE | wcglob.GLOBSTAR):
					try:
						size = int(file_path.stat().st_size)
					except OSError:
						size = 0
					matches.append(FileInfo(path=source_path, is_dir=False, size=size, modified_at=""))
			matches.sort(key=lambda info: info.get("path", ""))
			return GlobResult(matches=matches)
		except Exception as exc:
			return GlobResult(error=str(exc))

	def write(self, file_path: str, content: str) -> WriteResult:
		return WriteResult(error=str(SOURCE_READ_ONLY_ERROR))

	def edit(
		self,
		file_path: str,
		old_string: str,
		new_string: str,
		replace_all: bool = False,
	) -> EditResult:
		return EditResult(error=str(SOURCE_READ_ONLY_ERROR))

	async def als(self, path: str) -> LsResult:
		return await asyncio.to_thread(self.ls, path)

	async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
		return await asyncio.to_thread(self.read, file_path, offset, limit)

	async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
		return await asyncio.to_thread(self.grep, pattern, path, glob)

	async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
		return await asyncio.to_thread(self.glob, pattern, path)

	async def awrite(self, file_path: str, content: str) -> WriteResult:
		return WriteResult(error=str(SOURCE_READ_ONLY_ERROR))

	async def aedit(
		self,
		file_path: str,
		old_string: str,
		new_string: str,
		replace_all: bool = False,
	) -> EditResult:
		return EditResult(error=str(SOURCE_READ_ONLY_ERROR))


class ReadOnlyAttachmentBackend(BackendProtocol):
	"""Read-only backend exposing Frappe **File** attachments under `/attachments/`.

	The composite router strips the `/attachments/` prefix before dispatching,
	so methods here receive paths like `/FILE00123` that resolve to a Frappe
	**File** document by name. Every read enforces existence and read permission
	via the shared `_get_accessible_file_doc` helper.
	"""

	def ls(self, path: str) -> LsResult:
		return LsResult(entries=[])

	def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
		try:
			name = file_path.lstrip("/")
			if not name:
				return ReadResult(error=_("Attachment name is required"))
			file_doc = _get_accessible_file_doc(file_id=name)
			content = file_doc.get_content()
			if isinstance(content, bytes):
				text, encoding = _decode_file_bytes(content)
			else:
				text, encoding = str(content), "utf-8"
			if encoding == "base64":
				return ReadResult(file_data=FileData(content=text, encoding=encoding))
			sliced = _slice_text(text, offset, limit)
			if sliced is None:
				return ReadResult(error=_("Line offset {0} exceeds file length").format(offset))
			return ReadResult(file_data=FileData(content=sliced, encoding=encoding))
		except Exception as exc:
			return ReadResult(error=str(exc))

	def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
		return GrepResult(matches=[])

	def glob(self, pattern: str, path: str | None = None) -> GlobResult:
		return GlobResult(matches=[])

	def write(self, file_path: str, content: str) -> WriteResult:
		return WriteResult(error=str(ATTACHMENT_READ_ONLY_ERROR))

	def edit(
		self,
		file_path: str,
		old_string: str,
		new_string: str,
		replace_all: bool = False,
	) -> EditResult:
		return EditResult(error=str(ATTACHMENT_READ_ONLY_ERROR))

	async def als(self, path: str) -> LsResult:
		return await asyncio.to_thread(self.ls, path)

	async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
		return await asyncio.to_thread(self.read, file_path, offset, limit)

	async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
		return await asyncio.to_thread(self.grep, pattern, path, glob)

	async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
		return await asyncio.to_thread(self.glob, pattern, path)

	async def awrite(self, file_path: str, content: str) -> WriteResult:
		return WriteResult(error=str(ATTACHMENT_READ_ONLY_ERROR))

	async def aedit(
		self,
		file_path: str,
		old_string: str,
		new_string: str,
		replace_all: bool = False,
	) -> EditResult:
		return EditResult(error=str(ATTACHMENT_READ_ONLY_ERROR))


def build_ask_alyf_backend() -> CompositeBackend:
	"""Build the restricted composite virtual filesystem for Ask ALYF agents.

	Mounts:
	  - `/workspace/` (default): checkpointed writable scratch space via
		`StateBackend` — the only mount that accepts writes.
	  - `/source/`: read-only access to installed app source code, confined
		to installed app roots and gated by `ensure_code_search_enabled`.
	  - `/attachments/`: read-only access to Frappe **File** documents with
		per-read permission checks.
	"""
	return CompositeBackend(
		default=StateBackend(),
		routes={
			"/source/": ReadOnlySourceBackend(),
			"/attachments/": ReadOnlyAttachmentBackend(),
		},
	)

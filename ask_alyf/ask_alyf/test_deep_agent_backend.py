# Copyright (c) 2026, ALYF GmbH and Contributors
# See license.txt

from __future__ import annotations

from unittest.mock import patch

import frappe
from deepagents.backends import StateBackend
from frappe.tests.utils import FrappeTestCase

from ask_alyf.ask_alyf import tools
from ask_alyf.ask_alyf.deep_agent_backend import (
	ReadOnlyAttachmentBackend,
	ReadOnlySourceBackend,
	_attachment_read_only_error,
	_source_read_only_error,
	build_ask_alyf_backend,
)


def _app_roots():
	return tools.get_installed_app_roots()


class UnitTestAskALYFVirtualFilesystem(FrappeTestCase):
	def test_build_ask_alyf_backend_mounts_workspace_source_and_attachments(self):
		backend = build_ask_alyf_backend(_app_roots())
		route_prefixes = {prefix for prefix, _ in backend.sorted_routes}
		self.assertIn("/source/", route_prefixes)
		self.assertIn("/attachments/", route_prefixes)
		# The default backend is the writable StateBackend.
		self.assertIsInstance(backend.default, StateBackend)

	def test_workspace_routes_to_writable_default_backend(self):
		backend = build_ask_alyf_backend(_app_roots())
		target, stripped = backend._get_backend_and_key("/workspace/scratch.txt")
		self.assertIs(target, backend.default)
		# /workspace/ is not a declared route, so the path is passed through unchanged.
		self.assertEqual(stripped, "/workspace/scratch.txt")

	def test_source_routes_to_read_only_source_backend(self):
		backend = build_ask_alyf_backend(_app_roots())
		target, stripped = backend._get_backend_and_key("/source/ask_alyf/agent.py")
		self.assertIsInstance(target, ReadOnlySourceBackend)
		self.assertEqual(stripped, "/ask_alyf/agent.py")

	def test_attachments_routes_to_read_only_attachment_backend(self):
		backend = build_ask_alyf_backend(_app_roots())
		target, stripped = backend._get_backend_and_key("/attachments/FILE-0001")
		self.assertIsInstance(target, ReadOnlyAttachmentBackend)
		self.assertEqual(stripped, "/FILE-0001")

	def test_source_backend_rejects_writes_and_edits(self):
		backend = build_ask_alyf_backend(_app_roots())
		write_result = backend.write("/source/ask_alyf/new_file.py", "print('hi')")
		self.assertEqual(write_result.error, _source_read_only_error())

		edit_result = backend.edit(
			"/source/ask_alyf/agent.py",
			"old",
			"new",
		)
		self.assertEqual(edit_result.error, _source_read_only_error())

	def test_attachments_backend_rejects_writes_and_edits(self):
		backend = build_ask_alyf_backend(_app_roots())
		write_result = backend.write("/attachments/FILE-0001", "overwrite")
		self.assertEqual(write_result.error, _attachment_read_only_error())

		edit_result = backend.edit(
			"/attachments/FILE-0001",
			"old",
			"new",
		)
		self.assertEqual(edit_result.error, _attachment_read_only_error())

	def test_source_backend_blocks_path_traversal(self):
		backend = build_ask_alyf_backend(_app_roots())
		result = backend.read("/source/ask_alyf/../../../../etc/passwd")
		# Traversal escapes the app root; the read returns an error result
		# rather than host filesystem contents.
		self.assertTrue(result.error)
		self.assertIsNone(result.file_data)
		# The error must be the confinement error, not a code-search-disabled error.
		self.assertIn("installed app", result.error)

	def test_source_backend_excludes_hidden_paths(self):
		backend = build_ask_alyf_backend(_app_roots())
		result = backend.read("/source/ask_alyf/.gitignore")
		self.assertEqual(result.error, "Access to hidden paths is not allowed")
		self.assertIsNone(result.file_data)

	def test_source_backend_ls_root_handles_empty_app_roots_gracefully(self):
		backend = build_ask_alyf_backend({})
		result = backend.ls("/source/")
		self.assertFalse(result.error)
		self.assertEqual(result.entries, [])

	def test_source_backend_read_returns_content_for_real_app_file(self):
		backend = build_ask_alyf_backend(_app_roots())
		result = backend.read("/source/ask_alyf/ask_alyf/ask_alyf/agent.py")
		# The file exists in the installed ask_alyf app, so we get content back.
		self.assertIsNone(result.error)
		self.assertIsNotNone(result.file_data)
		self.assertIn("ask_alyfAgentRunner", result.file_data["content"])

	def test_attachment_backend_read_enforces_permission_checks(self):
		backend = build_ask_alyf_backend(_app_roots())
		with patch(
			"ask_alyf.ask_alyf.deep_agent_backend._get_accessible_file_doc",
			side_effect=frappe.PermissionError("no read access"),
		):
			result = backend.read("/attachments/FILE-PRIVATE")
		self.assertTrue(result.error)
		self.assertIsNone(result.file_data)

	def test_attachment_backend_read_rejects_empty_name(self):
		backend = build_ask_alyf_backend(_app_roots())
		result = backend.read("/attachments/")
		self.assertTrue(result.error)
		self.assertIsNone(result.file_data)

	def test_workspace_scratch_does_not_survive_across_backend_instances(self):
		"""Each `build_ask_alyf_backend(_app_roots())` call produces a fresh, independent
		`StateBackend`. There is no checkpointer, so scratch written through one
		backend instance cannot surface in another.
		"""
		backend1 = build_ask_alyf_backend(_app_roots())
		self.assertIsInstance(backend1.default, StateBackend)

		backend2 = build_ask_alyf_backend(_app_roots())
		self.assertIsInstance(backend2.default, StateBackend)
		# Distinct instances: scratch state is per-backend, never shared.
		self.assertIsNot(backend1.default, backend2.default)

		# StateBackend reads/writes through the live LangGraph config and keeps no
		# standalone store, so reading outside a graph execution has no scratch to
		# return. This is what makes /workspace/ an in-memory scratch mount whose
		# contents do not survive across separate backend (and therefore separate
		# agent) instances.
		with self.assertRaises(RuntimeError):
			backend1.default.read("/workspace/scratch.txt")
		with self.assertRaises(RuntimeError):
			backend2.default.read("/workspace/scratch.txt")

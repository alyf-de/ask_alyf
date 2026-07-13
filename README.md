# Ask ALYF

Ask ALYF adds an assistant to ERPNext so users can ask questions, find information, and get help working with documents without leaving the Desk.

It is built for teams that already use ERPNext and want a practical assistant inside their existing system, not a separate chat product with a separate permission model.

## What You Can Do

Use Ask ALYF to:

- ask questions about records, reports, configuration, and installed apps
- summarize documents and data that the current user is allowed to see
- create charts from ERPNext data
- generate PDFs from existing documents
- upload files and extract information from PDFs or images, when file upload is enabled
- navigate around Desk, open new forms, and jump to relevant fields
- create or update ERPNext documents in Agent mode, after explicit confirmation

Answers are shown in the chat bubble and can include formatted text, tables, code blocks, file links, and charts.

## Two Modes

Ask mode is for questions and research. The assistant can read permitted data, explain records, look up configuration, search documentation, and help users understand what is already in the system.

Agent mode is for taking action. The assistant can prepare changes such as creating, updating, submitting, cancelling, amending, renaming, or deleting documents. Before anything is changed, the user sees a proposal and must approve it.

## Field Writing Help

Ask ALYF can also add a small sparkles button next to long text fields on Desk forms. Users can describe what they want written, and the assistant fills the field in place.

This is useful for descriptions, emails, internal notes, terms, and other longer text fields. After text is inserted, an *Undo* action is available for a few seconds.

The field assistant is available only to users with the **Ask ALYF User** role and when _Allow Field Agent_ is enabled in **Ask ALYF Settings**.

## Built For Frappe

Ask ALYF stays close to the Frappe Framework. It uses the existing Desk, the standard file uploader, **Frappe Charts**, and the same role-based permissions users already have in ERPNext.

The assistant does not become an all-powerful back door. If a user cannot read or change something through Frappe permissions, Ask ALYF should not be able to do it for them either.

## Voice Input

Users can type messages. In browsers that support the Web Speech API, the chat bubble also offers microphone input that turns speech into text.

## Access And Safety

Access is granted through the **Ask ALYF User** role.

The assistant works with the permissions of the logged-in user. A user with limited access only gets limited answers and limited actions; an Administrator can do more because the Administrator can already do more in ERPNext.

Agent mode has additional guardrails:

- Every write operation requires explicit user confirmation before execution.
- Bulk writes are intentionally limited. `batch_insert` can create multiple records of the same DocType, but each row still goes through framework validation.
- Framework rules are respected. For example, submitted documents must be cancelled before they can be deleted.
- Administrators can exclude selected DocTypes from Agent mode in **Ask ALYF Settings**.

Read-only SQL is available only to Administrator and System Manager users.

## Configuration

Configure Ask ALYF in **Ask ALYF Settings**.

Common settings include:

- _Enabled_
- _LLM Provider_
- _Base URL_ for OpenAI-compatible providers
- _API Key_
- _Chat Model_
- _Allow Agent Mode_
- _Allow Code Search_
- _Allow File Upload_
- _Allow Field Agent_
- _Excluded DocTypes_
- separate vision model settings, if document and image extraction should use a different model

## Conversation History

Conversations are stored in the **Ask ALYF Conversation** DocType, which keeps messages available across page reloads and provides an audit trail for Agent mode proposals and actions.

The coordinator rebuilds the full conversation history as native LangChain messages on each turn from the stored `messages_json` field. This keeps the source of truth in a single, user-visible DocType that respects owner-only permissions.

Old conversations are deleted after 90 days by default. The retention period can be configured per site through **Log Settings**.

## Technical Reference

Ask ALYF uses tool calls to read data, inspect metadata, navigate the Desk, render charts, and prepare document changes. Data-access tools wrap Frappe APIs such as `frappe.client`, so normal framework permissions and validations continue to apply.

### Ask Mode Tools

Data retrieval:

- `get_list` lists records with filters, fields, ordering, pagination, and `group_by` aggregation
- `get_count` counts matching records
- `get` reads one permitted document
- `get_value` reads selected field values
- `get_single_value` reads a field from a Single DocType

Schema, permissions, and UI:

- `get_meta` reads DocType metadata
- `has_permission` checks whether the current user has a permission on a document
- `get_doc_permissions` returns the evaluated permissions for a document
- `list_accessible_doctypes` lists DocTypes the current user can read or write
- `list_accessible_reports` lists reports the current user can access
- `translate_ui_labels` translates UI labels so answers match the user's language

Files, printing, charts, and navigation:

- `get_print` generates a PDF print for a permitted document
- `get_file_id` resolves a **File** ID from an attachment reference
- `read_file_record` reads content from **File** records
- `extract_document_data` extracts structured data from PDF or image **File** records
- `show_chart` renders one or more **Frappe Charts** below an assistant message
- `set_route` navigates to a Desk route
- `new_doc` opens a new document form with optional defaults
- `scroll_to_field` scrolls to a field on the active form

Optional tools:

- `run_read_only_sql` runs read-only SQL for Administrator and System Manager users
- `get_app_version`, `read_github_releases`, and `read_documentation_page` help answer app and documentation questions

### Subagents

The coordinator delegates specialized work to restricted Deep Agents subagents via the built-in `task` tool. Each subagent has an explicit, minimal tool list so the parent's proposal and mutation tools are never inherited.

- `source-code-analyzer` is registered only when _Allow Code Search_ is enabled. It searches and reads installed app code through the read-only `/source/` virtual filesystem mount and returns a structured `SourceCodeAnalysisResult`.
- `document-planner` is registered only in Agent mode. It inspects metadata and documents with read-only tools and returns a `DocumentPlannerResult` that the coordinator turns into a confirmed proposal.
- A built-in `general-purpose` subagent is provided by Deep Agents for open-ended delegation.

### Todos and Virtual Filesystem

The coordinator uses Deep Agents' built-in `write_todos` planning state to track its own progress. Todos are internal scratch state and are not mapped to Frappe **ToDo** records.

The agent operates against a restricted composite virtual filesystem:

- `/workspace/` is an in-memory scratch mount backed by `StateBackend`. It is _not_ directly writable by the model: filesystem writes are denied by the harness profile (which excludes `write_file`, `edit_file`, and `execute`) and by a blanket `FilesystemPermission` deny rule on `/**`. Planning and progress tracking happen through the built-in `write_todos` tool, not through filesystem writes.
- `/source/` exposes installed app source code read-only, confined to installed app roots and gated by _Allow Code Search_.
- `/attachments/` exposes Frappe **File** documents read-only, with per-read permission checks.

Host filesystem writes, shell execution, and the built-in `write_file`/`edit_file`/`execute` tools are excluded from the model-visible tool surface.

### Agent Mode Tools

Agent mode includes the Ask mode tools plus tools that prepare confirmed actions:

- `insert` creates a document
- `batch_insert` creates multiple documents of the same DocType
- `save` updates a document
- `set_value` updates selected fields
- `submit` submits a submittable document
- `cancel` cancels a submitted document
- `amend` creates an amended copy of a cancelled document
- `delete` deletes a document when framework rules allow it
- `rename_doc` renames a document
- `attach_file` attaches a **File** to a document
- `run_whitelisted_method` calls an accessible `@frappe.whitelist()` method
- `frm_set_value` sets a field on the active form in the browser
- `frm_add_child` adds a child table row on the active form in the browser

### Request Context

Each request can include the current Desk route, active document `doctype` and `name`, list view filters, user language, user defaults, and user roles.

### Error Handling

Permission errors, validation errors, missing records, and LLM provider failures are shown as user-facing messages instead of raw tracebacks. If the assistant refers to a missing DocType or field, it can re-check metadata before retrying.

## Dependencies

- [Deep Agents](https://github.com/langchain-ai/deepagents) for agent orchestration, native messages, subagents, todos, and the virtual filesystem
- [LangChain](https://github.com/langchain-ai/langchain) and [langgraph](https://github.com/langchain-ai/langgraph) for the agent runtime
- [langchain-openai](https://github.com/langchain-ai/langchain) for the OpenAI-compatible chat model adapter
- [any-llm-sdk](https://github.com/mozilla-ai/any-llm) for LLM provider discovery and vision extraction
- [PyMuPDF](https://pymupdf.readthedocs.io/) for PDF handling

## Installation

Install this app with the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app ask_alyf
```

## Cursor Cloud Agents

This repository includes a Cursor cloud-agent setup in `.cursor/` for bootstrapping a self-contained Frappe bench on the remote machine.

To use it:

1. Open the `ask_alyf` repository itself in Cursor, not your local bench root.
2. Rebuild the cloud-agent environment so Cursor picks up `.cursor/environment.json`.
3. Let the agent finish the initial install. It creates a bench in `$HOME/frappe-bench`, starts MariaDB and Redis, creates a default site, and soft-links the checked-out repo into the bench.

Useful variables in `.cursor/install.sh`:

- `REPO_NAME` defaults to `ask_alyf` and is used for the bench app path and default site name.
- `SITE_NAME` overrides the default site, which otherwise becomes `<repo-name>.localhost`.
- `BENCH_ROOT` overrides the bench location.
- `FRAPPE_BRANCH` lets you pin a different Frappe branch.

Troubleshooting:

- If you change `.cursor/Dockerfile`, `.cursor/install.sh`, or dependency versions, rebuild the environment so Cursor refreshes the cached setup.
- If the standalone `start` hook is skipped by Cursor, the `bench` terminal still runs `.cursor/start.sh` before `bench start`.
- If setup fails during `bench init` or `bench build`, check the environment build logs first, then the `bench` terminal output.

## Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/ask_alyf
pre-commit install
```

Pre-commit is configured to use:

- ruff
- eslint
- prettier
- pyupgrade

## CI

This app can use GitHub Actions for CI. The configured workflows are:

- CI installs this app and runs unit tests on every push to the `version-16` branch.
- Linters run [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.

## Privacy

Ask ALYF is self-hosted. The published source code does not send usage data, analytics, or chat contents to the maintainers of this project.

The app calls the LLM and related providers you configure. Text, context, and uploaded documents may be processed by those providers under their privacy policies and terms. See [PRIVACY.md](PRIVACY.md).

## License

This project is licensed under the [GNU Affero General Public License v3.0](license.txt) (AGPL-3.0).

If you want to use Ask ALYF under different terms, for example to whitelabel or rebrand it without the AGPL's copyleft requirements, commercial licenses are available. Contact [hallo@alyf.de](mailto:hallo@alyf.de) for details.

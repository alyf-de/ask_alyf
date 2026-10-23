from __future__ import annotations

"""Field-level LLM context registry for Ask ALYF Field Agent Trigger.

Each entry in FIELD_CONTEXTS is keyed by (DocType, fieldname) and contains:
- system_prompt: str  -- specialist system prompt for the field
- jinja_globals: list[str]  -- names available via get_jenv().globals (e.g. "frappe")
- jinja_filters: list[str] | None  -- Jinja pipe filters (e.g. "json", "len")
- render_context_vars: list[str] | None  -- variables injected at render time by the
  specific renderer (NOT in jenv.globals; do NOT assert them against get_jenv().globals)
- safe_exec_env: list[str] | None  -- top-level keys in get_safe_globals() for Python scripts

For fields whose language/environment depends on a sibling field (e.g. Print Format.html
varies by `print_format_type`, System Console.console varies by `type`), use
FIELD_CONTEXT_VARIANTS instead. Each variant entry declares a `discriminator_field`
on the parent doc, a `default_variant` for fallback, and a `variants` map from
discriminator value to a regular field-context dict.
"""

FIELD_CONTEXTS: dict[tuple[str, str], dict] = {
	("Server Script", "script"): {
		"system_prompt": (
			"You are writing Python for a Frappe Server Script executed via `safe_exec`,"
			" which compiles your code with RestrictedPython before running it.\n\n"
			"Available globals:\n"
			"- `frappe` (with `.get_doc`, `.get_list`, `.get_all`, `.db`, `.qb`,"
			" `.msgprint`, `.throw`, `.sendmail`, `.get_print`, `.render_template`, `.utils`)\n"
			"- `doc` (the trigger document if DocType Event)\n"
			"- `json`\n"
			"- `dict`\n"
			"- `args` (method arguments if API-type script)\n\n"
			"RestrictedPython constraints — code will fail to compile or execute if you use:\n"
			"- `import` statements (no module imports — only the whitelisted namespace)\n"
			"- Names or attributes starting with `_` (e.g. `obj._private`, `cls.__dict__`,"
			" `func.__globals__`) — both reads and dict-key access are blocked\n"
			"- `str.format()` or `str.format_map()` — use f-strings or `%` formatting instead\n"
			"- Frame, code, traceback, generator, or coroutine introspection"
			" (`f_globals`, `f_locals`, `gi_frame`, `cr_frame`, `tb_frame`, etc.)\n"
			"- Reassigning modules, classes, functions, or builtins\n"
			"- Direct module attribute access on whitelisted modules beyond what is exposed\n\n"
			"`print(...)` works but its output is captured to `frappe.debug_log`"
			" via a print collector — use `frappe.log(...)` for explicit logging.\n"
			"Available builtins are limited (e.g. `abs`, `all`, `any`, `bool`, `dict`,"
			" `enumerate`, `isinstance`, `issubclass`, `list`, `max`, `min`, `range`,"
			" `set`, `sorted`, `sum`, `tuple`); `open`, `eval`, `exec`, `compile`,"
			" `__import__`, `globals`, `locals`, `vars` are NOT available.\n"
			"Return only the raw Python code — no markdown fences, no explanatory prose."
		),
		"jinja_globals": [],
		"jinja_filters": None,
		"render_context_vars": ["doc", "args"],
		"safe_exec_env": ["frappe", "json", "dict"],
	},
	("Client Script", "script"): {
		"system_prompt": (
			"You are writing JavaScript for a Frappe Client Script.\n\n"
			"Available APIs:\n"
			"- `frappe.call`, `frappe.xcall`\n"
			"- `frappe.db.get_value`, `frappe.db.get_list`\n"
			"- `cur_frm` (the active form object)\n"
			"- `frappe.model`, `frappe.ui`\n"
			"- `frappe.show_alert`, `frappe.msgprint`\n\n"
			"Use `frm` parameter from the handler signature, for example:\n"
			"  frappe.ui.form.on('DocType', { refresh(frm) { ... } })\n\n"
			"Return only the raw JavaScript — no markdown fences, no explanatory prose."
		),
		"jinja_globals": [],
		"jinja_filters": None,
		"render_context_vars": None,
		"safe_exec_env": None,
	},
	("Notification", "message"): {
		"system_prompt": (
			"You are writing a Jinja2 template for a Frappe email Notification message.\n\n"
			"Available globals (accessed as `{{ name }}`):\n"
			"- `frappe` (namespace with `frappe.utils.fmt_money`, `frappe.utils.formatdate`,"
			" `frappe.format_value`, `frappe.utils.get_url`)\n\n"
			"Notification-context variables (injected by the Notification engine at send time):\n"
			"- `doc` (the triggering document)\n\n"
			"Available Jinja pipe filters (used as `{{ value | filter }}`):\n"
			"`json`, `len`, `int`, `str`, `flt`\n\n"
			"Output HTML suitable for email rendering.\n"
			"Return only the raw Jinja2 HTML — no explanatory prose."
		),
		"jinja_globals": ["frappe"],
		"jinja_filters": ["json", "len", "int", "str", "flt"],
		"render_context_vars": ["doc"],
		"safe_exec_env": None,
	},
	("Web Page", "main_section"): {
		"system_prompt": (
			"You are writing Jinja2 HTML/Markdown content for a Frappe Web Page `main_section`.\n\n"
			"Available globals (accessed as `{{ name }}`):\n"
			"- `frappe`\n\n"
			"Web Page context variables (injected at render time):\n"
			"- `doc` (the Web Page document)\n\n"
			"Available Jinja pipe filters (used as `{{ value | filter }}`):\n"
			"`json`, `len`, `int`, `str`, `flt`\n\n"
			"The content may use Markdown or HTML depending on the page's content_type field.\n"
			"Return only the raw content — no explanatory prose."
		),
		"jinja_globals": ["frappe"],
		"jinja_filters": ["json", "len", "int", "str", "flt"],
		"render_context_vars": ["doc"],
		"safe_exec_env": None,
	},
	("Email Template", "response_html"): {
		"system_prompt": (
			"You are writing Jinja2 HTML for a Frappe Email Template body.\n\n"
			"Available globals (accessed as `{{ name }}`):\n"
			"- `frappe` (namespace with `frappe.utils.fmt_money`, `frappe.utils.formatdate`)\n\n"
			"Email Template context variables (injected when the template is rendered):\n"
			"- `doc` (the document being emailed about)\n\n"
			"Available Jinja pipe filters (used as `{{ value | filter }}`):\n"
			"`json`, `len`, `int`, `str`, `flt`\n\n"
			"Output must be valid HTML for email clients (inline styles preferred, no JavaScript).\n"
			"Return only the raw Jinja2 HTML — no explanatory prose."
		),
		"jinja_globals": ["frappe"],
		"jinja_filters": ["json", "len", "int", "str", "flt"],
		"render_context_vars": ["doc"],
		"safe_exec_env": None,
	},
	("Address Template", "template"): {
		"system_prompt": (
			"You are writing a Jinja2 template for a Frappe Address Template that renders"
			" a postal address.\n\n"
			"At render time, the Address document's fields are spread directly into the Jinja"
			" context — access them by name without a `doc.` prefix:\n"
			"- `address_line1`, `address_line2`, `city`, `state`, `pincode`, `country`\n"
			"- `phone`, `fax`, `email_id`\n"
			"- any custom fields on the Address DocType\n\n"
			"Translation helper:\n"
			"- `_('text')` — wraps user-facing labels for translation\n\n"
			"Use `{% if field %}...{% endif %}` to omit empty lines (most fields are optional).\n"
			"Output is rendered inline (typically with `<br>` line breaks) — keep it compact.\n"
			"Return only the raw Jinja2 HTML — no explanatory prose."
		),
		"jinja_globals": [],
		"jinja_filters": ["json", "len", "int", "str", "flt"],
		"render_context_vars": [
			"address_line1",
			"address_line2",
			"city",
			"state",
			"pincode",
			"country",
			"phone",
			"fax",
			"email_id",
		],
		"safe_exec_env": None,
	},
	("Notification", "condition"): {
		"system_prompt": (
			"You are writing a Python expression for a Frappe Notification condition,"
			" evaluated via `frappe.safe_eval` (RestrictedPython compiled in `eval` mode"
			" with `__builtins__` wiped).\n\n"
			"This is a SINGLE EXPRESSION (not a statement or block) that must return a"
			" truthy/falsy value — the notification fires when the expression is truthy.\n\n"
			"Available names in the eval context:\n"
			"- `doc` — the triggering document (access fields as `doc.fieldname`,"
			" `doc.get('fieldname')`, etc.)\n"
			"- `nowdate` — callable returning today's date as a string\n"
			"- `frappe` — limited namespace (utilities only, no DB writes)\n"
			"- Numeric helpers: `int`, `float`, `round` (no other builtins are exposed)\n\n"
			"Examples:\n"
			"- `doc.status == 'Open'`\n"
			"- `doc.grand_total > 1000 and doc.customer_group == 'Commercial'`\n"
			"- `doc.due_date and doc.due_date < nowdate()`\n\n"
			"RestrictedPython / safe_eval constraints — the expression will fail otherwise:\n"
			"- Single expression only — no `import`, assignments (`=`), `def`, `for`,"
			" `if/else` statements, or any block syntax\n"
			"- No walrus operator (`:=`)\n"
			"- No names or attributes starting with `_` (e.g. `doc._meta`, `obj.__class__`,"
			" `frappe.__dict__`) — both attribute reads and `obj['_key']` lookups are blocked\n"
			"- No `.format()` / `.format_map()` calls on strings — use f-strings if you must\n"
			"- No frame/code/traceback introspection (`f_globals`, `gi_frame`, `tb_frame`, etc.)\n"
			"- No call to `len()`, `str()`, `bool()`, `print()`, `open()`, `eval()`, etc."
			" — `__builtins__` is empty inside `safe_eval`\n\n"
			"Return only the raw Python expression on a single line — no markdown fences,"
			" no explanatory prose."
		),
		"jinja_globals": [],
		"jinja_filters": None,
		"render_context_vars": ["doc", "nowdate", "frappe"],
		"safe_exec_env": None,
	},
	# Process mining: the reader already knows the ERPNext happy path, so the prompt
	# spends its tool budget on how THIS site resolves the ambiguous cases. Runs in a
	# synchronous web request (see field_agent.run_field_agent), hence the hard caps.
	("Ask ALYF Skill", "description"): {
		"system_prompt": (
			"You are writing the description of an Ask ALYF Skill — a durable procedure that"
			" another Ask ALYF agent loads with `read_skill` and then follows step by step to"
			" complete the task named in the skill's title.\n\n"
			"Derive the procedure from this site's own historic records, not from generic"
			" ERPNext knowledge. The reader already knows the standard happy path. What it"
			" cannot know is how THIS organisation resolves the ambiguous cases. Mine those.\n\n"
			"EXISTING CONTENT\n"
			"If the current field value is not empty, treat it as a human-written procedure and"
			" keep it as the spine. Verify each bullet against the data, attach the counts that"
			" support it, and state plainly where the data contradicts it. Add the rules it does"
			" not cover. Never drop a human rule because you found no data for it. Keep it and"
			" mark it unverified.\n\n"
			"METHOD\n"
			"1. Read the skill title to identify the target DocType ('How to create a Purchase"
			" Invoice' -> Purchase Invoice). Use `get_meta` for its fields, mandatory flags and"
			" the link fields that pull in defaults.\n"
			"2. Sample the most recent submitted records (docstatus=1), newest first. Recent"
			" records reflect current practice; a full-history scan averages in workflows the"
			" company abandoned years ago.\n"
			"3. For distributions use `run_read_only_sql` with GROUP BY / COUNT(*) / ORDER BY"
			" count DESC / LIMIT. Never pull rows and tally them yourself.\n\n"
			"WHAT TO MINE — the ambiguous cases, not the happy path\n"
			"- Overridden defaults. Compare stored values against what the DocType default or"
			" the linked master (Item Default, Item Group, Supplier) would have produced. A"
			" field that always matches its default needs no bullet — it is already automatic."
			" The divergences are the house rules.\n"
			"- Corrections. `tabVersion` records every field change: filter on `ref_doctype`"
			" plus a recent `creation` bound and match the fieldname inside the `data` column."
			" A flag flipped after the fact (`is_purchase_item` on an Item that already"
			" existed for selling) names exactly the ambiguity the skill must pre-empt.\n"
			"- Masters created on demand. Compare a master's `creation` against the `creation`"
			" of the first transaction referencing it. Clustered within minutes means masters"
			" are created just-in-time — then mine the naming convention, group and UOM those"
			" masters use. Not clustered, plus a few codes carrying a large share of lines,"
			" means there is a catch-all master: name its exact code.\n"
			"- Rate overrides. A stored rate diverging from `price_list_rate` with"
			" `discount_percentage` or `margin_rate_or_amount` set means the practice is to"
			" override on the transaction. An `Item Price` row modified shortly after a"
			" transaction is submitted means the practice is to maintain the master instead.\n"
			"- Ad-hoc versus templated configuration. Tax or charge child rows present while"
			" the template link field is empty means people hand-add rows. A steady stream of"
			" newly created templates means they add templates instead.\n\n"
			"WHAT YOUR TOOLS CANNOT REACH\n"
			"Your tools read finished records, not the source documents behind them. You"
			" cannot open the attachment on a record, so merged lines, a value read off a"
			" scan, and any judgement about which source lines deserve their own row stay"
			" invisible to you. Do not assert a rule about how a source document maps to a"
			" record, and do not contradict one. If the current field value states such a"
			" rule, keep it as written.\n\n"
			"OUTPUT\n"
			"- Markdown bullets, imperative mood. No introduction, no summary, no prose"
			" paragraphs, no restating what the DocType is.\n"
			"- Order the bullets as the work actually happens: from the input the user starts"
			" with (a receipt file, an email, a request) through to the submitted record.\n"
			"- Every mined claim carries its counts, e.g. `expense_account for item_group"
			' "Software": 6800 (47/52 records)`. Counts tell the reader how far to trust the'
			" rule.\n"
			"- When the data is split with no discriminator, say so and instruct the reader to"
			" ask the user, e.g. `Cost center: 4/7 Administration, 3/7 Sales — no rule found,"
			" ask the user.` Never flatten a split into a confident instruction.\n"
			"- Name exact values: DocTypes, fieldnames, account numbers, template names, item"
			" codes. A bullet the reader cannot act on without guessing is worthless.\n"
			"- For an open-ended set, write a lookup instruction instead of a list. `Take the"
			" expense_account from this Item's previous invoice lines` stays correct as the"
			" data grows. A table of today's busiest item codes is stale next month and says"
			" nothing about a supplier nobody has invoiced yet. Enumerate only a closed set"
			" that is short and stable, such as the tax templates.\n"
			"- The strongest lookup instruction points at a worked example rather than at a"
			" field. `Open the most recent Purchase Invoice from this supplier, read its"
			" attached receipt, and mirror how that receipt was mapped to lines, items and"
			" accounts` hands the reader one exact precedent for the supplier in front of it."
			" It also covers the source-document mapping you cannot see yourself. Write the"
			" mined defaults alongside it, as the fallback for a supplier with no history.\n"
			"- Do not turn an entity into a rule below five supporting records. Report a thin"
			" pattern as a count and leave it at that. Two invoices from one supplier are not"
			" a policy.\n"
			"- Cover what to do when a referenced master is missing, when a value on the source"
			" document has no match in the system, and when a mandatory field has no default."
			" These are the cases the reader will actually get stuck on.\n\n"
			"BUDGET — you run inside a synchronous web request. Stay under roughly 15 tool"
			" calls: prefer one GROUP BY query over many single-record reads, request only the"
			" fields you need, and bound every `tabVersion` query by `ref_doctype` and date."
			" Answer with fewer, wider queries rather than exhausting the data.\n\n"
			"If a query returns nothing, state that the data does not support a rule rather"
			" than inventing one from ERPNext convention.\n\n"
			"Return only the markdown bullets — no fences, no explanatory prose."
		),
		"jinja_globals": [],
		"jinja_filters": None,
		"render_context_vars": None,
		"safe_exec_env": None,
	},
}


FIELD_CONTEXT_VARIANTS: dict[tuple[str, str], dict] = {
	# Print Format / html applies to custom_format=1 only; visual-builder-managed
	# formats (custom_format=0) are out of scope (the wand should not edit them).
	# We branch on (print_format_type, print_format_for) — the JS variant means
	# very different things for Reports (client-side microtemplate) vs DocTypes
	# (server-side Jinja over builder-generated HTML — uncommon when custom_format=1).
	("Print Format", "html"): {
		"discriminator_field": ("print_format_type", "print_format_for"),
		"default_variant": ("Jinja", "DocType"),
		"variants": {
			("Jinja", "DocType"): {
				"system_prompt": (
					"You are writing Jinja2 HTML for a custom Frappe Print Format"
					" (print_format_type = Jinja, print_format_for = DocType,"
					" custom_format = 1).\n\n"
					"Available globals (accessed as `{{ name }}`):\n"
					"- `frappe` (namespace with `frappe.utils.fmt_money`, `frappe.utils.formatdate`,"
					" `frappe.format_value`, `frappe.utils.get_url`, `frappe.utils.nowdate`,"
					" `frappe.utils.nowtime`)\n\n"
					"Print-context variables (injected by printview at render time, not in global Jinja env):\n"
					"- `doc` (the document being printed)\n"
					"- `doc.meta`\n"
					"- `letter_head`, `no_letterhead`, `print_settings`, `meta`, `layout`\n\n"
					"Available Jinja pipe filters (used as `{{ value | filter }}`):\n"
					"`json`, `len`, `int`, `str`, `flt`\n\n"
					"The output must be valid Jinja2 HTML that renders in wkhtmltopdf.\n"
					"Return only the raw Jinja2 HTML — no explanatory prose."
				),
				"jinja_globals": ["frappe"],
				"jinja_filters": ["json", "len", "int", "str", "flt"],
				"render_context_vars": [
					"doc",
					"doc.meta",
					"letter_head",
					"no_letterhead",
					"print_settings",
					"meta",
					"layout",
				],
				"safe_exec_env": None,
			},
			("Jinja", "Report"): {
				"system_prompt": (
					"You are writing Jinja2 HTML for a custom Frappe Report Print Format"
					" (print_format_type = Jinja, print_format_for = Report).\n\n"
					"Server-side Jinja globals (accessed as `{{ name }}`):\n"
					"- `frappe` (namespace with `frappe.utils.fmt_money`, `frappe.utils.formatdate`,"
					" `frappe.format_value`, `frappe.utils.get_url`)\n\n"
					"Report-context variables (injected at render time):\n"
					"- `data` (list of result rows; each row is a dict keyed by column fieldname)\n"
					"- `columns` (list of column definitions)\n"
					"- `filters` (dict of applied filter values)\n"
					"- `report` (the Report document)\n\n"
					"Available Jinja pipe filters (used as `{{ value | filter }}`):\n"
					"`json`, `len`, `int`, `str`, `flt`\n\n"
					"Iterate `data` to render rows; format numeric columns with"
					" `frappe.utils.fmt_money` or `frappe.format_value`.\n"
					"Return only the raw Jinja2 HTML — no explanatory prose."
				),
				"jinja_globals": ["frappe"],
				"jinja_filters": ["json", "len", "int", "str", "flt"],
				"render_context_vars": ["data", "columns", "filters", "report"],
				"safe_exec_env": None,
			},
			("JS", "Report"): {
				"system_prompt": (
					"You are writing a client-side microtemplate for a custom Frappe Report"
					" Print Format (print_format_type = JS, print_format_for = Report).\n\n"
					"This is rendered in the browser by `frappe.render_template` from"
					" `microtemplate.js` — NOT by server-side Jinja. The two engines look"
					" similar but are not the same.\n\n"
					"Microtemplate syntax:\n"
					"- Output an expression: `{{ expr }}` or `{%= expr %}`\n"
					"- Conditionals: `{% if cond %}...{% else %}...{% endif %}`\n"
					"  (also `{% if not cond %}...{% endif %}`)\n"
					"- Loops: `{% for item in list %}...{% endfor %}`\n"
					"  (each iteration exposes `item._index`)\n"
					"- Inline JavaScript: anything inside `{% ... %}` runs as JS\n\n"
					"Available data in the template scope (provided by query_report.js when"
					" rendering the print view):\n"
					"- `title` — translated report name\n"
					"- `subtitle` — applied filters HTML (or null)\n"
					"- `filters` — dict of applied filter values\n"
					"- `data` — array of row objects (keyed by column fieldname)\n"
					"- `original_data` — unsorted/raw rows\n"
					"- `columns` — array of column definitions (each has `fieldname`, `label`,"
					" `fieldtype`, `width`, etc.)\n"
					"- `report` — the QueryReport JS instance\n"
					"- `print_settings` — print_settings object with `.orientation`,"
					" `.with_letterhead`, etc.\n\n"
					"Helpers available globally in the browser:\n"
					"- `__('text')` — translation function\n"
					"- `frappe.format(value, df)` — format a column value per its fieldtype\n\n"
					"Caveats:\n"
					"- Single quotes (`'`) inside the template can break compilation — prefer"
					" double quotes for HTML attributes.\n"
					"- Do NOT use Jinja-only filters (`| json`, `| flt`, etc.) — those are"
					" not supported.\n\n"
					"Return only the raw microtemplate HTML — no markdown fences,"
					" no explanatory prose."
				),
				"jinja_globals": [],
				"jinja_filters": None,
				"render_context_vars": [
					"title",
					"subtitle",
					"filters",
					"data",
					"original_data",
					"columns",
					"report",
					"print_settings",
				],
				"safe_exec_env": None,
			},
		},
	},
	("System Console", "console"): {
		"discriminator_field": "type",
		"default_variant": "Python",
		"variants": {
			"Python": {
				"system_prompt": (
					"You are writing Python for the Frappe System Console (type = Python),"
					" executed via `safe_exec` (RestrictedPython). Restricted to System Manager"
					" / Administrator users.\n\n"
					"Available globals (top-level keys of the safe_exec environment):\n"
					"- `frappe` (with `.get_doc`, `.get_list`, `.get_all`, `.db` (read/write),"
					" `.qb`, `.msgprint`, `.utils`, `.as_json`, `.log`)\n"
					"- `json`\n"
					"- `dict`\n\n"
					"Output is captured from `frappe.debug_log` — use `frappe.log(...)` or"
					" `print(...)` (which routes to debug_log under safe_exec) to surface results.\n\n"
					"RestrictedPython constraints — code will fail to compile or execute if you use:\n"
					"- `import` statements (no module imports — only the whitelisted namespace)\n"
					"- Names or attributes starting with `_` (e.g. `obj._private`, `cls.__dict__`,"
					" `func.__globals__`) — both reads and dict-key access are blocked\n"
					"- `str.format()` or `str.format_map()` — use f-strings or `%` formatting instead\n"
					"- Frame, code, traceback, generator, or coroutine introspection"
					" (`f_globals`, `f_locals`, `gi_frame`, `cr_frame`, `tb_frame`, etc.)\n"
					"- Reassigning modules, classes, functions, or builtins\n"
					"- Builtins like `open`, `eval`, `exec`, `compile`, `__import__`,"
					" `globals`, `locals`, `vars` are NOT exposed; available builtins include"
					" `abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `isinstance`,"
					" `issubclass`, `list`, `max`, `min`, `range`, `set`, `sorted`, `sum`, `tuple`\n\n"
					"Notes:\n"
					"- `frappe.db.sql(...)` is wrapped to allow only `SELECT`/`EXPLAIN` queries.\n"
					"- Writes are committed only if the user enables the `commit` checkbox; the"
					" script otherwise rolls back. Prefer read-only logic when investigating data.\n\n"
					"Return only the raw Python code — no markdown fences, no explanatory prose."
				),
				"jinja_globals": [],
				"jinja_filters": None,
				"render_context_vars": None,
				"safe_exec_env": ["frappe", "json", "dict"],
			},
			"SQL": {
				"system_prompt": (
					"You are writing SQL for the Frappe System Console (type = SQL),"
					" executed via `frappe.utils.safe_exec.read_sql` inside a read-only"
					" transaction. Restricted to System Manager / Administrator users.\n\n"
					"Constraints:\n"
					"- The console enforces a READ-ONLY transaction (`frappe.db.begin(read_only=True)`)"
					" before running the query, so `INSERT`/`UPDATE`/`DELETE`/`DDL` will fail.\n"
					"- Write only `SELECT` statements (CTEs / subqueries / `UNION` are fine).\n"
					"- Results are returned as a JSON list of dicts via `frappe.as_json`.\n\n"
					"Dialect notes:\n"
					"- The underlying database may be MariaDB, PostgreSQL, or SQLite — prefer"
					" portable ANSI SQL when possible.\n"
					"- Frappe DocType tables are named with a `tab` prefix and the DocType name in"
					" backticks (MariaDB) or double quotes (Postgres), e.g. `` `tabSales Invoice` ``"
					' or `"tabSales Invoice"`.\n\n'
					"Return only the raw SQL query — no markdown fences, no explanatory prose."
				),
				"jinja_globals": [],
				"jinja_filters": None,
				"render_context_vars": None,
				"safe_exec_env": None,
			},
		},
	},
}

GENERIC_SYSTEM_PROMPT_TEMPLATE = (
	"You are assisting a user who is editing the `{fieldname}` field"
	" on a `{doctype}` document in Frappe/ERPNext.\n\n"
	"Field label: {label}\n"
	"Field type: {fieldtype}\n\n"
	"The current document context is provided below as JSON.\n"
	"Use it to generate a relevant, accurate value for this field.\n\n"
	"If the field type is Code, return only raw code without markdown fences.\n"
	"Otherwise return plain text or HTML as appropriate for the field type.\n"
	"Do not include explanatory prose in your response — return only the field value."
)


def resolve_variant(variant_entry: dict, doc: dict | None) -> dict:
	"""Pick the field context for a variant entry based on the doc's discriminator field(s).

	`discriminator_field` may be a single field name or a tuple of field names — in the
	tuple case the lookup key is a tuple of the doc's values for those fields.

	Falls back to the entry's `default_variant` when the doc is missing, any
	discriminator field is absent/empty, or the resulting key is not in `variants`.
	"""
	variants = variant_entry["variants"]
	default_variant = variant_entry["default_variant"]
	discriminator_field = variant_entry["discriminator_field"]
	doc_dict = doc or {}

	if isinstance(discriminator_field, tuple):
		key = tuple(doc_dict.get(f) for f in discriminator_field)
	else:
		key = doc_dict.get(discriminator_field) or default_variant

	return variants.get(key) or variants[default_variant]


def get_field_context(
	doctype: str,
	fieldname: str,
	fieldtype: str,
	doc: dict | None = None,
) -> dict:
	"""Return the registered field context for (doctype, fieldname) or a generic fallback.

	Args:
		doctype: The DocType name.
		fieldname: The field name.
		fieldtype: The Frappe fieldtype string (e.g. "Code", "Text Editor").
		doc: Optional document dict. Used to pick the right variant when
			(doctype, fieldname) is in FIELD_CONTEXT_VARIANTS (e.g. Print Format.html
			branches on `print_format_type`, System Console.console branches on `type`).

	Returns:
		A field context dict with system_prompt, jinja_globals, jinja_filters,
		render_context_vars, and safe_exec_env.
	"""
	variant_entry = FIELD_CONTEXT_VARIANTS.get((doctype, fieldname))
	if variant_entry is not None:
		return resolve_variant(variant_entry, doc)

	entry = FIELD_CONTEXTS.get((doctype, fieldname))
	if entry is not None:
		return entry

	try:
		import frappe as _frappe

		meta = _frappe.get_meta(doctype)
		df = meta.get_field(fieldname)
		label = (df.label or fieldname) if df else fieldname
	except Exception:
		label = fieldname

	return build_generic_fallback(doctype, fieldname, fieldtype, label)


def build_generic_fallback(doctype: str, fieldname: str, fieldtype: str, label: str) -> dict:
	"""Build a generic field context for unmapped (doctype, fieldname) combinations.

	Args:
		doctype: The DocType name.
		fieldname: The field name.
		fieldtype: The Frappe fieldtype string.
		label: The human-readable field label.

	Returns:
		A generic field context dict.
	"""
	system_prompt = GENERIC_SYSTEM_PROMPT_TEMPLATE.format(
		doctype=doctype,
		fieldname=fieldname,
		fieldtype=fieldtype,
		label=label,
	)
	return {
		"system_prompt": system_prompt,
		"jinja_globals": [],
		"jinja_filters": None,
		"render_context_vars": None,
		"safe_exec_env": None,
	}

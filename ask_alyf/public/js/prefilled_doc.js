// Opens a new unsaved form and fills it through the normal form triggers.
// Nothing is saved. Fields that cannot be set are returned in `omitted_fields`.
export class PrefilledDoc {
	constructor(payload) {
		this.payload = payload;
		this.frm = null;
		this.omittedFields = [];
		this.skippedFields = new Set([
			"doctype",
			...frappe.model.std_fields_list,
			...frappe.model.child_table_field_list,
		]);
	}

	// `getMatchingForm` returns the open form for `{ doctype, docname }`, or throws.
	async open(getMatchingForm) {
		await frappe.model.with_doctype(this.payload.doctype);
		const doc = frappe.model.get_new_doc(this.payload.doctype);

		const originalReroute = frappe.re_route_on_msgprint_hide;
		frappe.re_route_on_msgprint_hide = () => {};
		try {
			await frappe.set_route("Form", doc.doctype, doc.name);
			await waitForRequests();
			this.frm = getMatchingForm({ doctype: doc.doctype, docname: doc.name });
			await this.fillDocument(doc, this.payload.doc);
		} finally {
			frappe.re_route_on_msgprint_hide = originalReroute;
			this.frm?.refresh_fields();
		}

		const result = { doctype: doc.doctype, docname: doc.name };
		if (this.omittedFields.length) result.omitted_fields = this.omittedFields;
		return result;
	}

	async fillDocument(target, source, prefix = "") {
		const tables = new Map(tableFieldsOf(target.doctype).map((df) => [df.fieldname, df]));
		for (const df of frappe.get_meta(target.doctype)?.fields || []) {
			if (tables.has(df.fieldname) || this.skippedFields.has(df.fieldname)) continue;
			const value = source?.[df.fieldname];
			if (!hasValue(value)) continue;
			const isSet = await this.setField(target, source, df, value);
			if (!isSet) this.omittedFields.push(`${prefix}${__(df.label || df.fieldname)}`);
		}
		for (const [fieldname, tableDf] of tables) {
			const rows = source?.[fieldname];
			if (!Array.isArray(rows)) continue;
			frappe.model.clear_table(target, fieldname);
			for (const row of rows) {
				await this.addRow(target, tableDf, row);
			}
		}
	}

	async addRow(target, tableDf, row) {
		if (!row || typeof row !== "object" || Array.isArray(row)) return;
		const child = frappe.model.add_child(target, tableDf.options, tableDf.fieldname);
		// `frappe.model.add_child` does not run `<table>_add`, but the grid does.
		await this.frm.script_manager.trigger(`${tableDf.fieldname}_add`, child.doctype, child.name);
		await waitForRequests();
		await this.fillDocument(
			child,
			row,
			`${__(tableDf.label || tableDf.fieldname)} #${child.idx}: `,
		);
	}

	async setField(target, source, df, value) {
		if (["Link", "Dynamic Link"].includes(df.fieldtype)) {
			const fetchMap = fetchMapFor(target, source, df.fieldname);
			const linkedDoctype =
				df.fieldtype === "Dynamic Link" ? source?.[df.options] || target[df.options] : df.options;
			const response = await validateLink(linkedDoctype, value, Object.values(fetchMap));
			// keep the default when the link is not valid
			if (!response) return false;
			value = response.name;
			await setFetchedValues(target, fetchMap, response);
		}

		try {
			await frappe.model.set_value(target.doctype, target.name, df.fieldname, value, df.fieldtype);
		} catch {
			// a failing form trigger must not stop the fill
		} finally {
			await waitForRequests();
		}
		return hasValue(target[df.fieldname]);
	}
}

// Fields that `fetch_from` this link. Values from the payload win over fetched values.
function fetchMapFor(target, source, linkFieldname) {
	const fetchMap = {};
	for (const df of frappe.get_meta(target.doctype)?.fields || []) {
		if (
			!df.fetch_from?.startsWith(`${linkFieldname}.`) ||
			(df.fetch_if_empty && target[df.fieldname]) ||
			hasValue(source?.[df.fieldname])
		)
			continue;
		const [, sourceField] = df.fetch_from.split(".");
		if (sourceField) fetchMap[df.fieldname] = sourceField;
	}
	return fetchMap;
}

async function setFetchedValues(target, fetchMap, response) {
	for (const [targetField, sourceField] of Object.entries(fetchMap)) {
		const next = hasValue(response[sourceField]) ? response[sourceField] : "";
		if (target[targetField] === next) continue;
		try {
			await frappe.model.set_value(target.doctype, target.name, targetField, next);
		} catch {
			// a failing form trigger must not stop the fill
		}
	}
}

async function validateLink(doctype, name, fieldsToFetch) {
	if (!doctype || !hasValue(name)) return null;
	try {
		const response = await frappe.xcall("frappe.client.validate_link_and_fetch", {
			doctype,
			docname: name,
			fields_to_fetch: [...new Set(fieldsToFetch)],
		});
		return response?.name ? response : null;
	} catch {
		return null;
	}
}

async function waitForRequests() {
	for (let i = 0; i < 5 && frappe.request.ajax_count; i++) await frappe.after_ajax();
}

function tableFieldsOf(doctype) {
	return (frappe.get_meta(doctype)?.fields || []).filter(
		(df) => frappe.model.table_fields.includes(df.fieldtype) && df.fieldname && df.options,
	);
}

function hasValue(value) {
	return value !== undefined && value !== null && value !== "";
}

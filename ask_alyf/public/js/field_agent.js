(function () {
	// Gate on Ask ALYF User role + the allow_field_agent settings flag (delivered via boot payload).
	function isFieldAgentEnabled() {
		var boot = frappe.boot && frappe.boot.ask_alyf;
		return !!(boot && boot.allowed && boot.field_agent_enabled);
	}

	var AGENT_FIELDTYPES = new Set([
		"Long Text",
		"Small Text",
		"Text",
		"Text Editor",
		"HTML Editor",
		"Code",
		"Markdown Editor",
	]);

	function isMac() {
		return /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || "");
	}

	function prefersReducedMotion() {
		return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
	}

	// ---- Value extraction -------------------------------------------------------

	function getFieldValue(control) {
		// Ace editor (Code, Markdown Editor, HTML Editor)
		if (
			control.editor &&
			control.editor.session &&
			typeof control.editor.session.getValue === "function"
		) {
			return control.editor.session.getValue();
		}
		// Quill (Text Editor)
		if (control.quill && control.quill.root) {
			return control.quill.root.innerHTML;
		}
		// Textarea fallback (Text, Small Text, Long Text)
		if (control.$input) {
			return control.$input.val() || "";
		}
		return control.value || "";
	}

	// ---- Value setting ----------------------------------------------------------

	function setFieldValue(control, value) {
		// Frappe's set_value handles all editor types internally:
		// - Ace: via set_formatted_input (sets session.setValue)
		// - Quill: via its own setter
		// - Textarea: via jQuery
		// It also marks the form dirty and fires change handlers.
		if (typeof control.set_value === "function") {
			control.set_value(value);
		}
	}

	// ---- Overlay ----------------------------------------------------------------

	var activeOverlay = null;
	var activeControl = null;

	function closeOverlay() {
		if (activeOverlay) {
			activeOverlay.remove();
			activeOverlay = null;
		}
		activeControl = null;
		$(document).off("keydown.field_agent");
		$(document).off("mousedown.field_agent");
	}

	function createOverlay(control, triggerBtn) {
		closeOverlay();

		var overlay = $('<div class="field-agent-overlay"></div>');

		var promptArea = $(
			'<textarea class="field-agent-overlay-textarea" rows="3" placeholder="' +
				__("Describe what you want...") +
				'"></textarea>'
		);

		var footer = $('<div class="field-agent-overlay-footer"></div>');
		var hint = $(
			'<span class="field-agent-hint">' +
				"<kbd>" +
				(isMac() ? "\u2318" : "Ctrl") +
				"</kbd>" +
				"<kbd>\u23CE</kbd>" +
				"</span>"
		);
		var submitBtn = $(
			'<button class="btn btn-primary btn-sm field-agent-submit">' + __("Generate") + "</button>"
		);
		var statusText = $('<div class="field-agent-status hide"></div>');

		footer.append(hint).append(submitBtn);
		overlay.append(promptArea).append(statusText).append(footer);

		// Position overlay below the trigger button
		var btnRect = triggerBtn[0].getBoundingClientRect();
		overlay.css({
			top: btnRect.bottom + 4 + "px",
			left: btnRect.left + "px",
		});

		$("body").append(overlay);
		activeOverlay = overlay;
		activeControl = control;

		promptArea.focus();

		// ESC closes
		$(document).on("keydown.field_agent", function (e) {
			if (e.key === "Escape") {
				closeOverlay();
			}
		});

		// Click outside closes
		$(document).on("mousedown.field_agent", function (e) {
			if (
				activeOverlay &&
				!activeOverlay.is(e.target) &&
				activeOverlay.find(e.target).length === 0 &&
				!triggerBtn.is(e.target) &&
				triggerBtn.find(e.target).length === 0
			) {
				closeOverlay();
			}
		});

		promptArea.on("input", function () {
			promptArea.removeClass("is-invalid");
		});

		submitBtn.on("click", function () {
			var prompt = promptArea.val().trim();
			if (!prompt) {
				promptArea.removeClass("is-invalid");
				// Force reflow so the animation re-triggers if class was just removed.
				void promptArea[0].offsetWidth;
				promptArea.addClass("is-invalid");
				promptArea.focus();
				return;
			}
			runAgent(control, prompt, submitBtn, promptArea, statusText, triggerBtn);
		});

		// Submit on Ctrl+Enter / Cmd+Enter
		promptArea.on("keydown", function (e) {
			if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
				submitBtn.trigger("click");
			}
		});

		return overlay;
	}

	// ---- Main runner ------------------------------------------------------------

	function runAgent(control, prompt, submitBtn, promptArea, statusText, triggerBtn) {
		var frm = control.frm || window.cur_frm;
		if (!frm) {
			frappe.show_alert({ message: __("No active form found."), indicator: "red" });
			closeOverlay();
			return;
		}

		var doctype = frm.doctype;
		var fieldname = control.df.fieldname;
		var fieldtype = control.df.fieldtype;
		var currentValue = getFieldValue(control);
		var doc = frm.doc;

		// Disable UI while running
		submitBtn.prop("disabled", true);
		promptArea.prop("disabled", true);
		triggerBtn.prop("disabled", true);
		triggerBtn.addClass("is-active");
		statusText.text(__("Generating...")).removeClass("hide");

		// Disable field input
		if (control.editor && control.editor.setReadOnly) {
			control.editor.setReadOnly(true);
		} else if (control.$input) {
			control.$input.prop("disabled", true);
		}

		// Rotating status messages — subtle, informative, not playful.
		var stageHandles = [
			setTimeout(function () {
				if (activeOverlay) statusText.text(__("Drafting..."));
			}, 18000),
			setTimeout(function () {
				if (activeOverlay) statusText.text(__("Still generating..."));
			}, 45000),
			setTimeout(function () {
				if (activeOverlay) statusText.text(__("This is taking a moment..."));
			}, 90000),
		];

		frappe
			.xcall("ask_alyf.ask_alyf.api.field_agent_run", {
				doctype: doctype,
				fieldname: fieldname,
				fieldtype: fieldtype,
				current_value: currentValue,
				doc: doc,
				prompt: prompt,
			})
			.then(function (result) {
				var response = result && result.response;
				if (response) {
					setFieldValue(control, response);
					flashTargetField(control);
					offerUndo(control, currentValue);
				}
			})
			.catch(function (err) {
				var message =
					(err && (err.message || err.exc_type || err.exc)) || __("Could not generate content.");
				frappe.show_alert({ message: message, indicator: "red" }, 7);
			})
			.finally(function () {
				stageHandles.forEach(clearTimeout);
				if (control.editor && control.editor.setReadOnly) {
					control.editor.setReadOnly(false);
				} else if (control.$input) {
					control.$input.prop("disabled", false);
				}
				triggerBtn.prop("disabled", false);
				triggerBtn.removeClass("is-active");
				closeOverlay();
			});
	}

	// ---- Success polish ---------------------------------------------------------

	function flashTargetField(control) {
		if (prefersReducedMotion()) return;
		var $target = null;
		if (control.$wrapper) {
			$target = control.$wrapper.find(".ace_editor, .ql-container, textarea, input").first();
		}
		if (!$target || !$target.length) return;
		$target.removeClass("field-agent-target-flash");
		void $target[0].offsetWidth;
		$target.addClass("field-agent-target-flash");
		setTimeout(function () {
			$target.removeClass("field-agent-target-flash");
		}, 1300);
	}

	function offerUndo(control, previousValue) {
		var html =
			__("Field updated.") +
			' <button type="button" data-action="undo" class="btn btn-link btn-xs field-agent-undo">' +
			__("Undo") +
			"</button>";
		frappe.show_alert({ message: html, indicator: "green" }, 8, {
			undo: function (e) {
				if (e) {
					if (e.preventDefault) e.preventDefault();
					if (e.stopPropagation) e.stopPropagation();
				}
				setFieldValue(control, previousValue || "");
				frappe.show_alert({ message: __("Reverted."), indicator: "gray" }, 3);
			},
		});
	}

	// ---- Trigger button injection -----------------------------------------------

	function injectTriggerButton(control) {
		if (typeof control.get_status !== "function" || control.get_status() !== "Write") return;

		var $clearfix = control.$wrapper && control.$wrapper.find(".clearfix");
		if (!$clearfix || !$clearfix.length) return;

		// Avoid double-injection
		if ($clearfix.find(".field-agent-trigger").length) return;

		var triggerBtn = $(
			'<button type="button" class="btn btn-xs btn-icon field-agent-trigger" title="' +
				__("Generate with AI") +
				'"><svg class="icon icon-sm"><use href="#icon-sparkles"></use></svg></button>'
		);

		$clearfix.append(triggerBtn);

		triggerBtn.on("click", function (e) {
			e.stopPropagation();
			if (activeOverlay && activeControl === control) {
				closeOverlay();
				return;
			}
			createOverlay(control, triggerBtn);
		});
	}

	// ---- Monkey-patch ----------------------------------------------------------

	$(document).ready(function () {
		if (!isFieldAgentEnabled()) return;

		// frappe.ui.form.ControlText is defined in Frappe's core bundle which loads
		// before app_include_js files. It is available synchronously at document.ready.
		var ControlText = frappe.ui.form && frappe.ui.form.ControlText;
		if (!ControlText) return;

		var originalMake = ControlText.prototype.make;

		ControlText.prototype.make = function () {
			// Call original make
			originalMake.apply(this, arguments);

			// Guard: only target supported fieldtypes
			if (!this.df || !AGENT_FIELDTYPES.has(this.df.fieldtype)) return;

			// Guard: skip read-only fields
			if (this.df.read_only) return;

			// Guard: skip dialogs, grid cells, Web Forms — only desk forms with cur_frm
			if (!this.frm && !window.cur_frm) return;

			// For ControlCode and its subclasses (Ace editor), make_input is async
			// (loads the ace lib then calls make_ace_editor). We defer injection
			// until after the current call stack so Ace has a chance to initialize.
			var control = this;
			setTimeout(function () {
				injectTriggerButton(control);
			}, 0);
		};
	});
})();

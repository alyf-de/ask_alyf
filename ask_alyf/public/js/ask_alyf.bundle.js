import "./icons";
import "./field_agent";

(function () {
	if (window.ask_alyfWidget) {
		return;
	}

	const ASK_ALYF_AGGREGATION_CHART_TYPES = new Set(["pie", "donut", "percentage"]);
	// Must exceed frappe-charts getExtraHeight (~130) plus drawable area; see tools.MIN_FRAPPE_CHART_HEIGHT.
	const ASK_ALYF_FRAPPE_CHART_MIN_HEIGHT = 240;
	const ASK_ALYF_FRAPPE_CHART_MAX_HEIGHT = 720;
	const ASK_ALYF_JOB_POLL_INTERVAL_MS = 2000;
	const ASK_ALYF_MISSING_JOB_CHECKS = 3;

	/**
	 * Frappe Charts divides by labels.length and (for pie) grand total;
	 * tiny/negative plot height yields NaN axis lines in draw.js.
	 */
	function normalizeStoredFrappeChartOptions(out) {
		if (!out || typeof out !== "object") {
			return { ok: false };
		}
		const type = String(out.type || "bar");
		out.type = type;
		out.animate = false;
		out.disableEntryAnimation = true;

		if (!out.data || typeof out.data !== "object") {
			return { ok: false };
		}

		let labels = Array.isArray(out.data.labels)
			? out.data.labels.map((label) => (label === null || label === undefined ? "" : String(label)))
			: [];
		const maxLabels = 100;
		if (labels.length > maxLabels) {
			labels = labels.slice(0, maxLabels);
		}
		const nLabels = labels.length;
		if (!nLabels) {
			return { ok: false };
		}

		let datasets = Array.isArray(out.data.datasets) ? out.data.datasets : [];
		if (!datasets.length) {
			datasets = [{ name: "", values: new Array(nLabels).fill(0) }];
		}

		out.data.labels = labels;
		out.data.datasets = datasets.map((dataset) => {
			const row = { ...dataset };
			const vals = Array.isArray(dataset.values) ? dataset.values : [];
			const values = [];
			for (let i = 0; i < nLabels; i++) {
				const num = Number(vals[i]);
				values.push(Number.isFinite(num) ? num : 0);
			}
			row.values = values;
			return row;
		});

		if (ASK_ALYF_AGGREGATION_CHART_TYPES.has(type)) {
			const perSlice = labels.map((_, index) =>
				out.data.datasets.reduce((acc, dataset) => acc + Math.max(0, dataset.values[index] || 0), 0)
			);
			const grandTotal = perSlice.reduce((acc, value) => acc + value, 0);
			if (grandTotal <= 0) {
				return { ok: false };
			}
		} else if (type === "line") {
			const allY = out.data.datasets.flatMap((ds) => ds.values);
			const yMin = Math.min(...allY);
			const yMax = Math.max(...allY);
			if (allY.length && yMin === yMax) {
				// Line charts fit the axis to min/max. A flat series sits on the
				// x-axis because frappe-charts expands a zero range upward only.
				const pad = yMax === 0 ? 1 : Math.abs(yMax) * 0.1 || 0.1;
				out.axisOptions = {
					...out.axisOptions,
					yAxisRange: { min: yMax - pad, max: yMax + pad },
				};
			}
		}

		const height = Number(out.height);
		if (!Number.isFinite(height) || height <= 0) {
			delete out.height;
		} else if (height < ASK_ALYF_FRAPPE_CHART_MIN_HEIGHT) {
			out.height = ASK_ALYF_FRAPPE_CHART_MIN_HEIGHT;
		}

		return { ok: true, options: out };
	}

	/**
	 * frappe.Chart keeps window listeners, ResizeObserver, and a delayed `update` timer.
	 * If we drop the mount with innerHTML, those still fire and draw.js logs NaN for SVG attrs.
	 */
	function wrapAskAlyfFrappeChart(chart) {
		if (!chart || typeof chart.draw !== "function") {
			return chart;
		}
		const origDraw = chart.draw.bind(chart);
		const origUpdate = typeof chart.update === "function" ? chart.update.bind(chart) : null;
		chart.draw = function askAlyfGuardedDraw(...args) {
			if (chart._askAlyfDisposed) {
				return;
			}
			const parent = chart.parent;
			if (parent && !parent.isConnected) {
				return;
			}
			return origDraw(...args);
		};
		if (origUpdate) {
			chart.update = function askAlyfGuardedUpdate(data, drawing) {
				if (chart._askAlyfDisposed) {
					return;
				}
				const parent = chart.parent;
				if (parent && !parent.isConnected) {
					return;
				}
				return origUpdate(data, drawing);
			};
		}
		return chart;
	}

	class ask_alyfWidget {
		constructor() {
			this.state = {
				open: false,
				loading: false,
				conversation: null,
				conversations: [],
				activeTab: "chat",
				messages: [],
				pendingOperations: [],
				status: "",
				steps: [],
				mode: "Ask",
			};
			this.pendingStreamMessageId = null;
			this.renderedMessageKeys = new Set();
			this.handledFrontendCallIds = new Set();
			this.resizeState = null;
			this.voiceRecognition = null;
			this.boundResizeMove = (event) => this.resizePanel(event);
			this.boundResizeEnd = (event) => this.stopPanelResize(event);
			this.boundDocumentClick = (event) => this.onDocumentClick(event);
			this.deferredChartPaints = [];
			this.messageEntries = new Map();
			this.activeFrappeCharts = new Map();
			this.statusWrapperEl = null;
			this.statusBodyEl = null;
			this.liveStepsEl = null;
			this.pendingOperationsEl = null;
			this.suggestedPromptsEl = null;
			this.activeResponseJob = null;
			this.responseJobPollTimer = null;
			this.responseJobMonitorVersion = 0;
		}

		disposeActiveFrappeCharts(messageKey = null) {
			if (messageKey === null) {
				const keys = Array.from(this.activeFrappeCharts.keys());
				keys.forEach((key) => this.disposeActiveFrappeCharts(key));
				return;
			}

			const charts = this.activeFrappeCharts.get(messageKey) || [];
			this.activeFrappeCharts.delete(messageKey);
			for (const chart of charts) {
				try {
					chart._askAlyfDisposed = true;
					if (typeof chart.destroy === "function") {
						chart.destroy();
					}
				} catch {
					// Ignore teardown errors from third-party chart.
				}
			}
		}

		isChatAreaReady() {
			if (!this.panel || this.panel.classList.contains("ask_alyf-hidden")) {
				return false;
			}
			if (!this.chatViewEl || this.chatViewEl.classList.contains("ask_alyf-hidden")) {
				return false;
			}
			const width = this.messagesEl?.clientWidth ?? 0;
			return width >= 48;
		}

		flushDeferredChartPaints() {
			if (!this.deferredChartPaints?.length) {
				return;
			}
			if (!this.isChatAreaReady()) {
				return;
			}
			const batch = [...this.deferredChartPaints];
			this.deferredChartPaints = [];
			for (const paint of batch) {
				try {
					paint();
				} catch {
					// Ignore paint errors; individual mounts show their own fallback.
				}
			}
		}

		setTrackedFrappeChart(messageKey, index, chart) {
			const charts = this.activeFrappeCharts.get(messageKey) || [];
			charts[index] = chart;
			this.activeFrappeCharts.set(messageKey, charts);
		}

		getTrackedFrappeChart(messageKey, index) {
			return this.activeFrappeCharts.get(messageKey)?.[index] || null;
		}

		invalidateChartPaint(entry) {
			if (!entry) {
				return;
			}
			entry.chartPaintVersion = (entry.chartPaintVersion || 0) + 1;
		}

		resetMessageCharts(entry, messageKey) {
			this.invalidateChartPaint(entry);
			if (entry?.chartResizeObserver) {
				entry.chartResizeObserver.disconnect();
				entry.chartResizeObserver = null;
			}
			if (entry?.chartResizeFrame) {
				cancelAnimationFrame(entry.chartResizeFrame);
				entry.chartResizeFrame = 0;
			}
			this.disposeActiveFrappeCharts(messageKey);
			if (entry?.chartHolder) {
				entry.chartHolder.remove();
				entry.chartHolder = null;
			}
		}

		getMessageHtml(message) {
			if (message.role === "assistant") {
				return frappe.markdown(message.content || "");
			}
			if (message.role === "system" && Array.isArray(message.metadata?.files)) {
				const names = message.metadata.files
					.map((f) => this.renderFileLink(f))
					.filter(Boolean)
					.join(", ");
				return `<i class="fa fa-paperclip" aria-hidden="true"></i> ${names}`;
			}
			return this.escapeHtml(message.content || "").replace(/\n/g, "<br>");
		}

		getToolCallsFingerprint(message) {
			const toolCalls = message?.metadata?.tool_calls;
			if (message?.role !== "assistant" || !Array.isArray(toolCalls) || !toolCalls.length) {
				return "";
			}
			try {
				return JSON.stringify(toolCalls);
			} catch {
				return "";
			}
		}

		syncMessageToolCalls(entry, message) {
			const fingerprint = this.getToolCallsFingerprint(message);
			if (entry.toolCallsFingerprint === fingerprint) {
				return;
			}

			entry.toolCallsFingerprint = fingerprint;
			if (entry.toolCallsHolder) {
				entry.toolCallsHolder.remove();
				entry.toolCallsHolder = null;
			}
			if (!fingerprint) {
				return;
			}

			entry.toolCallsHolder = this.buildToolCallsElement(message.metadata.tool_calls);
			// Above the body: the agent did these before it answered.
			entry.wrapper.insertBefore(entry.toolCallsHolder, entry.body);
		}

		buildToolCallsElement(toolCalls, { open = false } = {}) {
			const holder = document.createElement("details");
			holder.className = "ask_alyf-tool-calls";
			holder.open = open;

			const summary = document.createElement("summary");
			summary.textContent = toolCalls.length === 1 ? __("1 step") : __("{0} steps", [toolCalls.length]);
			holder.appendChild(summary);

			const list = document.createElement("ol");
			list.className = "ask_alyf-tool-call-list";
			for (const call of toolCalls) {
				list.appendChild(this.buildToolCallItem(call));
			}
			holder.appendChild(list);
			return holder;
		}

		buildToolCallItem(call, { showArgs = true } = {}) {
			const item = document.createElement("li");
			if (call?.status === "failed") {
				item.classList.add("ask_alyf-tool-call-failed");
			}
			if (call?.status === "running") {
				item.classList.add("ask_alyf-tool-call-running");
			}

			const name = document.createElement("span");
			name.className = "ask_alyf-tool-call-name";
			// Older messages predate the server-side label and only carry the
			// raw tool name.
			name.textContent = call?.label || (call?.name || "").replace(/_/g, " ");
			item.appendChild(name);

			const args = showArgs ? this.formatToolCallArgs(call?.args) : "";
			if (args) {
				const detail = document.createElement("span");
				detail.className = "ask_alyf-tool-call-args";
				detail.textContent = args;
				item.appendChild(detail);
			}

			return item;
		}

		applyStepUpdate(step) {
			if (!step?.call_id) {
				return;
			}

			const index = this.state.steps.findIndex((entry) => entry.call_id === step.call_id);
			if (step.status === "dropped") {
				if (index !== -1) {
					this.state.steps.splice(index, 1);
				}
			} else if (index === -1) {
				this.state.steps.push({ ...step });
			} else {
				this.state.steps[index] = { ...this.state.steps[index], ...step };
			}

			this.renderLiveSteps();
			this.scrollToBottom();
		}

		adoptCompletedToolCalls(messageId, toolCalls) {
			if (!Array.isArray(toolCalls) || !toolCalls.length) {
				return;
			}
			const message = this.state.messages.find((item) => item.id === messageId);
			if (!message) {
				return;
			}
			// Stored steps are authoritative: they survive a reload, the live
			// ones do not.
			message.metadata = { ...(message.metadata || {}), tool_calls: toolCalls };
		}

		adoptRunningSteps(steps) {
			// Catches up a view that missed the broadcasts — after switching
			// conversations, or after a reload. Only a longer list is adopted,
			// so a poll that lags behind the live stream never rewinds it.
			if (!Array.isArray(steps) || steps.length <= this.state.steps.length) {
				return;
			}

			this.state.steps = steps.map((step) => ({ ...step }));
			this.renderLiveSteps();
			this.scrollToBottom();
		}

		clearLiveSteps() {
			this.state.steps = [];
			this.renderLiveSteps();
		}

		renderLiveSteps() {
			if (!this.messagesEl) {
				return;
			}

			if (!this.state.steps.length) {
				if (this.liveStepsEl) {
					this.liveStepsEl.remove();
					this.liveStepsEl = null;
				}
				return;
			}

			// Rebuilt rather than patched: the list is short and only changes
			// once per tool call.
			const wrapper = document.createElement("div");
			wrapper.className = "ask_alyf-message ask_alyf-assistant ask_alyf-live-steps";
			const list = document.createElement("ol");
			list.className = "ask_alyf-tool-call-list";
			for (const step of this.state.steps) {
				// Labels only while it runs; the arguments are there to read in
				// the message once the turn is done.
				list.appendChild(this.buildToolCallItem(step, { showArgs: false }));
			}
			wrapper.appendChild(list);

			if (this.liveStepsEl) {
				this.liveStepsEl.replaceWith(wrapper);
			} else {
				this.messagesEl.insertBefore(
					wrapper,
					this.statusWrapperEl || this.pendingOperationsEl || null
				);
			}
			this.liveStepsEl = wrapper;
		}

		formatToolCallArgs(args) {
			if (!args || typeof args !== "object") {
				return "";
			}
			return Object.entries(args)
				.map(([key, value]) => `${key}: ${value}`)
				.join(", ");
		}

		renderFileLink(fileEntry) {
			const label = (fileEntry?.file_name || fileEntry?.name || "").toString().trim();
			if (!label) {
				return "";
			}

			const href = this.getSafeFileHref(fileEntry?.file_url);
			if (!href) {
				return this.escapeHtml(label);
			}

			const link = document.createElement("a");
			link.href = href;
			link.target = "_blank";
			link.rel = "noopener noreferrer";
			link.textContent = label;
			return link.outerHTML;
		}

		getSafeFileHref(fileUrl) {
			const value = (fileUrl || "").toString().trim();
			if (!value) {
				return "";
			}

			if (value.startsWith("/")) {
				return value;
			}

			try {
				const parsed = new URL(value, window.location.origin);
				if (parsed.protocol === "http:" || parsed.protocol === "https:") {
					return parsed.href;
				}
			} catch {
				return "";
			}

			return "";
		}

		getChartsFingerprint(message) {
			const charts = message?.metadata?.frappe_charts;
			if (message?.role !== "assistant" || !Array.isArray(charts) || !charts.length) {
				return "";
			}
			try {
				return JSON.stringify(charts);
			} catch {
				return "__invalid_charts__";
			}
		}

		syncMessageCharts(entry, message, messageKey) {
			const chartFingerprint = this.getChartsFingerprint(message);
			if (entry.chartFingerprint === chartFingerprint) {
				return;
			}

			this.resetMessageCharts(entry, messageKey);
			entry.chartFingerprint = chartFingerprint;
			if (!chartFingerprint) {
				return;
			}

			this.mountFrappeChartsForMessage(entry, message, messageKey);
		}

		getAskAlyfChartChromeHeight(chart) {
			const measures = chart?.measures || {};
			const margins = measures.margins || {};
			const paddings = measures.paddings || {};
			return (
				(margins.top || 0) +
				(margins.bottom || 0) +
				(paddings.top || 0) +
				(paddings.bottom || 0) +
				(measures.titleHeight || 0) +
				(measures.legendHeight || 0)
			);
		}

		getResponsiveFrappeChartHeight(preferredHeight, widthPx, chartCount = 1) {
			const minHeight = Math.max(
				ASK_ALYF_FRAPPE_CHART_MIN_HEIGHT,
				Number.isFinite(preferredHeight) ? preferredHeight : 0
			);
			const widthDrivenHeight = Math.round(widthPx * 0.55);
			const panelHeight = this.panel?.clientHeight || 0;
			let maxHeight = ASK_ALYF_FRAPPE_CHART_MAX_HEIGHT;
			if (panelHeight > 0) {
				const stackedDivisor = Math.min(Math.max(chartCount, 1), 2);
				maxHeight = Math.max(
					minHeight,
					Math.min(
						ASK_ALYF_FRAPPE_CHART_MAX_HEIGHT,
						Math.floor((panelHeight * 0.8) / stackedDivisor)
					)
				);
			}
			return Math.max(minHeight, Math.min(maxHeight, widthDrivenHeight));
		}

		applyMountedFrappeChartLayout(chart, mount, widthPx, heightPx) {
			if (!chart || !mount?.isConnected) {
				return;
			}

			const nextWidth = String(widthPx);
			const nextHeight = String(heightPx);
			if (mount.dataset.askAlyfWidth === nextWidth && mount.dataset.askAlyfHeight === nextHeight) {
				return;
			}

			mount.style.width = `${widthPx}px`;
			mount.style.maxWidth = "100%";
			mount.dataset.askAlyfWidth = nextWidth;
			mount.dataset.askAlyfHeight = nextHeight;

			chart.argHeight = heightPx;
			chart.baseHeight = heightPx;
			chart.height = Math.max(1, heightPx - this.getAskAlyfChartChromeHeight(chart));
			chart.draw(false);
		}

		syncMessageFooter(entry, message) {
			if (message.role !== "assistant" || !message.content) {
				entry.footer?.remove();
				entry.footer = null;
				return;
			}

			if (!entry.footer) {
				const label = __("Copy as Markdown");
				const copyButton = document.createElement("button");
				copyButton.className = "ask_alyf-copy ask_alyf-icon-button";
				copyButton.type = "button";
				copyButton.title = label;
				copyButton.setAttribute("aria-label", label);
				copyButton.innerHTML =
					typeof frappe.utils?.icon === "function"
						? frappe.utils.icon("es-line-copy", "xs")
						: "Copy";
				copyButton.addEventListener("click", () =>
					frappe.utils.copy_to_clipboard(entry.copyText || "")
				);
				entry.timestampEl = document.createElement("span");
				entry.timestampEl.className = "ask_alyf-message-time";
				entry.footer = document.createElement("div");
				entry.footer.className = "ask_alyf-message-footer";
				entry.footer.append(copyButton, entry.timestampEl);
			}

			entry.copyText = message.content;
			entry.timestampEl.textContent = frappe.datetime.prettyDate(message.created_at);
			// Keep it last: charts and tool calls are inserted around the body.
			entry.wrapper.appendChild(entry.footer);
		}

		syncMessageElement(message, index, previousMessageKeys) {
			const messageKey = this.getMessageRenderKey(message, index);
			let entry = this.messageEntries.get(messageKey);
			if (!entry) {
				const wrapper = document.createElement("div");
				const body = document.createElement("div");
				body.className = "ask_alyf-message-body";
				wrapper.appendChild(body);
				entry = {
					body,
					chartFingerprint: "",
					chartHolder: null,
					chartPaintVersion: 0,
					chartResizeFrame: 0,
					chartResizeObserver: null,
					footer: null,
					html: null,
					role: null,
					timestampEl: null,
					toolCallsFingerprint: "",
					toolCallsHolder: null,
					wrapper,
				};
				this.messageEntries.set(messageKey, entry);
				if (!previousMessageKeys.has(messageKey)) {
					wrapper.classList.add("ask_alyf-message-enter");
				}
			}

			if (entry.role !== message.role) {
				entry.role = message.role;
				entry.wrapper.className = `ask_alyf-message ask_alyf-${message.role}`;
				if (!previousMessageKeys.has(messageKey)) {
					entry.wrapper.classList.add("ask_alyf-message-enter");
				}
			}

			const html = this.getMessageHtml(message);
			if (entry.html !== html) {
				entry.body.innerHTML = html;
				entry.html = html;
			}

			this.syncMessageToolCalls(entry, message);
			this.syncMessageCharts(entry, message, messageKey);
			this.syncMessageFooter(entry, message);
			return { entry, messageKey };
		}

		renderStatusMessage() {
			if (!this.messagesEl) {
				return;
			}

			if (!this.state.status) {
				if (this.statusWrapperEl) {
					this.statusWrapperEl.remove();
					this.statusWrapperEl = null;
					this.statusBodyEl = null;
				}
				return;
			}

			if (!this.statusWrapperEl) {
				this.statusWrapperEl = document.createElement("div");
				this.statusWrapperEl.className = "ask_alyf-message ask_alyf-status-message";
				this.statusBodyEl = document.createElement("div");
				this.statusBodyEl.className = "ask_alyf-message-body";
				this.statusWrapperEl.appendChild(this.statusBodyEl);
			}

			this.statusWrapperEl.className = "ask_alyf-message ask_alyf-status-message";
			if (this.state.loading) {
				this.statusWrapperEl.classList.add("ask_alyf-status-loading");
			}
			this.statusBodyEl.textContent = this.state.status;
			this.messagesEl.insertBefore(this.statusWrapperEl, this.pendingOperationsEl || null);
		}

		renderPendingOperation() {
			if (!this.messagesEl) {
				return;
			}

			const confirmable = (this.state.pendingOperations || []).filter((op) =>
				this.operationRequiresConfirmation(op)
			);

			if (!confirmable.length) {
				if (this.pendingOperationsEl) {
					this.pendingOperationsEl.remove();
					this.pendingOperationsEl = null;
				}
				return;
			}

			const container = document.createElement("div");
			container.className = "ask_alyf-proposals";

			for (const operation of confirmable) {
				const card = document.createElement("div");
				card.className = "ask_alyf-proposal";
				card.innerHTML = `
					<div class="ask_alyf-proposal-summary">${this.getPendingOperationSummaryHtml(operation)}</div>
					<div class="ask_alyf-proposal-actions">
						<button class="ask_alyf-confirm btn btn-primary btn-sm" type="button">${__("Confirm")}</button>
						<button class="ask_alyf-reject btn btn-secondary btn-sm" type="button">${__("Reject")}</button>
					</div>
				`;
				card.querySelector(".ask_alyf-confirm").addEventListener("click", () =>
					this.confirmPendingOperation(operation)
				);
				card.querySelector(".ask_alyf-reject").addEventListener("click", () =>
					this.rejectPendingOperation(operation)
				);
				container.appendChild(card);
			}

			if (confirmable.length > 1) {
				const bulkActions = document.createElement("div");
				bulkActions.className = "ask_alyf-proposal-bulk-actions";
				bulkActions.innerHTML = `
					<button class="ask_alyf-confirm-all btn btn-primary btn-xs" type="button">${__("Confirm all")}</button>
					<button class="ask_alyf-reject-all btn btn-secondary btn-xs" type="button">${__("Reject all")}</button>
				`;
				bulkActions
					.querySelector(".ask_alyf-confirm-all")
					.addEventListener("click", () => this.confirmAllPendingOperations());
				bulkActions
					.querySelector(".ask_alyf-reject-all")
					.addEventListener("click", () => this.rejectAllPendingOperations());
				container.appendChild(bulkActions);
			}

			if (this.pendingOperationsEl) {
				this.pendingOperationsEl.replaceWith(container);
			} else {
				this.messagesEl.appendChild(container);
			}
			this.pendingOperationsEl = container;
		}

		init() {
			if (this.initialized || !frappe?.boot?.ask_alyf?.allowed) {
				return;
			}

			if (this.state.mode === "Agent" && !this.isAgentModeEnabled()) {
				this.state.mode = "Ask";
			}

			this.initialized = true;
			this.make();
			this.bindRealtime();
			this.bindRouteChange();
			this.loadBootstrap();
		}

		make() {
			const root = document.createElement("div");
			root.className = "ask_alyf-root";
			root.innerHTML = `
				<button class="ask_alyf-bubble" type="button" title="${__("Open Ask ALYF")}" aria-label="${__(
				"Open Ask ALYF"
			)}"><img class="ask_alyf-bubble-logo" src="/assets/ask_alyf/img/logo.png" alt="" aria-hidden="true"></button>
				<div class="ask_alyf-panel ask_alyf-hidden">
					<div class="ask_alyf-resize-handle" title="${__("Resize chat window")}"></div>
					<div class="ask_alyf-header">
						<div>
							<div class="ask_alyf-title">Ask ALYF</div>
							<div class="ask_alyf-subtitle">${__("ERPNext assistant")}</div>
						</div>
						<div class="ask_alyf-actions">
							<button class="ask_alyf-header-button ask_alyf-new-chat btn btn-secondary btn-sm" type="button" title="${__(
								"Start a new conversation"
							)}" aria-label="${__("New chat")}">${__("New chat")}</button>
							<a class="ask_alyf-header-button ask_alyf-support-phone btn btn-secondary btn-sm ask_alyf-hidden" href="#" role="button" title="${__(
								"Call support"
							)}" aria-label="${__(
				"Call support"
			)}"><i class="fa fa-phone" aria-hidden="true"></i></a>
							<button class="ask_alyf-header-button ask_alyf-close btn btn-secondary btn-sm" type="button" title="${__(
								"Close"
							)}" aria-label="${__("Close")}">&times;</button>
						</div>
					</div>
					<div class="form-tabs-list ask_alyf-tabs-list">
						<ul class="nav form-tabs ask_alyf-tabs" role="tablist" aria-label="${__("Ask ALYF sections")}">
							<li class="nav-item">
								<button class="nav-link ask_alyf-tab active" type="button" role="tab" data-tab="chat" aria-selected="true">${__(
									"Chat"
								)}</button>
							</li>
							<li class="nav-item">
								<button class="nav-link ask_alyf-tab" type="button" role="tab" data-tab="history" aria-selected="false">${__(
									"History"
								)}</button>
							</li>
						</ul>
					</div>
					<div class="ask_alyf-config-warning ask_alyf-hidden"></div>
					<div class="ask_alyf-chat-view">
						<div class="ask_alyf-messages"></div>
						<div class="ask_alyf-composer">
							<div class="ask_alyf-input-shell">
								<textarea class="ask_alyf-input" rows="3" placeholder="${__("Ask about this ERPNext instance")}"></textarea>
								<div class="ask_alyf-mode-dropdown">
									<button class="ask_alyf-mode-trigger btn btn-secondary btn-sm" type="button" aria-haspopup="menu" aria-expanded="false">
										<span class="ask_alyf-mode-trigger-label"></span>
										<i class="fa fa-chevron-down ask_alyf-mode-trigger-chevron" aria-hidden="true"></i>
									</button>
									<div class="ask_alyf-mode-menu ask_alyf-hidden" role="menu">
										<button class="ask_alyf-mode-option btn btn-secondary btn-sm" type="button" role="menuitemradio" data-mode="Ask">${__(
											"Ask"
										)}</button>
										<button class="ask_alyf-mode-option btn btn-secondary btn-sm" type="button" role="menuitemradio" data-mode="Agent">${__(
											"Agent"
										)}</button>
									</div>
								</div>
								<div class="ask_alyf-composer-actions">
									<button class="ask_alyf-icon-button ask_alyf-attach btn btn-secondary btn-sm ask_alyf-hidden" type="button" title="${__(
										"Attach file"
									)}" aria-label="${__(
				"Attach file"
			)}"><i class="fa fa-paperclip"></i></button>
									<button class="ask_alyf-icon-button ask_alyf-mic btn btn-secondary btn-sm" type="button" title="${__(
										"Voice input"
									)}" aria-label="${__(
				"Voice input"
			)}"><i class="fa fa-microphone"></i></button>
									<button class="ask_alyf-send btn btn-primary btn-sm" type="button">${__("Send")}</button>
								</div>
							</div>
							<div class="ask_alyf-disclaimer">${__(
								"Ask ALYF is an AI and can make mistakes, including with numbers and information about people."
							)}</div>
						</div>
					</div>
					<div class="ask_alyf-history-view ask_alyf-hidden">
						<div class="ask_alyf-history-list"></div>
					</div>
				</div>
			`;

			document.body.appendChild(root);

			this.root = root;
			this.panel = root.querySelector(".ask_alyf-panel");
			this.messagesEl = root.querySelector(".ask_alyf-messages");
			this.warningEl = root.querySelector(".ask_alyf-config-warning");
			this.inputEl = root.querySelector(".ask_alyf-input");
			this.bubbleEl = root.querySelector(".ask_alyf-bubble");
			this.sendEl = root.querySelector(".ask_alyf-send");
			this.attachEl = root.querySelector(".ask_alyf-attach");
			this.micEl = root.querySelector(".ask_alyf-mic");
			this.micEl.setAttribute("aria-pressed", "false");
			this.resizeHandleEl = root.querySelector(".ask_alyf-resize-handle");
			this.chatViewEl = root.querySelector(".ask_alyf-chat-view");
			this.historyViewEl = root.querySelector(".ask_alyf-history-view");
			this.historyListEl = root.querySelector(".ask_alyf-history-list");
			this.tabEls = Array.from(root.querySelectorAll(".ask_alyf-tab"));
			this.modeDropdownEl = root.querySelector(".ask_alyf-mode-dropdown");
			this.modeTriggerEl = root.querySelector(".ask_alyf-mode-trigger");
			this.modeTriggerLabelEl = root.querySelector(".ask_alyf-mode-trigger-label");
			this.modeMenuEl = root.querySelector(".ask_alyf-mode-menu");
			this.modeOptionEls = Array.from(root.querySelectorAll(".ask_alyf-mode-option"));
			this.supportPhoneEl = root.querySelector(".ask_alyf-support-phone");
			this.tabEls.forEach((tabEl) => {
				tabEl.addEventListener("click", (event) => this.onTabClick(event));
			});
			this.modeTriggerEl.addEventListener("click", (event) => this.onModeTriggerClick(event));
			this.modeOptionEls.forEach((optionEl) => {
				optionEl.addEventListener("click", (event) => this.onModeOptionClick(event));
			});
			document.addEventListener("click", this.boundDocumentClick);
			this.syncModeControl();
			this.syncSupportPhoneAction(frappe?.boot?.ask_alyf || {});

			root.querySelector(".ask_alyf-bubble").addEventListener("click", () => this.toggle(true));
			root.querySelector(".ask_alyf-close").addEventListener("click", () => this.toggle(false));
			root.querySelector(".ask_alyf-send").addEventListener("click", () => this.sendMessage());
			root.querySelector(".ask_alyf-new-chat").addEventListener("click", () =>
				this.startNewConversation()
			);
			this.attachEl.addEventListener("click", () => this.openFileUploader());
			this.micEl.addEventListener("click", () => this.startVoiceInput());
			this.resizeHandleEl.addEventListener("pointerdown", (event) => this.startPanelResize(event));
			this.inputEl.addEventListener("keydown", (event) => {
				if (event.key === "Enter" && !event.shiftKey) {
					event.preventDefault();
					this.sendMessage();
					return;
				}
				if (event.key === "Escape") {
					this.closeModeMenu();
				}
			});
			this.inputEl.addEventListener("input", () => this.autoResizeInput());
			this.updateVoiceInputHint();
			this.autoResizeInput();
			this.setActiveTab(this.state.activeTab);
		}

		bindRealtime() {
			frappe.realtime.on("ask_alyf_status", (message) => {
				if (message.conversation !== this.state.conversation?.name) return;
				this.setStatus(message.text || "");
			});

			frappe.realtime.on("ask_alyf_response_start", (message) => {
				if (message.conversation !== this.state.conversation?.name) return;
				this.setLoading(true);
				this.clearLiveSteps();
				this.setStatus(__("Thinking..."));
			});

			frappe.realtime.on("ask_alyf_step", (message) => {
				if (message.conversation !== this.state.conversation?.name) return;
				this.applyStepUpdate(message.step);
			});

			frappe.realtime.on("ask_alyf_file_attachment", (message) => {
				if (message.conversation !== this.state.conversation?.name) return;
				if (message.message) {
					this.state.messages.push(message.message);
					this.renderMessages();
					this.scrollToBottom();
				}
			});

			frappe.realtime.on("ask_alyf_response_chunk", (message) => {
				if (message.conversation !== this.state.conversation?.name) return;
				this.appendAssistantChunk(message.message_id, message.chunk || "");
			});

			frappe.realtime.on("ask_alyf_response_complete", async (message) => {
				if (message.conversation !== this.state.conversation?.name) return;
				this.stopResponseJobMonitor();
				this.setLoading(false);
				this.setStatus("");
				this.adoptCompletedToolCalls(message.message_id, message.tool_calls);
				this.clearLiveSteps();
				this.state.pendingOperations = this.normalizePendingOperations(message.pending_operations);
				this.pendingStreamMessageId = null;

				for (const op of this.state.pendingOperations) {
					await this.ensureDoctypeMeta(op);
				}

				this.renderMessages();
				this.refreshConversationList();
				this.maybeAutoExecuteFrontendActions();
			});
		}

		bindRouteChange() {
			frappe.router.on("change", () => {
				if (this.state.open) {
					this.setStatus("");
				}
			});
		}

		async loadBootstrap() {
			const response = await frappe.call({
				method: "ask_alyf.api.bootstrap",
				args: {
					conversation: this.state.conversation?.name,
				},
			});
			const askAlyfBoot = response.message.ask_alyf || {};
			await this.applyConversation(response.message.conversation);
			this.syncSupportPhoneAction(askAlyfBoot);
			this.syncFileUploadButton();
			await this.refreshConversationList();

			if (!askAlyfBoot.configured) {
				this.warningEl.classList.remove("ask_alyf-hidden");
				this.warningEl.textContent = __(
					"Ask ALYF is visible, but no API key/model is configured yet in Ask ALYF Settings."
				);
			}
		}

		async ensureDoctypeMeta(operation) {
			const doctype = operation?.payload?.doctype;
			if (!doctype) return;
			try {
				await frappe.model.with_doctype(doctype);
			} catch (e) {
				// Ignore errors if doctype doesn't exist or user lacks permission
			}
		}

		async applyConversation(conversation) {
			this.stopResponseJobMonitor();
			this.pendingStreamMessageId = null;
			this.handledFrontendCallIds = new Set();
			// Steps of the run that just finished are part of its assistant
			// message now, and steps of another conversation are not ours.
			this.state.steps = [];
			this.state.conversation = conversation;
			this.state.messages = conversation.messages || [];
			this.state.pendingOperations = this.normalizePendingOperations(conversation.pending_operations);

			for (const op of this.state.pendingOperations) {
				await this.ensureDoctypeMeta(op);
			}

			this.cacheRenderedMessageKeys(this.state.messages);
			this.syncConversationMode(this.state.messages);
			this.renderHistoryList();
			this.renderMessages();
			this.restoreProcessingState();
		}

		getAwaitingResponseMessage() {
			const messages = this.state.messages;
			if (!messages.length || this.state.pendingOperations.length) {
				return null;
			}
			const lastMessage = messages[messages.length - 1];
			if (lastMessage.role !== "user") {
				return null;
			}
			const createdAt = lastMessage.created_at;
			if (createdAt && !lastMessage.metadata?.background_job_id) {
				const ageMs = Date.now() - new Date(createdAt).getTime();
				if (ageMs > 5 * 60 * 1000) {
					return null;
				}
			}
			return lastMessage;
		}

		isAwaitingResponse() {
			return Boolean(this.getAwaitingResponseMessage());
		}

		restoreProcessingState() {
			const userMessage = this.getAwaitingResponseMessage();
			if (!userMessage) {
				return;
			}

			this.setLoading(true);
			this.setStatus(__("Processing..."));
			const jobId = userMessage.metadata?.background_job_id;
			if (jobId) {
				this.startResponseJobMonitor({
					jobId,
					conversation: this.state.conversation?.name,
					userMessageId: userMessage.id,
				});
			}
		}

		stopResponseJobMonitor() {
			this.responseJobMonitorVersion += 1;
			if (this.responseJobPollTimer) {
				clearTimeout(this.responseJobPollTimer);
				this.responseJobPollTimer = null;
			}
			this.activeResponseJob = null;
		}

		startResponseJobMonitor({ jobId, conversation, userMessageId }) {
			this.stopResponseJobMonitor();
			if (!jobId || !conversation || !userMessageId) {
				return;
			}

			const version = this.responseJobMonitorVersion;
			this.activeResponseJob = {
				conversation,
				jobId,
				missingChecks: 0,
				userMessageId,
				version,
			};
			this.scheduleResponseJobPoll(version);
		}

		scheduleResponseJobPoll(version) {
			if (this.activeResponseJob?.version !== version) {
				return;
			}
			this.responseJobPollTimer = setTimeout(
				() => this.pollResponseJob(version),
				ASK_ALYF_JOB_POLL_INTERVAL_MS
			);
		}

		async pollResponseJob(version) {
			const activeJob = this.activeResponseJob;
			this.responseJobPollTimer = null;
			if (
				!activeJob ||
				activeJob.version !== version ||
				activeJob.conversation !== this.state.conversation?.name
			) {
				return;
			}

			let response;
			try {
				response = await frappe.call({
					method: "ask_alyf.ask_alyf.api.get_message_job_status",
					args: {
						conversation: activeJob.conversation,
						job_id: activeJob.jobId,
						user_message_id: activeJob.userMessageId,
					},
				});
			} catch {
				this.scheduleResponseJobPoll(version);
				return;
			}

			if (this.activeResponseJob?.version !== version) {
				return;
			}

			const result = response.message || {};
			if (result.status === "pending") {
				activeJob.missingChecks = 0;
				this.adoptRunningSteps(result.tool_calls);
				this.scheduleResponseJobPoll(version);
				return;
			}

			if (result.status === "completed") {
				this.stopResponseJobMonitor();
				if (activeJob.conversation !== this.state.conversation?.name) {
					return;
				}
				if (result.conversation) {
					await this.applyConversation(result.conversation);
				}
				this.setLoading(false);
				this.setStatus("");
				this.refreshConversationList();
				this.maybeAutoExecuteFrontendActions();
				return;
			}

			if (result.status === "missing") {
				activeJob.missingChecks += 1;
				if (activeJob.missingChecks < ASK_ALYF_MISSING_JOB_CHECKS) {
					this.scheduleResponseJobPoll(version);
					return;
				}
			}

			this.stopResponseJobMonitor();
			if (activeJob.conversation !== this.state.conversation?.name) {
				return;
			}
			this.pendingStreamMessageId = null;
			this.setLoading(false);
			this.setStatus(
				result.status === "failed"
					? __("Ask ALYF could not finish processing this message. Please try again.")
					: __("Ask ALYF stopped processing this message. Please try again.")
			);
		}

		onTabClick(event) {
			const selectedTab = event.currentTarget?.dataset?.tab || "chat";
			this.setActiveTab(selectedTab);
			if (selectedTab === "history") {
				this.refreshConversationList();
			}
		}

		setActiveTab(tabName) {
			const nextTab = tabName === "history" ? "history" : "chat";
			this.state.activeTab = nextTab;

			const showHistory = nextTab === "history";
			this.chatViewEl?.classList.toggle("ask_alyf-hidden", showHistory);
			this.historyViewEl?.classList.toggle("ask_alyf-hidden", !showHistory);

			this.tabEls.forEach((tabEl) => {
				const isActive = tabEl.dataset.tab === nextTab;
				tabEl.classList.toggle("active", isActive);
				tabEl.setAttribute("aria-selected", isActive ? "true" : "false");
			});

			if (nextTab === "chat") {
				requestAnimationFrame(() => {
					requestAnimationFrame(() => this.flushDeferredChartPaints());
				});
			}
		}

		onHistoryConversationClick(event) {
			const conversationName = event.currentTarget?.dataset?.conversation;
			if (!conversationName) {
				return;
			}

			this.setActiveTab("chat");
			if (conversationName === this.state.conversation?.name) {
				this.syncConversationMode();
				this.inputEl?.focus();
				return;
			}

			this.openConversation(conversationName);
		}

		onModeOptionClick(event) {
			const option = event.currentTarget;
			const selectedMode = option?.dataset?.mode;
			if (!selectedMode || option.disabled) {
				return;
			}

			this.state.mode = selectedMode;
			this.syncModeControl();
		}

		setModeToAskDefault() {
			this.state.mode = "Ask";
			this.syncModeControl();
		}

		getConversationMode(messages = []) {
			for (let index = messages.length - 1; index >= 0; index -= 1) {
				const storedMode = messages[index]?.metadata?.mode;
				if (storedMode === "Ask" || storedMode === "Agent") {
					return storedMode;
				}
			}
			return "Ask";
		}

		syncConversationMode(messages = this.state.messages) {
			this.state.mode = this.getConversationMode(messages);
			this.syncModeControl();
		}

		isAgentModeEnabled() {
			const askAlyfSettings = frappe?.boot?.ask_alyf || {};
			return Boolean(askAlyfSettings.agent_mode_enabled);
		}

		isFileUploadEnabled() {
			return Boolean(frappe?.boot?.ask_alyf?.file_upload_enabled);
		}

		syncFileUploadButton() {
			if (!this.attachEl) {
				return;
			}
			this.attachEl.classList.toggle("ask_alyf-hidden", !this.isFileUploadEnabled());
		}

		openFileUploader() {
			if (!this.state.conversation?.name || this.state.loading) {
				return;
			}
			new frappe.ui.FileUploader({
				doctype: "Ask ALYF Conversation",
				docname: this.state.conversation.name,
				on_success: (file_doc) => this.onFileUploaded(file_doc),
			});
		}

		async onFileUploaded(fileDoc) {
			if (!fileDoc?.file_name) {
				return;
			}
			try {
				const response = await frappe.call({
					method: "ask_alyf.api.attach_file",
					type: "POST",
					args: {
						conversation: this.state.conversation.name,
						file: {
							name: fileDoc.name,
							file_name: fileDoc.file_name,
						},
					},
				});
				if (response.message?.conversation) {
					await this.applyConversation(response.message.conversation);
				}
			} catch (error) {
				frappe.msgprint(error.message || __("Failed to attach file to conversation."));
			}
		}

		syncSupportPhoneAction(askAlyfBoot = {}) {
			if (!this.supportPhoneEl) {
				return;
			}

			const supportPhoneNumber = (askAlyfBoot.support_phone_number || "").toString().trim();
			const supportPhoneUriFromBoot = (askAlyfBoot.support_phone_uri || "").toString().trim();
			const supportPhoneUri = supportPhoneUriFromBoot.startsWith("tel:")
				? supportPhoneUriFromBoot
				: this.getSupportPhoneUri(supportPhoneNumber);

			if (!supportPhoneUri) {
				this.supportPhoneEl.classList.add("ask_alyf-hidden");
				this.supportPhoneEl.removeAttribute("href");
				return;
			}

			const phoneButtonLabel = supportPhoneNumber
				? __("Call support: {0}", [supportPhoneNumber])
				: __("Call support");
			this.supportPhoneEl.href = supportPhoneUri;
			this.supportPhoneEl.title = phoneButtonLabel;
			this.supportPhoneEl.setAttribute("aria-label", phoneButtonLabel);
			this.supportPhoneEl.classList.remove("ask_alyf-hidden");
		}

		getSupportPhoneUri(phoneNumber) {
			const normalizedPhoneNumber = (phoneNumber || "").toString().trim();
			if (!normalizedPhoneNumber) {
				return "";
			}

			const digitsOnly = normalizedPhoneNumber.replace(/\D/g, "");
			if (!digitsOnly) {
				return "";
			}

			const prefix = normalizedPhoneNumber.startsWith("+") ? "+" : "";
			return `tel:${prefix}${digitsOnly}`;
		}

		onModeTriggerClick(event) {
			event.preventDefault();
			event.stopPropagation();
			const menuOpen = !this.modeMenuEl.classList.contains("ask_alyf-hidden");
			if (menuOpen) {
				this.closeModeMenu();
				return;
			}
			this.openModeMenu();
		}

		syncModeControl() {
			if (!this.modeTriggerEl) {
				return;
			}

			const isAgentModeAllowed = this.isAgentModeEnabled();
			if (!isAgentModeAllowed && this.state.mode === "Agent") {
				this.state.mode = "Ask";
			}

			const modeLabel = this.state.mode === "Agent" ? __("Agent") : __("Ask");
			this.modeTriggerLabelEl.textContent = modeLabel;
			this.modeTriggerEl.setAttribute("aria-label", __("Mode: {0}", modeLabel));

			this.modeOptionEls.forEach((option) => {
				const optionMode = option.dataset.mode;
				const isSelected = optionMode === this.state.mode;
				const isDisabled = optionMode === "Agent" && !isAgentModeAllowed;
				option.classList.toggle("btn-primary", isSelected);
				option.classList.toggle("btn-secondary", !isSelected);
				option.classList.toggle("is-disabled", isDisabled);
				option.disabled = isDisabled;
				option.setAttribute("aria-checked", isSelected ? "true" : "false");
			});

			this.closeModeMenu();
		}

		onDocumentClick(event) {
			if (!this.modeDropdownEl || this.modeMenuEl.classList.contains("ask_alyf-hidden")) {
				return;
			}
			if (this.modeDropdownEl.contains(event.target)) {
				return;
			}
			this.closeModeMenu();
		}

		openModeMenu() {
			if (!this.modeMenuEl) {
				return;
			}
			this.modeMenuEl.classList.remove("ask_alyf-hidden");
			this.modeTriggerEl.setAttribute("aria-expanded", "true");
		}

		closeModeMenu() {
			if (!this.modeMenuEl) {
				return;
			}
			this.modeMenuEl.classList.add("ask_alyf-hidden");
			this.modeTriggerEl.setAttribute("aria-expanded", "false");
		}

		formatConversationLabel(conversation) {
			const title = (conversation.title || "").trim() || __("Untitled conversation");
			return `${title}`;
		}

		formatConversationTimestamp(conversation) {
			const timestamp = conversation.last_message_at || conversation.modified;
			if (!timestamp) {
				return "";
			}

			if (!frappe.datetime?.str_to_user) {
				return timestamp;
			}

			try {
				return frappe.datetime.str_to_user(timestamp);
			} catch {
				return timestamp;
			}
		}

		getRolePrompts() {
			return [
				{
					roles: ["Sales User", "Sales Manager"],
					label: __("Selling"),
					prompts: [
						__("Chart my monthly sales revenue for the last 6 months"),
						__("What Sales Orders are pending delivery?"),
						__("How many quotations were sent this month?"),
					],
				},
				{
					roles: ["Purchase User", "Purchase Manager"],
					label: __("Buying"),
					prompts: [
						__("Chart spending by supplier for this quarter"),
						__("Which Purchase Orders are overdue?"),
						__("How many pending purchase receipts do I have?"),
					],
				},
				{
					roles: ["Accounts User", "Accounts Manager"],
					label: __("Accounts"),
					prompts: [
						__("Chart monthly expenses by cost center"),
						__("Show me unpaid Sales Invoices older than 30 days"),
						__("What's the total outstanding receivables?"),
					],
				},
				{
					roles: ["HR User", "HR Manager"],
					label: __("HR"),
					prompts: [
						__("Show employee headcount by department as a chart"),
						__("Which employees are on leave today?"),
						__("How many leave applications are pending approval?"),
					],
				},
				{
					roles: ["Stock User", "Stock Manager"],
					label: __("Stock"),
					prompts: [
						__("Chart stock value by warehouse"),
						__("Which items are below their reorder level?"),
						__("What were the top 10 most moved items this month?"),
					],
				},
				{
					roles: ["Manufacturing User", "Manufacturing Manager"],
					label: __("Manufacturing"),
					prompts: [
						__("Chart production output by item this week"),
						__("Show me open Work Orders and their status"),
						__("How many Work Orders are behind schedule?"),
					],
				},
				{
					roles: ["Projects User", "Projects Manager"],
					label: __("Projects"),
					prompts: [
						__("Show project progress as a chart"),
						__("What open tasks are assigned to me?"),
						__("Which project tasks are overdue?"),
					],
				},
				{
					roles: ["Support Team"],
					label: __("Support"),
					prompts: [
						__("Chart open issues by priority"),
						__("Show me unresolved issues from this week"),
						__("What's the average resolution time for issues?"),
					],
				},
				{
					roles: ["System Manager", "Administrator"],
					label: __("System"),
					prompts: [
						__("Show me the top 10 DocTypes by record count"),
						__("What are the recent Error Logs?"),
						__("Which users logged in today?"),
					],
				},
			];
		}

		getSuggestedPrompts() {
			const userRoles = new Set(frappe.user_roles || []);
			if (!userRoles.size) {
				return [];
			}

			const matchingGroups = this.getRolePrompts().filter((group) =>
				group.roles.some((role) => userRoles.has(role))
			);
			if (!matchingGroups.length) {
				return [];
			}

			const shuffled = this.shuffleArray(
				matchingGroups.map((group) => ({
					label: group.label,
					prompts: this.shuffleArray([...group.prompts]),
				}))
			);

			const prompts = [];
			const indices = shuffled.map(() => 0);
			for (let round = 0; prompts.length < 3 && round < 3; round++) {
				for (let i = 0; i < shuffled.length && prompts.length < 3; i++) {
					const group = shuffled[i];
					const idx = indices[i];
					if (idx < group.prompts.length) {
						prompts.push({ group: group.label, text: group.prompts[idx] });
						indices[i]++;
					}
				}
			}

			return prompts;
		}

		shuffleArray(array) {
			for (let i = array.length - 1; i > 0; i--) {
				const j = Math.floor(Math.random() * (i + 1));
				[array[i], array[j]] = [array[j], array[i]];
			}
			return array;
		}

		renderSuggestedPrompts() {
			if (!this.messagesEl) {
				return;
			}

			if (this.suggestedPromptsEl) {
				this.suggestedPromptsEl.remove();
				this.suggestedPromptsEl = null;
			}

			if (this.state.messages.length) {
				return;
			}

			const prompts = this.getSuggestedPrompts();
			if (!prompts.length) {
				return;
			}

			const grouped = new Map();
			for (const prompt of prompts) {
				if (!grouped.has(prompt.group)) {
					grouped.set(prompt.group, []);
				}
				grouped.get(prompt.group).push(prompt.text);
			}

			const container = document.createElement("div");
			container.className = "ask_alyf-suggested-prompts";

			for (const [groupLabel, groupPrompts] of grouped) {
				const groupEl = document.createElement("div");
				groupEl.className = "ask_alyf-prompt-group";

				if (groupLabel) {
					const labelEl = document.createElement("div");
					labelEl.className = "ask_alyf-prompt-group-label";
					labelEl.textContent = groupLabel;
					groupEl.appendChild(labelEl);
				}

				for (const text of groupPrompts) {
					const button = document.createElement("button");
					button.type = "button";
					button.className = "ask_alyf-suggested-prompt";
					button.textContent = text;
					button.addEventListener("click", () => {
						this.inputEl.value = text;
						this.sendMessage();
					});
					groupEl.appendChild(button);
				}

				container.appendChild(groupEl);
			}

			this.suggestedPromptsEl = container;
			this.messagesEl.appendChild(container);
		}

		renderHistoryList() {
			if (!this.historyListEl) {
				return;
			}

			const currentName = this.state.conversation?.name || "";
			const recentConversations = (this.state.conversations || [])
				.filter((conversation) => conversation?.name)
				.slice(0, 20);
			this.historyListEl.innerHTML = "";

			if (!recentConversations.length) {
				const emptyStateEl = document.createElement("div");
				emptyStateEl.className = "ask_alyf-history-empty";
				emptyStateEl.textContent = __("No conversations yet.");
				this.historyListEl.appendChild(emptyStateEl);
				return;
			}

			recentConversations.forEach((conversation) => {
				const itemEl = document.createElement("button");
				itemEl.type = "button";
				itemEl.className = "ask_alyf-history-item btn btn-secondary btn-sm";
				itemEl.dataset.conversation = conversation.name;

				if (conversation.name === currentName) {
					itemEl.classList.remove("btn-secondary");
					itemEl.classList.add("btn-primary");
				}

				const titleEl = document.createElement("div");
				titleEl.className = "ask_alyf-history-item-title";
				titleEl.textContent = this.formatConversationLabel(conversation);

				const metaEl = document.createElement("div");
				metaEl.className = "ask_alyf-history-item-meta";
				const timestampLabel = this.formatConversationTimestamp(conversation);
				metaEl.textContent = timestampLabel || "";

				itemEl.appendChild(titleEl);
				itemEl.appendChild(metaEl);
				itemEl.addEventListener("click", (event) => this.onHistoryConversationClick(event));
				this.historyListEl.appendChild(itemEl);
			});
		}

		async refreshConversationList() {
			try {
				const response = await frappe.call({
					method: "ask_alyf.api.list_conversations",
					args: { limit: 20 },
				});
				this.state.conversations = response.message || [];
				this.renderHistoryList();
			} catch {
				// Ignore list refresh errors to keep chat usable.
			}
		}

		async openConversation(conversationName) {
			this.setLoading(true);
			this.setStatus(__("Loading conversation..."));

			try {
				const response = await frappe.call({
					method: "ask_alyf.api.bootstrap",
					args: { conversation: conversationName },
				});
				await this.applyConversation(response.message.conversation);
				if (!this.isAwaitingResponse()) {
					this.setLoading(false);
					this.setStatus("");
				}
			} catch (error) {
				this.setLoading(false);
				this.setStatus("");
				frappe.msgprint(error.message || __("Failed to open conversation."));
				this.renderHistoryList();
			}
		}

		toggle(open) {
			this.state.open = open;
			this.panel.classList.toggle("ask_alyf-hidden", !open);
			this.bubbleEl.classList.toggle("ask_alyf-hidden", open);
			this.closeModeMenu();
			if (open) {
				this.playPanelEnterAnimation();
				this.autoResizeInput();
				if (this.state.activeTab === "chat") {
					this.inputEl.focus();
				}
				this.refreshConversationList();
				requestAnimationFrame(() => {
					requestAnimationFrame(() => this.flushDeferredChartPaints());
				});
			} else {
				this.stopPanelResize();
			}
		}

		setLoading(value) {
			this.state.loading = value;
			this.root.classList.toggle("ask_alyf-loading", value);
			if (this.sendEl) {
				this.sendEl.setAttribute("aria-busy", value ? "true" : "false");
			}
			this.renderStatusMessage();
		}

		setStatus(text) {
			if ((text || "") === this.state.status) {
				return;
			}
			this.state.status = text || "";
			this.renderStatusMessage();
			this.scrollToBottom();
		}

		getActiveFormTab(frm) {
			const tab = frm.get_active_tab?.();
			if (!tab?.df?.fieldname) {
				return null;
			}

			const label = tab.label || tab.df.label || tab.df.fieldname;
			return {
				fieldname: tab.df.fieldname,
				label: __(label, null, tab.doctype || frm.doctype),
			};
		}

		getCurrentContext() {
			const route = frappe.get_route() || [];
			const context = {
				route: route.join("/"),
				route_parts: route,
				lang: frappe?.boot?.lang || document.documentElement.lang || "en",
				user_defaults: { ...(frappe?.boot?.user?.defaults || {}) },
			};

			if (frappe.session?.user !== "Administrator") {
				context.user_roles = frappe.user_roles || [];
			}

			if (route[0] === "Form" && window.cur_frm?.doc) {
				const frm = window.cur_frm;
				context.current_doctype = frm.doc.doctype;
				context.current_docname = frm.doc.name;
				context.current_form_tab = this.getActiveFormTab(frm);
			}

			if (route[0] === "List" && window.cur_list?.filter_area) {
				context.list_filters = cur_list.filter_area.get().map((filter) => filter.slice(0, 4));
				context.list_doctype = cur_list.doctype;
			}

			return context;
		}

		autoResizeInput() {
			if (!this.inputEl) {
				return;
			}

			this.inputEl.style.height = "auto";
			const computedMinHeight = Number.parseFloat(getComputedStyle(this.inputEl).minHeight);
			const computedMaxHeight = Number.parseFloat(getComputedStyle(this.inputEl).maxHeight);
			const minHeight = Number.isFinite(computedMinHeight) ? computedMinHeight : 0;
			const maxHeight = Number.isFinite(computedMaxHeight)
				? computedMaxHeight
				: this.inputEl.scrollHeight || minHeight;
			const contentHeight = this.inputEl.scrollHeight || minHeight;
			const nextHeight = Math.max(minHeight, Math.min(contentHeight, maxHeight));

			this.inputEl.style.height = `${nextHeight}px`;
			this.inputEl.style.overflowY = this.inputEl.scrollHeight > nextHeight ? "auto" : "hidden";
		}

		startPanelResize(event) {
			if (event.button !== undefined && event.button !== 0) {
				return;
			}

			event.preventDefault();
			const panelRect = this.panel.getBoundingClientRect();
			const bounds = this.getPanelResizeBounds();
			this.resizeState = {
				pointerId: event.pointerId,
				startX: event.clientX,
				startY: event.clientY,
				startWidth: panelRect.width,
				startHeight: panelRect.height,
				...bounds,
			};
			this.root.classList.add("ask_alyf-resizing");

			if (this.resizeHandleEl?.setPointerCapture) {
				try {
					this.resizeHandleEl.setPointerCapture(event.pointerId);
				} catch {
					// Ignore pointer capture failures.
				}
			}

			window.addEventListener("pointermove", this.boundResizeMove);
			window.addEventListener("pointerup", this.boundResizeEnd);
			window.addEventListener("pointercancel", this.boundResizeEnd);
		}

		resizePanel(event) {
			if (!this.resizeState) {
				return;
			}

			if (event.pointerId !== undefined && event.pointerId !== this.resizeState.pointerId) {
				return;
			}

			event.preventDefault();
			const deltaX = this.resizeState.startX - event.clientX;
			const deltaY = this.resizeState.startY - event.clientY;
			const nextWidth = this.clamp(
				this.resizeState.startWidth + deltaX,
				this.resizeState.minWidth,
				this.resizeState.maxWidth
			);
			const nextHeight = this.clamp(
				this.resizeState.startHeight + deltaY,
				this.resizeState.minHeight,
				this.resizeState.maxHeight
			);

			this.panel.style.width = `${nextWidth}px`;
			this.panel.style.height = `${nextHeight}px`;
		}

		stopPanelResize(event) {
			if (!this.resizeState) {
				return;
			}

			if (
				event?.pointerId !== undefined &&
				this.resizeState.pointerId !== undefined &&
				event.pointerId !== this.resizeState.pointerId
			) {
				return;
			}

			if (event?.pointerId !== undefined && this.resizeHandleEl?.releasePointerCapture) {
				try {
					this.resizeHandleEl.releasePointerCapture(event.pointerId);
				} catch {
					// Ignore pointer capture release failures.
				}
			}

			this.resizeState = null;
			this.root.classList.remove("ask_alyf-resizing");
			window.removeEventListener("pointermove", this.boundResizeMove);
			window.removeEventListener("pointerup", this.boundResizeEnd);
			window.removeEventListener("pointercancel", this.boundResizeEnd);
		}

		getPanelResizeBounds() {
			const styles = getComputedStyle(this.panel);
			const minWidth = Number.parseFloat(styles.minWidth);
			const minHeight = Number.parseFloat(styles.minHeight);
			const maxWidth = Number.parseFloat(styles.maxWidth);
			const maxHeight = Number.parseFloat(styles.maxHeight);
			const fallbackMaxWidth = Math.max(window.innerWidth - 16, 280);
			const fallbackMaxHeight = Math.max(window.innerHeight - 16, 320);
			const resolvedMinWidth = Number.isFinite(minWidth) ? minWidth : 280;
			const resolvedMinHeight = Number.isFinite(minHeight) ? minHeight : 320;
			const resolvedMaxWidth = Number.isFinite(maxWidth) ? maxWidth : fallbackMaxWidth;
			const resolvedMaxHeight = Number.isFinite(maxHeight) ? maxHeight : fallbackMaxHeight;

			return {
				minWidth: resolvedMinWidth,
				minHeight: resolvedMinHeight,
				maxWidth: Math.max(resolvedMinWidth, resolvedMaxWidth),
				maxHeight: Math.max(resolvedMinHeight, resolvedMaxHeight),
			};
		}

		clamp(value, min, max) {
			return Math.min(Math.max(value, min), max);
		}

		async sendMessage() {
			const text = this.inputEl.value.trim();
			if (!text || this.state.loading) {
				return;
			}
			const requestedConversation = this.state.conversation?.name || null;

			this.setActiveTab("chat");
			this.toggle(true);
			this.setLoading(true);
			this.setStatus(__("Sending..."));

			const optimisticMessage = {
				id: `local-${Date.now()}`,
				role: "user",
				content: text,
			};
			this.state.messages.push(optimisticMessage);
			this.state.pendingOperations = [];
			this.renderMessages();
			this.inputEl.value = "";
			this.autoResizeInput();

			try {
				const response = await frappe.call({
					method: "ask_alyf.api.send_message",
					type: "POST",
					args: {
						message: text,
						mode: this.state.mode,
						conversation: this.state.conversation?.name,
						context: this.getCurrentContext(),
					},
				});
				const responseConversation = response.message.conversation;
				const currentConversation = this.state.conversation?.name || null;
				const stillViewingConversation = requestedConversation
					? currentConversation === requestedConversation
					: !currentConversation || currentConversation === responseConversation;
				if (!stillViewingConversation) {
					this.refreshConversationList();
					return;
				}

				if (responseConversation) {
					this.state.conversation = {
						...(this.state.conversation || {}),
						name: responseConversation,
					};
				}
				optimisticMessage.id = response.message.user_message_id || optimisticMessage.id;
				optimisticMessage.metadata = {
					background_job_id: response.message.job_id,
					mode: this.state.mode,
				};
				this.renderMessages();
				this.refreshConversationList();
				if (this.state.loading) {
					this.setStatus(__("Waiting for response..."));
					this.startResponseJobMonitor({
						jobId: response.message.job_id,
						conversation: responseConversation,
						userMessageId: response.message.user_message_id,
					});
				}
			} catch (error) {
				if (this.state.conversation?.name === requestedConversation) {
					this.setLoading(false);
					this.setStatus("");
				}
				frappe.msgprint(error.message || __("Failed to send message."));
			}
		}

		appendAssistantChunk(messageId, chunk) {
			let message = this.state.messages.find((item) => item.id === messageId);
			if (!message) {
				// The steps that were streaming live belong to this answer, so
				// they move into it rather than being dropped and re-appearing
				// once the message is reloaded.
				message = {
					id: messageId,
					role: "assistant",
					content: "",
					created_at: frappe.datetime.now_datetime(),
					metadata: {},
				};
				if (this.state.steps.length) {
					message.metadata.tool_calls = this.state.steps.map((step) => ({ ...step }));
					this.state.steps = [];
				}
				this.state.messages.push(message);
			}

			message.content += chunk;
			this.pendingStreamMessageId = messageId;
			this.renderMessages();
			this.scrollToBottom();
		}

		async startNewConversation() {
			const response = await frappe.call({
				method: "ask_alyf.api.start_new_conversation",
				type: "POST",
			});
			this.setActiveTab("chat");
			await this.applyConversation(response.message);
			this.setModeToAskDefault();
			this.setLoading(false);
			this.refreshConversationList();
			this.setStatus("");
		}

		isFrontendAction(operation) {
			return operation?.kind === "frontend_action";
		}

		operationRequiresConfirmation(operation) {
			if (!operation || typeof operation !== "object") {
				return false;
			}
			if (!Object.prototype.hasOwnProperty.call(operation, "requires_confirmation")) {
				return true;
			}
			return Boolean(operation.requires_confirmation);
		}

		getPendingOperationSummary(operation) {
			if (!operation) {
				return "";
			}
			return operation.summary || operation.tool || __("Pending operation");
		}

		getPendingOperationSummaryHtml(operation) {
			const summaryHtml = this.renderInlineMarkdown(this.getPendingOperationSummary(operation));
			const previewHtml = this.getPendingOperationPreviewHtml(operation);
			return previewHtml ? `${summaryHtml}${previewHtml}` : summaryHtml;
		}

		getPendingOperationPreviewHtml(operation) {
			if (operation?.tool === "batch_insert") {
				return this.getBatchInsertPreviewHtml(operation);
			}
			if (["insert", "save", "set_value"].includes(operation?.tool)) {
				return this.getSingleOperationPreviewHtml(operation);
			}
			return "";
		}

		getSingleOperationPreviewHtml(operation) {
			const payload = operation?.payload || {};
			let fields = {};

			if (["insert", "save"].includes(operation.tool)) {
				fields = payload.values || {};
			} else if (operation.tool === "set_value") {
				if (payload.fieldname) {
					fields[payload.fieldname] = payload.value;
				}
			}

			const keys = Object.keys(fields);
			if (!keys.length) {
				return "";
			}

			const listHtml = keys
				.map((key) => {
					const label = this.getBatchInsertPreviewColumnLabel(operation, key);
					const value = this.formatBatchInsertPreviewValue(fields[key], payload.doctype, key);
					return `<li><em>${this.escapeHtml(label)}</em>: ${value}</li>`;
				})
				.join("");

			return `
				<details class="ask_alyf-proposal-details" open>
					<summary>${this.escapeHtml(__("Changes preview"))}</summary>
					<ul class="ask_alyf-proposal-list">
						${listHtml}
					</ul>
				</details>
			`;
		}

		getBatchInsertPreviewHtml(operation) {
			const records = Array.isArray(operation?.payload?.records) ? operation.payload.records : [];
			if (!records.length) {
				return "";
			}

			const columns = this.getBatchInsertPreviewColumns(records);
			if (!columns.length) {
				return "";
			}

			const headerHtml = columns
				.map((column) => {
					const label = this.getBatchInsertPreviewColumnLabel(operation, column);
					return `<th scope="col">${this.escapeHtml(label)}</th>`;
				})
				.join("");
			const bodyHtml = records
				.map((record, index) => {
					const safeRecord =
						record && typeof record === "object" && !Array.isArray(record) ? record : {};
					const cells = columns
						.map((column) => {
							const value = this.formatBatchInsertPreviewValue(
								safeRecord[column],
								operation?.payload?.doctype,
								column
							);
							return `<td>${value}</td>`;
						})
						.join("");
					return `<tr><th scope="row" class="ask_alyf-proposal-row-number">${
						index + 1
					}</th>${cells}</tr>`;
				})
				.join("");

			return `
				<details class="ask_alyf-proposal-details">
					<summary>${this.escapeHtml(__("Records to create ({0})", [records.length]))}</summary>
					<div class="ask_alyf-proposal-table-wrap">
						<table class="ask_alyf-proposal-table">
							<thead>
								<tr>
									<th scope="col" class="ask_alyf-proposal-row-number">#</th>
									${headerHtml}
								</tr>
							</thead>
							<tbody>${bodyHtml}</tbody>
						</table>
					</div>
				</details>
			`;
		}

		getBatchInsertPreviewColumns(records) {
			const columns = [];
			const seen = new Set();
			for (const record of records) {
				if (!record || typeof record !== "object" || Array.isArray(record)) {
					continue;
				}
				for (const key of Object.keys(record)) {
					if (seen.has(key)) {
						continue;
					}
					seen.add(key);
					columns.push(key);
				}
			}
			return columns;
		}

		getBatchInsertPreviewColumnLabel(operation, column) {
			const doctype = operation?.payload?.doctype;
			if (doctype && column) {
				const df = frappe.meta.get_docfield(doctype, column);
				if (df && df.label) {
					return __(df.label);
				}
			}
			return column;
		}

		formatBatchInsertPreviewValue(value, doctype, fieldname) {
			if (value === null || value === undefined) {
				return "";
			}
			if (doctype && fieldname) {
				const df = frappe.meta.get_docfield(doctype, fieldname);
				if (df) {
					if (df.fieldtype === "Table" && Array.isArray(value)) {
						return this.escapeHtml(__("{0} rows", [value.length]));
					}
					return frappe.format(value, df);
				}
			}
			if (Array.isArray(value)) {
				return this.escapeHtml(__("{0} items", [value.length]));
			}
			if (typeof value === "string") {
				return this.escapeHtml(value);
			}
			if (typeof value === "number" || typeof value === "bigint") {
				return frappe.format(value, { fieldtype: "Float" });
			}
			if (typeof value === "boolean") {
				return frappe.format(value, { fieldtype: "Check" });
			}
			try {
				return this.escapeHtml(JSON.stringify(value));
			} catch (error) {
				return this.escapeHtml(String(value));
			}
		}

		renderInlineMarkdown(value) {
			const container = document.createElement("div");
			container.innerHTML = frappe.markdown((value || "").toString());
			if (container.childElementCount === 1 && container.firstElementChild?.tagName === "P") {
				return container.firstElementChild.innerHTML;
			}
			return container.innerHTML;
		}

		getMatchingForm(payload = {}) {
			if (!window.cur_frm?.doc) {
				throw new Error(__("No form is currently open."));
			}

			const expectedDoctype = payload.doctype;
			if (expectedDoctype && expectedDoctype !== cur_frm.doc.doctype) {
				throw new Error(
					__("Current form is {0}, expected {1}.", [__(cur_frm.doc.doctype), __(expectedDoctype)])
				);
			}

			const expectedDocname = payload.docname;
			if (expectedDocname && expectedDocname !== cur_frm.doc.name) {
				throw new Error(
					__("Current document is {0}, expected {1}.", [cur_frm.doc.name, expectedDocname])
				);
			}

			return cur_frm;
		}

		async dispatchFrontendAction(tool, payload = {}) {
			if (tool === "set_route") {
				await frappe.set_route(...(payload.route || []));
				return { route: payload.route || [] };
			}

			if (tool === "new_doc") {
				frappe.new_doc(payload.doctype, payload.route_options || null);
				return { doctype: payload.doctype };
			}

			if (tool === "scroll_to_field") {
				const frm = this.getMatchingForm(payload);
				if (typeof frm.scroll_to_field !== "function") {
					throw new Error(__("Scrolling to fields is not available on this form."));
				}
				frm.scroll_to_field(payload.fieldname);
				return { fieldname: payload.fieldname };
			}

			if (tool === "frm_set_value") {
				const frm = this.getMatchingForm(payload);
				if (!frm.fields_dict?.[payload.fieldname]) {
					throw new Error(__("Field {0} does not exist on this form.", [payload.fieldname]));
				}
				await frm.set_value(payload.fieldname, payload.value);
				return { fieldname: payload.fieldname };
			}

			if (tool === "frm_add_child") {
				const frm = this.getMatchingForm(payload);
				if (!frm.fields_dict?.[payload.fieldname]) {
					throw new Error(__("Field {0} does not exist on this form.", [payload.fieldname]));
				}
				const row = frm.add_child(payload.fieldname, payload.values || {});
				frm.refresh_field(payload.fieldname);
				return { fieldname: payload.fieldname, row_name: row?.name || "" };
			}

			if (tool === "show_chart") {
				return { tool: "show_chart" };
			}

			throw new Error(__("Unsupported frontend action: {0}", [tool]));
		}

		async reportFrontendActionResult(operation, status, result = null, errorMessage = "") {
			if (!operation?.call_id || !this.state.conversation?.name) {
				return;
			}

			const args = {
				conversation: this.state.conversation.name,
				call_id: operation.call_id,
				status,
				mode: this.state.mode,
			};
			if (result && typeof result === "object") {
				args.result = result;
			}
			if (errorMessage) {
				args.error = errorMessage;
			}

			const response = await frappe.call({
				method: "ask_alyf.api.frontend_action_result",
				type: "POST",
				args,
			});
			if (response.message?.conversation) {
				await this.applyConversation(response.message.conversation);
				this.maybeAutoExecuteFrontendActions();
			}
			this.refreshConversationList();
		}

		async executeFrontendAction(operation) {
			if (!this.isFrontendAction(operation)) {
				return false;
			}
			if (!operation.call_id) {
				frappe.msgprint(__("Frontend action is missing a call ID."));
				return false;
			}

			this.handledFrontendCallIds.add(operation.call_id);
			try {
				const actionResult = await this.dispatchFrontendAction(
					operation.tool,
					operation.payload || {}
				);
				await this.reportFrontendActionResult(operation, "success", actionResult);
				return true;
			} catch (error) {
				const errorMessage = error?.message || __("Failed to execute frontend action.");
				try {
					await this.reportFrontendActionResult(operation, "failed", null, errorMessage);
				} catch {
					// Keep the widget responsive even if status reporting fails.
				}
				frappe.msgprint(errorMessage);
				return false;
			}
		}

		async maybeAutoExecuteFrontendActions() {
			for (const operation of [...this.state.pendingOperations]) {
				if (!this.isFrontendAction(operation)) {
					continue;
				}
				if (this.operationRequiresConfirmation(operation)) {
					continue;
				}
				if (!operation.call_id || this.handledFrontendCallIds.has(operation.call_id)) {
					continue;
				}
				await this.executeFrontendAction(operation);
			}
		}

		async confirmPendingOperation(operation) {
			if (!operation || !this.state.conversation?.name) {
				return;
			}

			this.removePendingOperation(operation);
			this.renderMessages();
			this.setLoading(true);
			this.setStatus(
				this.isFrontendAction(operation) ? __("Applying action...") : __("Confirming action...")
			);
			try {
				if (this.isFrontendAction(operation)) {
					const actionCompleted = await this.executeFrontendAction(operation);
					if (!actionCompleted) {
						this.restorePendingOperation(operation);
						this.renderMessages();
					}
					return;
				}

				const response = await frappe.call({
					method: "ask_alyf.api.confirm_pending_operation",
					type: "POST",
					args: {
						conversation: this.state.conversation.name,
						call_id: operation.call_id,
						mode: this.state.mode,
					},
				});
				await this.applyConversation(response.message.conversation);
				this.refreshConversationList();
				this.maybeAutoExecuteFrontendActions();
			} catch (error) {
				this.restorePendingOperation(operation);
				this.renderMessages();
				frappe.msgprint(error.message || __("Failed to confirm pending operation."));
			} finally {
				this.setLoading(false);
				this.setStatus("");
			}
		}

		async rejectPendingOperation(operation) {
			if (!operation || !this.state.conversation?.name) {
				return;
			}

			this.removePendingOperation(operation);
			this.renderMessages();
			try {
				if (this.isFrontendAction(operation)) {
					if (operation.call_id) {
						this.handledFrontendCallIds.add(operation.call_id);
					}
					await this.reportFrontendActionResult(operation, "rejected");
					return;
				}

				const response = await frappe.call({
					method: "ask_alyf.api.reject_pending_operation",
					type: "POST",
					args: {
						conversation: this.state.conversation.name,
						call_id: operation.call_id,
						mode: this.state.mode,
					},
				});
				await this.applyConversation(response.message.conversation);
				this.refreshConversationList();
				this.maybeAutoExecuteFrontendActions();
			} catch (error) {
				this.restorePendingOperation(operation);
				this.renderMessages();
				frappe.msgprint(error.message || __("Failed to reject pending operation."));
			}
		}

		async confirmAllPendingOperations() {
			const operations = [...(this.state.pendingOperations || [])].filter((op) =>
				this.operationRequiresConfirmation(op)
			);
			for (const operation of operations) {
				await this.confirmPendingOperation(operation);
			}
		}

		async rejectAllPendingOperations() {
			const operations = [...(this.state.pendingOperations || [])].filter((op) =>
				this.operationRequiresConfirmation(op)
			);
			for (const operation of operations) {
				await this.rejectPendingOperation(operation);
			}
		}

		removePendingOperation(operation) {
			this.state.pendingOperations = this.state.pendingOperations.filter(
				(op) => op.call_id !== operation.call_id
			);
		}

		restorePendingOperation(operation) {
			if (!this.state.pendingOperations.some((op) => op.call_id === operation.call_id)) {
				this.state.pendingOperations.push(operation);
			}
		}

		normalizePendingOperations(value) {
			return Array.isArray(value) ? value : [];
		}

		renderMessages() {
			const previousMessageKeys = this.renderedMessageKeys;
			const nextMessageKeys = new Set();
			let anchor = this.messagesEl.firstChild;

			this.state.messages.forEach((message, index) => {
				const { entry, messageKey } = this.syncMessageElement(message, index, previousMessageKeys);
				nextMessageKeys.add(messageKey);
				if (entry.wrapper === anchor) {
					anchor = anchor.nextSibling;
				} else {
					this.messagesEl.insertBefore(entry.wrapper, anchor);
				}
			});

			for (const [messageKey, entry] of this.messageEntries) {
				if (nextMessageKeys.has(messageKey)) {
					continue;
				}
				this.resetMessageCharts(entry, messageKey);
				entry.wrapper.remove();
				this.messageEntries.delete(messageKey);
			}

			this.renderedMessageKeys = nextMessageKeys;
			this.renderLiveSteps();
			this.renderStatusMessage();
			this.renderPendingOperation();
			this.renderSuggestedPrompts();
			this.scrollToBottom();

			requestAnimationFrame(() => {
				requestAnimationFrame(() => this.flushDeferredChartPaints());
			});
		}

		playPanelEnterAnimation() {
			if (!this.panel) {
				return;
			}
			this.panel.classList.remove("ask_alyf-panel-pop");
			// Trigger reflow so the entrance animation restarts each open.
			void this.panel.offsetWidth;
			this.panel.classList.add("ask_alyf-panel-pop");
		}

		getMessageRenderKey(message, index) {
			if (message?.id) {
				return `id:${message.id}`;
			}
			return `fallback:${message?.role || "message"}:${index}`;
		}

		cacheRenderedMessageKeys(messages = []) {
			this.renderedMessageKeys = new Set(
				messages.map((message, index) => this.getMessageRenderKey(message, index))
			);
		}

		scrollToBottom() {
			this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
		}

		mountFrappeChartsForMessage(entry, message, messageKey) {
			const wrapper = entry.wrapper;
			const meta = message?.metadata || {};
			const charts = meta.frappe_charts;
			if (!Array.isArray(charts) || !charts.length) {
				return;
			}
			if (typeof frappe.Chart !== "function") {
				return;
			}

			const jobs = [];
			for (const rawOptions of charts) {
				if (!rawOptions || typeof rawOptions !== "object") {
					jobs.push({ ok: false });
					continue;
				}
				let cloned;
				try {
					cloned = JSON.parse(JSON.stringify(rawOptions));
				} catch {
					jobs.push({ ok: false });
					continue;
				}
				jobs.push(normalizeStoredFrappeChartOptions(cloned));
			}
			if (!jobs.length) {
				return;
			}

			const holder = document.createElement("div");
			holder.className = "ask_alyf-message-charts";
			jobs.forEach((job, index) => {
				const shell = document.createElement("div");
				shell.className = "ask_alyf-frappe-chart-shell";
				const actions = document.createElement("div");
				actions.className = "ask_alyf-frappe-chart-actions ask_alyf-hidden";
				const downloadButton = document.createElement("button");
				const downloadLabel = __("Download chart as SVG");
				downloadButton.className = "ask_alyf-frappe-chart-download ask_alyf-icon-button";
				downloadButton.type = "button";
				downloadButton.title = downloadLabel;
				downloadButton.setAttribute("aria-label", downloadLabel);
				downloadButton.disabled = true;
				downloadButton.innerHTML =
					typeof frappe.utils?.icon === "function"
						? frappe.utils.icon("es-line-download", "xs")
						: "SVG";
				downloadButton.addEventListener("click", () => {
					const chart = this.getTrackedFrappeChart(messageKey, index);
					if (!chart || typeof chart.export !== "function") {
						return;
					}
					try {
						chart.export();
					} catch {
						frappe.msgprint(__("Could not download chart."));
					}
				});
				actions.appendChild(downloadButton);
				const mount = document.createElement("div");
				mount.className = "ask_alyf-frappe-chart-mount";
				shell.appendChild(actions);
				shell.appendChild(mount);
				holder.appendChild(shell);
				job.actionsEl = actions;
				job.downloadButtonEl = downloadButton;
			});
			wrapper.appendChild(holder);
			entry.chartHolder = holder;

			const mounts = Array.from(holder.querySelectorAll(".ask_alyf-frappe-chart-mount"));
			const paintVersion = (entry.chartPaintVersion || 0) + 1;
			entry.chartPaintVersion = paintVersion;
			const layoutCharts = ({ create = false } = {}) => {
				if (entry.chartPaintVersion !== paintVersion || !holder.isConnected || !wrapper.isConnected) {
					return false;
				}
				let contentWidth = holder.clientWidth;
				if (!Number.isFinite(contentWidth) || contentWidth < 48) {
					contentWidth = this.messagesEl?.clientWidth || this.panel?.clientWidth || 0;
				}
				if (!Number.isFinite(contentWidth) || contentWidth < 48) {
					return false;
				}
				const widthPx = Math.max(200, Math.floor(contentWidth - 8));
				jobs.forEach((job, index) => {
					const mount = mounts[index];
					if (!mount || !mount.isConnected) {
						return;
					}
					if (!job.ok) {
						mount.classList.add("ask_alyf-frappe-chart-error");
						mount.textContent = __("Invalid chart data.");
						return;
					}
					const preferredHeight = Number(job.options.height);
					const heightPx = this.getResponsiveFrappeChartHeight(
						preferredHeight,
						widthPx,
						jobs.length
					);
					if (create) {
						mount.style.width = `${widthPx}px`;
						mount.style.maxWidth = "100%";
						mount.dataset.askAlyfWidth = String(widthPx);
						mount.dataset.askAlyfHeight = String(heightPx);
						try {
							const chart = new frappe.Chart(mount, {
								...job.options,
								height: heightPx,
							});
							wrapAskAlyfFrappeChart(chart);
							if (typeof chart.destroy === "function") {
								chart.destroy();
							}
							this.setTrackedFrappeChart(messageKey, index, chart);
							job.actionsEl?.classList.remove("ask_alyf-hidden");
							if (job.downloadButtonEl) {
								job.downloadButtonEl.disabled = false;
							}
						} catch {
							mount.classList.add("ask_alyf-frappe-chart-error");
							mount.textContent = __("Could not render chart.");
						}
						return;
					}
					this.applyMountedFrappeChartLayout(
						this.getTrackedFrappeChart(messageKey, index),
						mount,
						widthPx,
						heightPx
					);
				});
				return true;
			};

			const scheduleInitialPaint = () => {
				requestAnimationFrame(() => {
					requestAnimationFrame(() => {
						if (
							entry.chartPaintVersion !== paintVersion ||
							!holder.isConnected ||
							!wrapper.isConnected
						) {
							return;
						}
						if (!this.isChatAreaReady()) {
							this.deferredChartPaints.push(scheduleInitialPaint);
							return;
						}
						if (!layoutCharts({ create: true })) {
							this.deferredChartPaints.push(scheduleInitialPaint);
							return;
						}
						if (!entry.chartResizeObserver && typeof ResizeObserver === "function") {
							entry.chartResizeObserver = new ResizeObserver(() => {
								if (entry.chartResizeFrame) {
									cancelAnimationFrame(entry.chartResizeFrame);
								}
								entry.chartResizeFrame = requestAnimationFrame(() => {
									entry.chartResizeFrame = 0;
									if (
										entry.chartPaintVersion !== paintVersion ||
										!holder.isConnected ||
										!wrapper.isConnected ||
										!this.isChatAreaReady()
									) {
										return;
									}
									layoutCharts();
								});
							});
							if (this.panel) {
								entry.chartResizeObserver.observe(this.panel);
							}
						}
					});
				});
			};
			scheduleInitialPaint();
		}

		startVoiceInput() {
			const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
			if (!Recognition) {
				frappe.msgprint(__("Your browser does not support voice input."));
				return;
			}

			if (this.voiceRecognition) {
				this.voiceRecognition.stop();
				return;
			}

			const speechLanguage = this.getPreferredSpeechLanguage();
			this.updateVoiceInputHint(speechLanguage);

			const recognition = new Recognition();
			this.voiceRecognition = recognition;
			recognition.lang = speechLanguage;
			recognition.interimResults = false;
			recognition.maxAlternatives = 1;
			recognition.onstart = () => {
				this.setVoiceInputListening(true);
			};
			recognition.onresult = (event) => {
				const transcript = event.results?.[0]?.[0]?.transcript;
				if (transcript) {
					this.inputEl.value = transcript;
					this.autoResizeInput();
				}
			};
			recognition.onerror = () => {
				this.setVoiceInputListening(false);
			};
			recognition.onend = () => {
				this.setVoiceInputListening(false);
			};

			try {
				recognition.start();
			} catch (error) {
				this.setVoiceInputListening(false);
			}
		}

		setVoiceInputListening(isListening) {
			if (!isListening) {
				this.voiceRecognition = null;
			}

			if (!this.micEl) {
				return;
			}

			this.micEl.classList.toggle("is-listening", isListening);
			this.micEl.setAttribute("aria-pressed", isListening ? "true" : "false");
		}

		updateVoiceInputHint(languageCode = this.getPreferredSpeechLanguage()) {
			if (!this.micEl) {
				return;
			}

			const tooltip = __("Voice input language: {0}", [languageCode]);
			this.micEl.title = tooltip;
			this.micEl.setAttribute("aria-label", tooltip);
		}

		getPreferredSpeechLanguage() {
			const langCandidate =
				frappe?.boot?.lang || document.documentElement.lang || navigator.language || "en-US";
			return this.normalizeSpeechLanguage(langCandidate);
		}

		normalizeSpeechLanguage(lang) {
			const normalized = (lang || "").toString().replace("_", "-").trim();
			if (!normalized) {
				return "en-US";
			}

			const key = normalized.toLowerCase();
			const languageMap = {
				de: "de-DE",
				en: "en-US",
				es: "es-ES",
				fr: "fr-FR",
				it: "it-IT",
				ja: "ja-JP",
				ko: "ko-KR",
				nl: "nl-NL",
				pt: "pt-PT",
				zh: "zh-CN",
				"zh-hans": "zh-CN",
				"zh-hant": "zh-TW",
			};

			if (languageMap[key]) {
				return languageMap[key];
			}

			const [base, region] = normalized.split("-");
			if (base && region) {
				return `${base.toLowerCase()}-${region.toUpperCase()}`;
			}

			if (normalized.length === 2) {
				return `${normalized.toLowerCase()}-${normalized.toUpperCase()}`;
			}

			return "en-US";
		}

		escapeHtml(value) {
			return frappe.utils.escape_html((value ?? "").toString());
		}
	}

	window.ask_alyfWidget = new ask_alyfWidget();
	$(function () {
		window.ask_alyfWidget.init();
	});
})();

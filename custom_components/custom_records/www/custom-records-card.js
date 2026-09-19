/**
 * Custom Records - Lovelace card.
 *
 * A lightweight custom card (no build step, no external dependencies) that
 * lists and adds records for a single configured record type via the
 * custom_records WebSocket API.
 *
 * Card config:
 *   type: custom:custom-records-card
 *   record_type: blood_pressure   # required - the record type id
 *   title: Blood Pressure         # optional - defaults to the record type's name
 *   last: 20                      # optional - a count (max rows, default 20) OR a duration like
 *                                  # '30m', '12h', '3d', '2w' (minutes/hours/days/weeks - show
 *                                  # everything from that far back, still capped server-side)
 *   show_add_record: true         # optional - show the "Add record" button/dialog, default true
 *   show_actions: true            # optional - show a per-row actions menu (currently just
 *     Delete, behind a confirmation) with a 3-dot trigger, default true
 *   columns:                      # optional - table-only column allow-list + order (add-record
 *     - systolic                  # form is unaffected and always shows every field); omit for
 *     - diastolic                 # today's default behavior (every field, record type's order)
 *   image_header_field: timestamp # optional - timestamp (default) or a non-image field key
 *   image_overlay_fields:         # optional - ordered non-image fields shown over the image
 *     - field: systolic
 *       label: Systolic pressure  # optional - defaults to the field label
 *
 * A visual editor (CustomRecordsCardEditor, below) is also registered via
 * getConfigElement(), so all of the above can be configured through the
 * dashboard's "Visual editor" instead of raw YAML.
 */

const DEFAULT_LAST_COUNT = 20;
const LAST_DURATION_RE = /^(\d+)(m|h|d|w)$/i;
const DURATION_UNIT_MS = { m: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000 };

// Client-side mirror of media_store.py's ALLOWED_IMAGE_EXTENSIONS - purely an
// early-feedback convenience (the server enforces this authoritatively too).
const IMAGE_UPLOAD_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"]);
const IMAGE_UPLOAD_ACCEPT = Array.from(IMAGE_UPLOAD_EXTENSIONS).join(",");
// Friendlier client-side cap in addition to HA's own file_upload 100MB limit.
const MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024;

// Fired on hass.bus (see const.py's EVENT_RECORDS_UPDATED) whenever a record
// type's data or definition changes, from ANY source (this card, another
// card/tab, an automation's service call, the purge job, etc.) - lets an
// already-open card refetch instead of silently going stale.
const EVENT_RECORDS_UPDATED = "custom_records_updated";
// Coalesces bursts of update events (e.g. many rows added in quick
// succession) into a single refetch.
const UPDATE_DEBOUNCE_MS = 300;

/**
 * Parse the card's `last` config value into either a row count or a
 * duration (in ms), defaulting to DEFAULT_LAST_COUNT rows when unset.
 * Returns null if the value is neither a positive number nor a recognized
 * duration string (e.g. '2w'), so callers can reject it as a config error.
 */
function parseLast(value) {
    if (value === undefined) {
        return { type: "count", value: DEFAULT_LAST_COUNT };
    }
    if (typeof value === "number") {
        return Number.isInteger(value) && value >= 1 ? { type: "count", value } : null;
    }
    if (typeof value === "string") {
        const match = LAST_DURATION_RE.exec(value.trim());
        if (!match) {
            return null;
        }
        const amount = Number(match[1]);
        return amount >= 1
            ? { type: "duration", ms: amount * DURATION_UNIT_MS[match[2].toLowerCase()] }
            : null;
    }
    return null;
}

function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
    })[ch]);
}

/**
 * Whether times should be rendered with an AM/PM 12-hour clock, per the
 * user's HA profile "time format" setting (`hass.locale.time_format`, one
 * of "language"/"system"/"12"/"24"). Mirrors HA frontend's own
 * `useAmPm()` heuristic for the "language"/"system" cases (see
 * home-assistant/frontend's `src/common/datetime/use_am_pm.ts`).
 */
function useAmPm(hass) {
    const timeFormat = hass?.locale?.time_format;
    if (timeFormat === "12") {
        return true;
    }
    if (timeFormat === "24") {
        return false;
    }
    const testLanguage = timeFormat === "language" ? hass?.locale?.language : undefined;
    return new Date("January 1, 2023 22:00:00").toLocaleString(testLanguage).includes("10");
}

/**
 * Formats a numeric date (e.g. "9/8/2021") honoring the user's HA profile
 * "date format" setting (`hass.locale.date_format`: "language"/"system"/
 * "DMY"/"MDY"/"YMD"). Reordering the day/month/year parts for an explicit
 * DMY/MDY/YMD override can't be done with Intl options alone, so - like HA
 * frontend's own `formatDateNumeric()` (`src/common/datetime/format_date.ts`)
 * - this reassembles the Intl-produced parts in the requested order.
 */
function formatDateNumeric(hass, date) {
    const locale = hass?.locale;
    const dateFormat = locale?.date_format;
    const localeString = dateFormat === "system" ? undefined : locale?.language;
    const formatter = new Intl.DateTimeFormat(localeString, {
        year: "numeric",
        month: "numeric",
        day: "numeric",
    });
    if (!dateFormat || dateFormat === "language" || dateFormat === "system") {
        return formatter.format(date);
    }
    const parts = formatter.formatToParts(date);
    const literal = parts.find((p) => p.type === "literal")?.value ?? "";
    const day = parts.find((p) => p.type === "day")?.value ?? "";
    const month = parts.find((p) => p.type === "month")?.value ?? "";
    const year = parts.find((p) => p.type === "year")?.value ?? "";
    const lastPart = parts[parts.length - 1];
    let lastLiteral = lastPart?.type === "literal" ? lastPart.value : "";
    if (locale?.language === "bg" && dateFormat === "YMD") {
        // Matches a special case in HA frontend's formatDateNumeric().
        lastLiteral = "";
    }
    const formats = {
        DMY: `${day}${literal}${month}${literal}${year}${lastLiteral}`,
        MDY: `${month}${literal}${day}${literal}${year}${lastLiteral}`,
        YMD: `${year}${literal}${month}${literal}${day}${lastLiteral}`,
    };
    return formats[dateFormat] ?? formatter.format(date);
}

/** Formats a time-of-day with seconds (e.g. "8:23:15 PM" / "20:23:15"),
 * honoring the user's HA profile "time format" setting - see useAmPm(). */
function formatTimeWithSeconds(hass, date) {
    const amPm = useAmPm(hass);
    return new Intl.DateTimeFormat(hass?.locale?.language, {
        hour: amPm ? "numeric" : "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: amPm ? "h12" : "h23",
    }).format(date);
}

/**
 * Formats a Date per the user's HA profile locale settings (language, date
 * format, 12h/24h time format) instead of the browser's default locale, so
 * the card matches the rest of the HA UI (e.g. dashboards/history). Mirrors
 * HA frontend's own `formatDateTimeNumeric()`.
 */
function formatDateTime(hass, date) {
    try {
        return `${formatDateNumeric(hass, date)}, ${formatTimeWithSeconds(hass, date)}`;
    } catch {
        return date.toLocaleString();
    }
}

class CustomRecordsCard extends HTMLElement {
    constructor() {
        super();
        this.attachShadow({ mode: "open" });
        this._config = null;
        this._hass = null;
        this._recordType = null;
        this._records = [];
        this._formValues = {};
        this._loading = false;
        this._submitting = false;
        this._error = null;
        this._imageUrls = {};
        // Pending browser-side image uploads for the currently-open add-record
        // dialog, keyed by field key -> { file, previewUrl }. Populated by
        // _handleImageFileChange(), consumed (uploaded to HA) at submit time
        // by _handleSubmit() - see the "field-image" upload/path toggle in
        // _renderFieldInput()/_openAddDialog(). Always reset (revoking any
        // previewUrl) alongside _formValues, in _closeDialog() and
        // _openAddDialog()'s init.
        this._imagePendingUploads = {};
        this._unsubscribeUpdates = null;
        this._subscribingToUpdates = false;
        this._updateDebounceTimer = null;
        // The currently-open add-record <ha-dialog>, appended to document.body
        // (not this.shadowRoot) so a Lovelace masonry/grid dashboard's layout
        // can't visually clip it - see _openAddDialog(). null when closed.
        this._dialogEl = null;
        // The currently-open confirmation <ha-dialog> (e.g. "delete this
        // record?"), same document.body-append technique - see
        // _openConfirmDialog(). null when closed. Deliberately a separate
        // property from _dialogEl since the two dialogs are unrelated and
        // could in principle both exist momentarily during teardown.
        this._confirmDialogEl = null;
        // The currently-open enlarged-image <ha-dialog> (table thumbnail
        // click), same document.body-append technique - see
        // _openImageDialog(). null when closed.
        this._imageDialogEl = null;
        // Tri-state: null = not yet validated against the record type's real
        // fields (unknown `record_type` / unknown `columns` keys), true =
        // validated and valid, false = validated and invalid. Reset to null
        // in setConfig() so _validateConfig() runs exactly once per config -
        // see that method for why validation lives there, not in _loadData().
        this._configValid = null;
        this._configGeneration = 0;
        this._loadGeneration = 0;
    }

    setConfig(config) {
        if (!config || !config.record_type) {
            throw new Error("custom-records-card: 'record_type' is required in the card config");
        }
        if (config.last !== undefined && !parseLast(config.last)) {
            throw new Error(
                "custom-records-card: 'last' must be a positive integer (e.g. 20) or a duration like '30m', '12h', '3d', '2w'",
            );
        }
        if (config.filter !== undefined && !Array.isArray(config.filter)) {
            throw new Error(
                "custom-records-card: 'filter' must be a list of single-key field maps, e.g. [{name: Max}]",
            );
        }
        if (
            config.columns !== undefined &&
            (!Array.isArray(config.columns) || !config.columns.every((c) => typeof c === "string"))
        ) {
            throw new Error(
                "custom-records-card: 'columns' must be a list of field key strings, e.g. [systolic, diastolic]",
            );
        }
        if (config.image_header_field !== undefined && typeof config.image_header_field !== "string") {
            throw new Error("custom-records-card: 'image_header_field' must be a field key string");
        }
        if (
            config.image_overlay_fields !== undefined &&
            (!Array.isArray(config.image_overlay_fields) || !config.image_overlay_fields.every(
                (entry) => entry && typeof entry === "object" && !Array.isArray(entry) && typeof entry.field === "string" &&
                    (entry.label === undefined || typeof entry.label === "string"),
            ))
        ) {
            throw new Error("custom-records-card: 'image_overlay_fields' must be a list of {field, label?} objects");
        }
        this._configGeneration += 1;
        this._loadGeneration += 1;
        this._closeDialog();
        this._closeConfirmDialog();
        this._closeImageDialog();
        this._config = config;
        this._recordType = null;
        this._records = [];
        this._formValues = {};
        this._loading = false;
        this._submitting = false;
        this._error = null;
        this._configValid = null;
        this._render();
        // No-op if `hass` isn't connected yet (validated instead from the
        // `hass` setter below, once it is) - see _validateConfig().
        this._validateConfig();
    }

    set hass(hass) {
        const firstRun = !this._hass;
        this._hass = hass;
        if (firstRun) {
            // setConfig() always runs before this setter (framework
            // guarantee), so this is the earliest point `hass` is available
            // for a freshly-mounted card - validate now if setConfig()'s own
            // call above couldn't (because `hass` wasn't connected yet).
            this._validateConfig();
        }
        this._subscribeToUpdates();
    }

    connectedCallback() {
        // Re-establish the subscription if the card is re-attached to the DOM
        // (e.g. after a dashboard view switch) - disconnectedCallback tears it
        // down below to avoid leaking it while detached.
        this._subscribeToUpdates();
    }

    disconnectedCallback() {
        if (this._updateDebounceTimer) {
            clearTimeout(this._updateDebounceTimer);
            this._updateDebounceTimer = null;
        }
        if (this._unsubscribeUpdates) {
            this._unsubscribeUpdates();
            this._unsubscribeUpdates = null;
        }
        // Avoid leaving an orphaned dialog on document.body if the card is
        // removed from the DOM (e.g. dashboard view switch) while it's open.
        this._closeDialog();
        this._closeConfirmDialog();
        this._closeImageDialog();
    }

    async _subscribeToUpdates() {
        if (!this._hass || this._unsubscribeUpdates || this._subscribingToUpdates) {
            return;
        }
        // Set synchronously, before the `await` below, so a `hass` setter call
        // that re-enters this method while the first call is still pending
        // (hass is reassigned to every card on nearly every state change, so
        // this is a real, frequent race - not just theoretical) is blocked
        // immediately rather than racing to also call subscribeEvents().
        this._subscribingToUpdates = true;
        try {
            const unsubscribe = await this._hass.connection.subscribeEvents(
                (event) => this._onRecordsUpdated(event),
                EVENT_RECORDS_UPDATED,
            );
            if (this.isConnected) {
                this._unsubscribeUpdates = unsubscribe;
            } else {
                // Card was removed from the DOM while the subscribe call was
                // still in flight - don't leak the subscription.
                unsubscribe();
            }
        } catch {
            // Live refresh is a best-effort enhancement - if subscribing fails
            // (e.g. the connection isn't ready yet), the card still works, just
            // without live updates until the next manual reload.
        } finally {
            this._subscribingToUpdates = false;
        }
    }

    _onRecordsUpdated(event) {
        if (!this._config || event.data?.record_type !== this._config.record_type) {
            return;
        }
        if (this._updateDebounceTimer) {
            clearTimeout(this._updateDebounceTimer);
        }
        this._updateDebounceTimer = setTimeout(() => {
            this._updateDebounceTimer = null;
            this._loadData();
        }, UPDATE_DEBOUNCE_MS);
    }

    getCardSize() {
        return 3 + Math.ceil((this._records || []).length / 2);
    }

    /**
     * Validates the config against the record type's real fields (unknown
     * `record_type` / unknown `columns` keys) - needs live server data (the
     * record type's field list), so it's necessarily async, but it's driven
     * by config/hass lifecycle events (setConfig(), the hass setter's first
     * run), NOT by _loadData()'s per-refresh hot path, so it only ever runs
     * once per config (guarded by `_configValid`, reset to null in
     * setConfig()). `_loadData()` just checks the resulting `_configValid`
     * and stops immediately (waiting for the next config/hass change) if
     * it's anything other than `true`.
     */
    async _validateConfig() {
        if (!this._hass || !this._config || this._configValid !== null) {
            return;
        }
        const configGeneration = this._configGeneration;
        const config = this._config;
        const hass = this._hass;
        try {
            const typesResponse = await hass.callWS({
                type: "custom_records/list_record_types",
            });
            if (configGeneration !== this._configGeneration) {
                return;
            }
            const recordType = (typesResponse.record_types || []).find(
                (rt) => rt.id === config.record_type,
            );
            if (!recordType) {
                throw new Error(`Unknown record_type '${config.record_type}'`);
            }
            if (config.columns) {
                const validKeys = new Set(recordType.fields.map((f) => f.key));
                const unknownColumn = config.columns.find((key) => !validKeys.has(key));
                if (unknownColumn) {
                    throw new Error(`Unknown column field '${unknownColumn}'`);
                }
            }
            const imageFields = (config.image_overlay_fields || []).map((entry) => ({
                key: entry.field, option: "image_overlay_fields",
            }));
            if (config.image_header_field !== undefined && config.image_header_field !== "timestamp") {
                imageFields.push({ key: config.image_header_field, option: "image_header_field" });
            }
            for (const { key, option } of imageFields) {
                const field = recordType.fields.find((field) => field.key === key);
                if (!field) {
                    throw new Error(`Unknown ${option} field '${key}'`);
                }
                if (field.type === "image") {
                    throw new Error(`'${option}' field '${key}' must not be an image field`);
                }
            }
            this._configValid = true;
            await this._loadData();
        } catch (err) {
            if (configGeneration !== this._configGeneration) {
                return;
            }
            this._configValid = false;
            this._error = err.message || String(err);
            this._render();
        }
    }

    async _loadData() {
        if (!this._hass || !this._config || !this._configValid) {
            return;
        }
        const loadGeneration = ++this._loadGeneration;
        const configGeneration = this._configGeneration;
        const config = this._config;
        const hass = this._hass;
        const isCurrent = () =>
            loadGeneration === this._loadGeneration && configGeneration === this._configGeneration;
        this._loading = true;
        this._error = null;
        this._render();
        try {
            const typesResponse = await hass.callWS({
                type: "custom_records/list_record_types",
            });
            if (!isCurrent()) {
                return;
            }
            const recordType = (typesResponse.record_types || []).find(
                (rt) => rt.id === config.record_type,
            );
            if (!recordType) {
                throw new Error(`Unknown record_type '${config.record_type}'`);
            }

            const last = parseLast(config.last);
            const recordsResponse = await hass.callWS({
                type: "custom_records/list_records",
                record_type: config.record_type,
                ...(last.type === "count"
                    ? { limit: last.value }
                    : { start: new Date(Date.now() - last.ms).toISOString() }),
                ...(config.filter ? { filter: config.filter } : {}),
            });
            if (!isCurrent()) {
                return;
            }
            const records = (recordsResponse.records || []).sort(
                (a, b) => new Date(b.timestamp) - new Date(a.timestamp),
            );
            this._recordType = recordType;
            this._records = records;
            this._imageUrls = {};
        } catch (err) {
            if (!isCurrent()) {
                return;
            }
            this._error = err.message || String(err);
        } finally {
            if (isCurrent()) {
                this._loading = false;
                this._render();
            }
        }

        // Resolve image fields to signed, displayable URLs in the background
        // (via HA's media_source, which handles authentication) and
        // re-render once they're available, without blocking the initial
        // (text/number/etc.) render above.
        if (isCurrent()) {
            await this._resolveImages({ configGeneration, loadGeneration });
        }
    }

    async _resolveImages({ configGeneration, loadGeneration }) {
        const recordType = this._recordType;
        const records = this._records;
        const hass = this._hass;
        const isCurrent = () =>
            loadGeneration === this._loadGeneration && configGeneration === this._configGeneration;
        const imageFieldKeys = (recordType?.fields || [])
            .filter((f) => f.type === "image")
            .map((f) => f.key);
        if (!imageFieldKeys.length || !records.length) {
            return;
        }

        let anyResolved = false;
        await Promise.all(
            records.flatMap((record) =>
                imageFieldKeys.map(async (fieldKey) => {
                    const value = record[fieldKey];
                    const cacheKey = `${record.id}/${fieldKey}`;
                    if (!value || !value.media_source || this._imageUrls[cacheKey] !== undefined) {
                        return;
                    }
                    try {
                        const resolved = await hass.callWS({
                            type: "media_source/resolve_media",
                            media_content_id: value.media_source,
                        });
                        if (isCurrent()) {
                            this._imageUrls[cacheKey] = resolved.url;
                        }
                    } catch {
                        if (isCurrent()) {
                            this._imageUrls[cacheKey] = null;
                        }
                    }
                    anyResolved = anyResolved || isCurrent();
                }),
            ),
        );
        if (anyResolved && isCurrent()) {
            this._render();
        }
    }

    async _handleSubmit(event) {
        event.preventDefault();
        if (!this._recordType || !this._hass || !this._dialogEl || this._submitting) {
            return;
        }
        const dialog = this._dialogEl;
        const configGeneration = this._configGeneration;
        const config = this._config;
        const recordType = this._recordType;
        const hass = this._hass;
        const isCurrent = () =>
            dialog === this._dialogEl && configGeneration === this._configGeneration;
        this._submitting = true;
        this._setDialogSubmitting(true);
        const fields = {};
        for (const field of recordType.fields) {
            if (field.type === "image") {
                // Handled below - either an in-progress upload or a manual path,
                // depending on which mode the field's toggle is currently on.
                continue;
            }
            const value = this._formValues[field.key];
            if (value === undefined || value === "") {
                continue;
            }
            fields[field.key] = field.type === "number" ? Number(value) : value;
        }

        // Submission errors are shown INSIDE the still-open dialog (see
        // _setDialogError()), not via this._error/_render() - that mechanism
        // replaces the entire card body with just an error message (see the
        // `else if (this._error)` branch in _render()), which would be wrong
        // now that the form lives in an always-visible dialog on top of the
        // rest of the card, and would also lose any already-entered values in
        // OTHER fields when the dialog gets rebuilt from scratch.
        this._setDialogError(null);

        for (const field of recordType.fields) {
            if (field.type !== "image") {
                continue;
            }
            const pending = this._imagePendingUploads[field.key];
            if (pending) {
                // Upload happens HERE, at submit time (not on file selection) -
                // see _handleImageFileChange()/plan notes.
                try {
                    const formData = new FormData();
                    formData.append("file", pending.file);
                    const response = await hass.fetchWithAuth("/api/file_upload", {
                        method: "POST",
                        body: formData,
                    });
                    if (!isCurrent()) {
                        return;
                    }
                    if (!response.ok) {
                        throw new Error(`Upload failed (HTTP ${response.status})`);
                    }
                    const uploadResult = await response.json();
                    fields[field.key] = { file_id: uploadResult.file_id };
                } catch (err) {
                    if (!isCurrent()) {
                        return;
                    }
                    this._setDialogError(`${field.label}: ${err.message || String(err)}`);
                    this._submitting = false;
                    this._setDialogSubmitting(false);
                    return;
                }
                continue;
            }
            const path = this._formValues[field.key];
            if (!path) {
                continue;
            }
            try {
                const result = await hass.callWS({
                    type: "custom_records/validate_image_path",
                    path,
                });
                if (!isCurrent()) {
                    return;
                }
                if (!result.valid) {
                    this._setDialogError(`${field.label}: ${result.error}`);
                    this._submitting = false;
                    this._setDialogSubmitting(false);
                    return;
                }
                fields[field.key] = path;
            } catch (err) {
                if (!isCurrent()) {
                    return;
                }
                this._setDialogError(err.message || String(err));
                this._submitting = false;
                this._setDialogSubmitting(false);
                return;
            }
        }

        try {
            if (!isCurrent()) {
                return;
            }
            await hass.callWS({
                type: "custom_records/add_record",
                record_type: config.record_type,
                fields,
            });
            if (!isCurrent()) {
                return;
            }
            this._formValues = {};
            this._closeDialog();
            await this._loadData();
        } catch (err) {
            if (isCurrent()) {
                this._setDialogError(err.message || String(err));
            }
        } finally {
            if (isCurrent()) {
                this._submitting = false;
                this._setDialogSubmitting(false);
            }
        }
    }

    /**
     * Opens the add-record dialog, appended to document.body (NOT this
     * shadow root) so a Lovelace masonry/grid dashboard's layout can't
     * visually clip it - HA's own internal dialog manager does the same for
     * the same reason. No-op if already open or the record type hasn't
     * loaded yet.
     */
    _openAddDialog() {
        if (this._dialogEl || !this._recordType) {
            return;
        }
        this._formValues = Object.fromEntries(
            this._recordType.fields
                .filter((field) => field.default !== null && field.default !== undefined)
                .map((field) => [field.key, field.default]),
        );
        for (const field of this._recordType.fields) {
            if (field.type === "boolean" && this._formValues[field.key] === undefined) {
                this._formValues[field.key] = false;
            }
        }
        // No pending uploads should exist yet (only set/cleared while a dialog
        // is open - see _closeDialog()), but reset defensively.
        this._imagePendingUploads = {};
        const formFields = this._recordType.fields
            .map((field) => {
                const wrapperClass =
                    field.type === "boolean"
                        ? "field-boolean"
                        : field.type === "image"
                            ? "field-image"
                            : "field";
                return `<div class="${wrapperClass}">${this._renderFieldInput(field)}</div>`;
            })
            .join("");

        const dialog = document.createElement("ha-dialog");
        // NOTE: HA's current `ha-dialog` (wrapping `wa-dialog`) exposes the
        // header text via the `headerTitle` property/`header-title`
        // attribute - there is no `heading` property (that was the old
        // mwc-dialog-based API from older HA frontend versions).
        dialog.headerTitle = this._config.title || this._recordType.name;
        // Rendered into the dialog's default (light DOM) slot, so this
        // <style> block isn't shadow-DOM-encapsulated the way the rest of
        // the card is - mitigated with specific "cmc-" prefixed class names
        // rather than introducing a scoping mechanism.
        dialog.innerHTML = `
      <style>
        .cmc-add-form { display: grid; grid-template-columns: auto 1fr; column-gap: 8px; row-gap: 8px; align-items: center; min-width: 280px; }
        .cmc-add-form .field { display: contents; }
        .cmc-add-form .field-boolean { grid-column: 1 / -1; }
        .cmc-add-form .field-image { grid-column: 1 / -1; display: flex; flex-direction: column; gap: 4px; }
        .cmc-image-field { display: flex; flex-direction: column; gap: 6px; }
        .cmc-image-upload-block {
          display: flex;
          align-items: stretch;
          border: 1px solid var(--outline-color, var(--divider-color));
          border-radius: 8px;
          overflow: hidden;
          min-height: 40px;
        }
        .cmc-image-upload-block[hidden] { display: none; }
        .cmc-image-upload-block .cmc-image-file-input { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
        .cmc-image-upload-mode { display: contents; }
        .cmc-image-upload-mode[hidden] { display: none; }
        .cmc-image-choose-btn, .cmc-image-remove-btn, .cmc-image-mode-btn {
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 4px;
          margin: 0;
          padding: 0 12px;
          border: none;
          background: transparent;
          color: var(--primary-color);
          font: inherit;
          cursor: pointer;
        }
        .cmc-image-choose-btn:hover, .cmc-image-remove-btn:hover, .cmc-image-mode-btn:hover { background: var(--secondary-background-color); }
        .cmc-image-choose-btn:focus-visible, .cmc-image-remove-btn:focus-visible, .cmc-image-mode-btn:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
        .cmc-image-remove-btn, .cmc-image-mode-btn { color: var(--secondary-text-color); padding: 0 10px; }
        .cmc-image-remove-btn ha-icon, .cmc-image-mode-btn ha-icon { --mdc-icon-size: 20px; }
        .cmc-image-upload-divider { align-self: stretch; width: 1px; background: var(--divider-color); flex-shrink: 0; }
        .cmc-image-upload-filename-wrap { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; padding: 0 8px; }
        .cmc-image-upload-preview { width: 28px; height: 28px; object-fit: cover; border-radius: 4px; display: block; flex-shrink: 0; }
        .cmc-image-upload-preview[hidden] { display: none; }
        .cmc-image-upload-filename { color: var(--secondary-text-color); font-size: 0.9em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .cmc-image-path-input { flex: 1; min-width: 0; padding: 0 12px; border: none; background: transparent; font: inherit; color: inherit; outline: none; }
        .cmc-image-path-input:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
        .cmc-image-remove-wrap { display: flex; align-items: stretch; }
        .cmc-image-remove-wrap[hidden] { display: none; }
        .cmc-dialog-error { grid-column: 1 / -1; color: var(--error-color, red); margin: 0; }
        .cmc-native-submit { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
      </style>
      <form class="cmc-add-form">
        ${formFields}
                <p class="cmc-dialog-error" role="alert" aria-live="polite" hidden></p>
                <button class="cmc-native-submit" type="submit" tabindex="-1" aria-hidden="true">Submit</button>
      </form>
            <ha-dialog-footer slot="footer">
                <ha-button type="button" appearance="plain" slot="secondaryAction" class="cmc-cancel-btn">Cancel</ha-button>
                <ha-button type="button" appearance="filled" slot="primaryAction" class="cmc-submit-btn">Add record</ha-button>
            </ha-dialog-footer>
    `;

        // Escape key / backdrop click both fire "closed" natively (mwc-dialog
        // behavior) - just need to detach/clean up when that happens.
        dialog.addEventListener("closed", () => this._closeDialog());

        const form = dialog.querySelector("form");
        form.addEventListener("submit", (event) => this._handleSubmit(event));
        dialog.querySelector(".cmc-submit-btn").addEventListener("click", () => form.requestSubmit());
        form.querySelectorAll("[data-key]").forEach((input) => {
            const key = input.dataset.key;
            const isCheckbox = input.type === "checkbox";
            const isMultiSelect = input.tagName === "SELECT" && input.multiple;
            input.addEventListener("change", this._handleInputChange(key, isCheckbox, isMultiSelect));
        });
        this._recordType.fields
            .filter((field) => field.type === "image")
            .forEach((field) => {
                const fileInput = dialog.querySelector(`[data-image-file-key="${field.key}"]`);
                fileInput?.addEventListener("change", this._handleImageFileChange(field));
                const chooseBtn = dialog.querySelector(`[data-image-choose-key="${field.key}"]`);
                chooseBtn?.addEventListener("click", () => fileInput?.click());
                const pathInput = dialog.querySelector(`[data-image-path-mode-key="${field.key}"]`);
                pathInput?.addEventListener("input", () => this._updateRemoveVisibility(field));
                const removeBtn = dialog.querySelector(`[data-image-remove-key="${field.key}"]`);
                removeBtn?.addEventListener("click", () => this._clearImageValue(field));
                const modeBtn = dialog.querySelector(`[data-image-mode-btn-key="${field.key}"]`);
                modeBtn?.addEventListener("click", this._toggleImageMode(field));
            });
        dialog.querySelector(".cmc-cancel-btn").addEventListener("click", () => this._closeDialog());

        this._dialogEl = dialog;
        document.body.appendChild(dialog);
        dialog.open = true;
    }

    /**
     * Closes and detaches the add-record dialog, if open. Idempotent/safe to
     * call multiple times (e.g. once from a button click and again from the
     * dialog's own "closed" event) and when no dialog is open at all.
     */
    _closeDialog() {
        if (!this._dialogEl) {
            return;
        }
        const dialog = this._dialogEl;
        this._dialogEl = null;
        this._formValues = {};
        Object.values(this._imagePendingUploads).forEach((pending) => {
            URL.revokeObjectURL(pending.previewUrl);
        });
        this._imagePendingUploads = {};
        this._submitting = false;
        dialog.open = false;
        if (dialog.parentNode) {
            dialog.parentNode.removeChild(dialog);
        }
    }

    /** Shows (or clears, when `message` is falsy) an error INSIDE the
     * currently-open add-record dialog, without touching/re-rendering the
     * rest of the card. No-op if the dialog isn't open. */
    _setDialogError(message) {
        if (!this._dialogEl) {
            return;
        }
        const errorEl = this._dialogEl.querySelector(".cmc-dialog-error");
        if (!errorEl) {
            return;
        }
        errorEl.textContent = message || "";
        errorEl.hidden = !message;
    }

    _setDialogSubmitting(submitting) {
        if (!this._dialogEl) {
            return;
        }
        this._dialogEl.querySelectorAll(".cmc-cancel-btn, .cmc-submit-btn").forEach((button) => {
            button.disabled = submitting;
        });
    }

    /**
     * Opens a small themed confirmation dialog (same document.body-append
     * `<ha-dialog>` technique as the add-record dialog - see its comment for
     * why). `onConfirm` is only called if the user clicks the confirm
     * button; Cancel/Escape/backdrop-click just close the dialog.
     */
    _openConfirmDialog({ title, message, confirmLabel, onConfirm }) {
        if (this._confirmDialogEl) {
            return;
        }
        const dialog = document.createElement("ha-dialog");
        dialog.headerTitle = title;
        dialog.innerHTML = `
      <style>
        .cmc-confirm-message { margin: 0 0 16px 0; min-width: 240px; }
      </style>
      <p class="cmc-confirm-message"></p>
            <ha-dialog-footer slot="footer">
                <ha-button type="button" appearance="plain" slot="secondaryAction" class="cmc-cancel-btn">Cancel</ha-button>
                <ha-button type="button" appearance="filled" variant="danger" slot="primaryAction" class="cmc-confirm-btn"></ha-button>
            </ha-dialog-footer>
    `;
        dialog.querySelector(".cmc-confirm-message").textContent = message;
        dialog.querySelector(".cmc-confirm-btn").textContent = confirmLabel;

        dialog.addEventListener("closed", () => this._closeConfirmDialog());
        dialog.querySelector(".cmc-cancel-btn").addEventListener("click", () => this._closeConfirmDialog());
        dialog.querySelector(".cmc-confirm-btn").addEventListener("click", async () => {
            this._closeConfirmDialog();
            await onConfirm();
        });

        this._confirmDialogEl = dialog;
        document.body.appendChild(dialog);
        dialog.open = true;
    }

    /** Closes and detaches the confirmation dialog, if open. Idempotent, same
     * pattern as _closeDialog(). */
    _closeConfirmDialog() {
        if (!this._confirmDialogEl) {
            return;
        }
        const dialog = this._confirmDialogEl;
        this._confirmDialogEl = null;
        dialog.open = false;
        if (dialog.parentNode) {
            dialog.parentNode.removeChild(dialog);
        }
    }

    /**
     * Opens a themed dialog showing a table thumbnail enlarged (same
     * document.body-append `<ha-dialog>` technique as the other dialogs -
     * see _openConfirmDialog()'s comment for why). Purely a viewer - no
     * footer/actions, just Escape/backdrop-click/close-button to dismiss.
     * `naturalWidth`/`naturalHeight` (the already-loaded thumbnail's own
     * intrinsic size) are used to size the dialog to fit the image
     * closely, rather than a generic fixed box - a fixed box leaves large
     * (and uneven-looking, if its aspect ratio doesn't match the image's)
     * blank padding for any image that doesn't happen to need the full box.
     */
    _openImageDialog(url, label, naturalWidth, naturalHeight, record) {
        this._closeImageDialog();
        const dialog = document.createElement("ha-dialog");
        const headerField = this._recordType?.fields.find(
            (field) => field.key === this._config.image_header_field && field.key !== "timestamp",
        );
        dialog.headerTitle = record
            ? headerField
                ? `${headerField.label}: ${this._formatValueText(record[headerField.key], headerField)}`
                : formatDateTime(this._hass, new Date(record.timestamp))
            : label || "Image";
        const overlayEntries = this._imageOverlayEntries(record);
        // ha-dialog's actual rendered width is NOT controlled by the plain
        // `--width` custom property (that gets immediately recomputed/
        // overridden by ha-dialog's own internal style rule, targeting its
        // shadow-DOM <wa-dialog> child, as
        // `--width: min(var(--ha-dialog-width-md, 580px), var(--full-width))`)
        // - `--ha-dialog-width-md` is the actual public override hook HA
        // provides for exactly this. Confirmed by reading the compiled
        // frontend bundle; setting `--width` directly, tried first, had no
        // effect and left the dialog at its ~580px default, which is what
        // was causing the wider in-content box to overflow it (both the
        // horizontal scrollbar and the large empty padding the image was
        // shrunk to fit around).
        const maxWidthPx = Math.min(window.innerWidth * 0.9, 900);
        const maxHeightPx = window.innerHeight * 0.8;
        let width = naturalWidth || maxWidthPx;
        let height = naturalHeight || maxHeightPx;
        if (width > maxWidthPx) {
            height *= maxWidthPx / width;
            width = maxWidthPx;
        }
        if (height > maxHeightPx) {
            width *= maxHeightPx / height;
            height = maxHeightPx;
        }
        dialog.style.setProperty("--ha-dialog-width-md", `${Math.ceil(width)}px`);
        // ha-dialog's `.body` element (the actual scrollable content area)
        // sets its own padding as
        // `var(--dialog-content-padding, 0 var(--ha-space-6) var(--ha-space-6) var(--ha-space-6))`
        // (confirmed by reading the compiled frontend bundle) - note it's
        // already deliberately topless by default (the header below it
        // provides its own bottom spacing instead), with --ha-space-6 (24px)
        // on the other three sides. Overriding --dialog-content-padding with
        // a single value (tried first) replaces the WHOLE shorthand, which
        // is why that first attempt also put padding on top - this passes
        // the same 4-value shape back, just swapped to the smaller
        // --ha-space-4 (16px) a regular card's content area uses instead of
        // --ha-space-6, so it stays topless like the default.
        dialog.style.setProperty(
            "--dialog-content-padding",
            "0 var(--ha-space-4, 16px) var(--ha-space-4, 16px) var(--ha-space-4, 16px)",
        );
        dialog.innerHTML = `
      <style>
        .cmc-image-dialog-content { position: relative; width: fit-content; max-width: 100%; margin: 0 auto; overflow: hidden; }
        /* max-width/max-height only ever SHRINK an oversized image down to
           fit the dialog sized above - deliberately no explicit width/height
           (which would force even a small/low-res image to stretch up and
           fill it). max-height is a redundant safety net here (the sizing
           above already accounts for it) in case naturalWidth/Height weren't
           available (e.g. image still loading) when this was computed. */
        .cmc-image-dialog-img { max-width: 100%; max-height: 80vh; object-fit: contain; display: block; }
                .cmc-image-dialog-overlay {
                    position: absolute; left: 0; right: 0; bottom: 0;
                    background-color: var(--ha-picture-card-background-color, rgba(0, 0, 0, 0.3));
                    color: var(--ha-picture-card-text-color, #fff);
                    padding: 16px; font-size: var(--ha-font-size-l); line-height: 16px;
                    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; pointer-events: none;
                }
      </style>
      <div class="cmc-image-dialog-content">
        <img class="cmc-image-dialog-img" src="${escapeHtml(url)}" alt="${escapeHtml(label || "")}" />
        ${overlayEntries.length ? `<div class="cmc-image-dialog-overlay">${overlayEntries.map(
            (entry) => `${escapeHtml(entry.label)}: ${entry.value}`,
        ).join(" &middot; ")}</div>` : ""}
      </div>
    `;
        dialog.addEventListener("closed", () => this._closeImageDialog());
        this._imageDialogEl = dialog;
        document.body.appendChild(dialog);
        dialog.open = true;
    }

    /** Closes and detaches the enlarged-image dialog, if open. Idempotent,
     * same pattern as _closeDialog()/_closeConfirmDialog(). */
    _closeImageDialog() {
        if (!this._imageDialogEl) {
            return;
        }
        const dialog = this._imageDialogEl;
        this._imageDialogEl = null;
        dialog.open = false;
        if (dialog.parentNode) {
            dialog.parentNode.removeChild(dialog);
        }
    }

    /**
     * Per-row overflow-menu actions (rendered behind the 3-dot trigger in
     * _render()). Currently just "Delete", but returned as a list so more
     * row actions can be added later without reworking the menu markup or
     * its wiring - each entry just needs a label, mdi icon name (e.g.
     * "mdi:delete", resolved at runtime by `<ha-icon>` - no need to bundle
     * icon path data ourselves), optional `danger` styling flag, and a
     * handler.
     */
    _rowActions(record) {
        return [
            {
                label: "Delete",
                icon: "mdi:delete",
                danger: true,
                handler: () => this._confirmDeleteRecord(record.id),
            },
        ];
    }

    _confirmDeleteRecord(recordId) {
        this._openConfirmDialog({
            title: "Delete record?",
            message: "This action can't be undone.",
            confirmLabel: "Delete",
            onConfirm: async () => {
                try {
                    await this._hass.callWS({
                        type: "custom_records/delete_record",
                        record_type: this._config.record_type,
                        record_id: recordId,
                    });
                    await this._loadData();
                } catch (err) {
                    this._error = err.message || String(err);
                    this._render();
                }
            },
        });
    }

    _handleInputChange(key, isCheckbox, isMultiSelect) {
        return (event) => {
            if (isCheckbox) {
                this._formValues[key] = event.target.checked;
            } else if (isMultiSelect) {
                this._formValues[key] = Array.from(event.target.selectedOptions).map(
                    (option) => option.value,
                );
            } else {
                this._formValues[key] = event.target.value;
            }
        };
    }

    /**
     * Validates+stages a picked image file for upload (the actual network
     * upload happens later, at submit time - see _handleSubmit()). Rejects
     * an unsupported extension or an over-10MB file with an inline dialog
     * error, leaving any previously staged file untouched in that case.
     */
    _handleImageFileChange(field) {
        return (event) => {
            const file = event.target.files && event.target.files[0];
            if (!file) {
                this._clearImageUpload(field);
                return;
            }
            const dotIndex = file.name.lastIndexOf(".");
            const ext = dotIndex === -1 ? "" : file.name.slice(dotIndex).toLowerCase();
            if (!IMAGE_UPLOAD_EXTENSIONS.has(ext)) {
                this._setDialogError(`${field.label}: unsupported image extension '${ext}'`);
                // Clear any previously-staged selection too - the native file
                // input's value has already moved to this rejected file, so
                // silently keeping the old pending upload would submit a file
                // that no longer matches what's displayed as "chosen".
                this._clearImageUpload(field);
                return;
            }
            if (file.size > MAX_IMAGE_UPLOAD_BYTES) {
                this._setDialogError(`${field.label}: image is too large (max 10MB)`);
                this._clearImageUpload(field);
                return;
            }
            this._setDialogError(null);
            const previous = this._imagePendingUploads[field.key];
            if (previous) {
                URL.revokeObjectURL(previous.previewUrl);
            }
            const previewUrl = URL.createObjectURL(file);
            this._imagePendingUploads[field.key] = { file, previewUrl };
            const dialog = this._dialogEl;
            if (!dialog) {
                return;
            }
            const previewEl = dialog.querySelector(`[data-image-preview-key="${field.key}"]`);
            const filenameEl = dialog.querySelector(`[data-image-filename-key="${field.key}"]`);
            if (previewEl) {
                previewEl.src = previewUrl;
                previewEl.hidden = false;
            }
            if (filenameEl) {
                filenameEl.textContent = file.name;
            }
            this._updateRemoveVisibility(field);
        };
    }

    /**
     * Clears a staged pending upload (Remove button, or the upload->path
     * mode switch) - revokes its preview object URL and resets the file
     * input/preview/filename DOM back to their empty state. Safe to call
     * with nothing staged.
     */
    _clearImageUpload(field) {
        const pending = this._imagePendingUploads[field.key];
        if (pending) {
            URL.revokeObjectURL(pending.previewUrl);
        }
        delete this._imagePendingUploads[field.key];
        const dialog = this._dialogEl;
        if (!dialog) {
            return;
        }
        const fileInput = dialog.querySelector(`[data-image-file-key="${field.key}"]`);
        const previewEl = dialog.querySelector(`[data-image-preview-key="${field.key}"]`);
        const filenameEl = dialog.querySelector(`[data-image-filename-key="${field.key}"]`);
        if (fileInput) {
            fileInput.value = "";
        }
        if (previewEl) {
            previewEl.hidden = true;
            previewEl.removeAttribute("src");
        }
        if (filenameEl) {
            filenameEl.textContent = "No file chosen";
        }
        this._updateRemoveVisibility(field);
    }

    /**
     * Shows/hides the shared clear ("x") control based on whichever mode
     * (upload or path) is currently active for this field - a pending
     * upload in upload mode, or non-empty text in path mode.
     */
    _updateRemoveVisibility(field) {
        const dialog = this._dialogEl;
        if (!dialog) {
            return;
        }
        const removeWrap = dialog.querySelector(`[data-image-remove-wrap-key="${field.key}"]`);
        if (!removeWrap) {
            return;
        }
        const uploadMode = dialog.querySelector(`[data-image-upload-mode-key="${field.key}"]`);
        const isUploadMode = !!uploadMode && !uploadMode.hidden;
        const hasValue = isUploadMode
            ? !!this._imagePendingUploads[field.key]
            : !!dialog.querySelector(`[data-image-path-mode-key="${field.key}"]`)?.value;
        removeWrap.hidden = !hasValue;
    }

    /**
     * Clears whichever value is currently active for an image field (Remove
     * button click) - the pending upload in upload mode, or the typed text
     * in path mode.
     */
    _clearImageValue(field) {
        const dialog = this._dialogEl;
        if (!dialog) {
            return;
        }
        const uploadMode = dialog.querySelector(`[data-image-upload-mode-key="${field.key}"]`);
        if (uploadMode && !uploadMode.hidden) {
            this._clearImageUpload(field);
            return;
        }
        const pathInput = dialog.querySelector(`[data-image-path-mode-key="${field.key}"]`);
        if (pathInput) {
            pathInput.value = "";
        }
        delete this._formValues[field.key];
        this._updateRemoveVisibility(field);
    }

    /**
     * Toggles an image field between its two mutually-exclusive input modes
     * (upload, the default, and manual path entry), both living inside the
     * SAME bordered control - only the leading mode-switch icon button and
     * the shared clear ("x") control stay constant; the middle section swaps
     * between the file-choose UI and a blended-in text input. Switching away
     * from a mode clears its value (an unconsumed file is only ever local to
     * the browser at this point - see _handleSubmit() - so there's nothing
     * server-side to clean up). `required` (native HTML constraint
     * validation) moves to whichever input is currently active; the hidden
     * one is automatically excluded from constraint validation per the HTML
     * spec.
     */
    _toggleImageMode(field) {
        return () => {
            const dialog = this._dialogEl;
            if (!dialog) {
                return;
            }
            const uploadMode = dialog.querySelector(`[data-image-upload-mode-key="${field.key}"]`);
            const pathInput = dialog.querySelector(`[data-image-path-mode-key="${field.key}"]`);
            const modeBtn = dialog.querySelector(`[data-image-mode-btn-key="${field.key}"]`);
            const modeIcon = modeBtn?.querySelector("ha-icon");
            const fileInput = dialog.querySelector(`[data-image-file-key="${field.key}"]`);
            const switchingToPath = !uploadMode.hidden;
            uploadMode.hidden = switchingToPath;
            pathInput.hidden = !switchingToPath;
            if (switchingToPath) {
                this._clearImageUpload(field);
                if (fileInput) {
                    fileInput.required = false;
                }
                pathInput.required = !!field.required;
                modeIcon?.setAttribute("icon", "mdi:folder-open");
                modeBtn?.setAttribute("aria-label", "Switch to uploading a file");
                modeBtn?.setAttribute("title", "Switch to uploading a file");
                pathInput.focus();
            } else {
                pathInput.value = "";
                pathInput.required = false;
                delete this._formValues[field.key];
                if (fileInput) {
                    fileInput.required = !!field.required;
                }
                modeIcon?.setAttribute("icon", "mdi:upload");
                modeBtn?.setAttribute("aria-label", "Switch to entering a file path");
                modeBtn?.setAttribute("title", "Switch to entering a file path");
            }
            this._updateRemoveVisibility(field);
        };
    }

    _renderFieldInput(field) {
        const label = `${escapeHtml(field.label)}${field.required ? " *" : ""}`;
        const inputId = `field-${field.key}`;
        const required = field.required ? " required" : "";
        const value = this._formValues[field.key];
        const valueAttribute = value === undefined || value === null ? "" : ` value="${escapeHtml(value)}"`;
        if (field.type === "image") {
            return `<label id="${inputId}-label">${label}</label>
<div class="cmc-image-field" data-image-field-key="${field.key}">
  <div class="cmc-image-upload-block" data-image-control-key="${field.key}">
    <button type="button" class="cmc-image-mode-btn" data-image-mode-btn-key="${field.key}" aria-label="Switch to entering a file path" title="Switch to entering a file path"><ha-icon icon="mdi:upload"></ha-icon></button>
    <span class="cmc-image-upload-divider"></span>
    <span class="cmc-image-upload-mode" data-image-upload-mode-key="${field.key}">
      <input type="file" class="cmc-image-file-input" accept="${IMAGE_UPLOAD_ACCEPT}" data-image-file-key="${field.key}"${required} aria-labelledby="${inputId}-label" />
      <button type="button" class="cmc-image-choose-btn" data-image-choose-key="${field.key}">Choose file</button>
      <span class="cmc-image-upload-divider"></span>
      <span class="cmc-image-upload-filename-wrap">
        <img class="cmc-image-upload-preview" data-image-preview-key="${field.key}" alt="" hidden />
        <span class="cmc-image-upload-filename" data-image-filename-key="${field.key}">No file chosen</span>
      </span>
    </span>
    <input type="text" class="cmc-image-path-input" data-key="${field.key}" data-image-path-mode-key="${field.key}" aria-labelledby="${inputId}-label"${valueAttribute} placeholder="Full path to an existing image file under /config, e.g. /config/www/photo.jpg" hidden />
    <div class="cmc-image-remove-wrap" data-image-remove-wrap-key="${field.key}" hidden>
      <span class="cmc-image-upload-divider"></span>
      <button type="button" class="cmc-image-remove-btn" data-image-remove-key="${field.key}" aria-label="Clear selection"><ha-icon icon="mdi:close"></ha-icon></button>
    </div>
  </div>
</div>`;
        }
        if (field.type === "long_text") {
            return `<label for="${inputId}">${label}</label><textarea id="${inputId}" data-key="${field.key}"${required}>${value === undefined || value === null ? "" : escapeHtml(value)}</textarea>`;
        }
        if (field.type === "boolean") {
            return `<label><input type="checkbox" data-key="${field.key}"${value ? " checked" : ""}${field.required ? ' aria-required="true"' : ""} /> ${label}</label>`;
        }
        if (field.type === "datetime") {
            return `<label for="${inputId}">${label}</label><input id="${inputId}" type="datetime-local" data-key="${field.key}"${valueAttribute}${required} />`;
        }
        if (field.type === "single_select" || field.type === "multi_select") {
            const options = (field.options || [])
                .map((option) => {
                    const selected = Array.isArray(value) ? value.includes(option) : value === option;
                    return `<option value="${escapeHtml(option)}"${selected ? " selected" : ""}>${escapeHtml(option)}</option>`;
                })
                .join("");
            const multiple = field.type === "multi_select" ? "multiple" : "";
            return `<label for="${inputId}">${label}</label><select id="${inputId}" data-key="${field.key}" ${multiple}${required}><option value=""></option>${options}</select>`;
        }
        const inputType = field.type === "number" ? "number" : "text";
        const step = field.type === "number" ? ` step="any"` : "";
        return `<label for="${inputId}">${label}</label><input id="${inputId}" type="${inputType}" data-key="${field.key}"${step}${valueAttribute}${required} />`;
    }

    _imageOverlayEntries(record) {
        if (!record) return [];
        return (this._config.image_overlay_fields || []).flatMap((entry) => {
            const field = this._recordType?.fields.find((field) => field.key === entry.field);
            const value = record[entry.field];
            if (!field || field.type === "image" || value === undefined || value === null || value === "" || (Array.isArray(value) && value.length === 0)) {
                return [];
            }
            return [{ label: entry.label || field.label, value: this._formatValue(value, field) }];
        });
    }

    _formatValue(value, field) {
        return escapeHtml(this._formatValueText(value, field));
    }

    _formatValueText(value, field) {
        if (value === undefined || value === null) {
            return "";
        }
        if (field.type === "boolean") {
            return value ? "Yes" : "No";
        }
        if (Array.isArray(value)) {
            return value.join(", ");
        }
        return String(value);
    }

    _renderCell(record, field) {
        if (field.type !== "image") {
            return this._formatValue(record[field.key], field);
        }
        const value = record[field.key];
        if (!value || !value.media_source) {
            return "";
        }
        const url = this._imageUrls[`${record.id}/${field.key}`];
        if (url === null) {
            return `<em>Image unavailable</em>`;
        }
        if (!url) {
            return "Loading image...";
        }
        return `<img class="record-image" data-record-id="${escapeHtml(record.id)}" src="${url}" alt="${escapeHtml(field.label)}" tabindex="0" role="button" aria-label="Enlarge image" />`;
    }

    /**
     * Fields to show as TABLE columns, honoring the `columns` config's
     * allow-list + order when present (validated against real field keys in
     * _loadData()). The add-record form always uses the full, unfiltered
     * `this._recordType.fields` instead - it is deliberately unaffected by
     * this config, per the "table only" scope decision.
     */
    _visibleFields() {
        if (!this._recordType) {
            return [];
        }
        if (!this._config.columns) {
            return this._recordType.fields;
        }
        return this._config.columns
            .map((key) => this._recordType.fields.find((f) => f.key === key))
            .filter(Boolean);
    }

    _render() {
        if (!this.shadowRoot) {
            return;
        }
        if (!this._config) {
            this.shadowRoot.innerHTML = "";
            return;
        }

        const showAddRecord = this._config.show_add_record !== false;
        const showActions = this._config.show_actions !== false;

        const title = escapeHtml(
            this._config.title || (this._recordType ? this._recordType.name : this._config.record_type),
        );

        let bodyHtml;
        if (this._loading && !this._recordType) {
            bodyHtml = "<p>Loading...</p>";
        } else if (this._error) {
            bodyHtml = `<p class="error">${escapeHtml(this._error)}</p>`;
        } else if (!this._recordType) {
            bodyHtml = "<p>No data.</p>";
        } else {
            const tableFields = this._visibleFields();
            const headerCells = tableFields.map((f) => `<th scope="col">${escapeHtml(f.label)}</th>`).join("");
            const actionsHeader = showActions ? '<th scope="col"><span class="visually-hidden">Actions</span></th>' : "";
            const rows = this._records
                .map((record) => {
                    const cells = tableFields
                        .map((f) => `<td>${this._renderCell(record, f)}</td>`)
                        .join("");
                    const actionsCell = showActions
                        ? `<td class="actions-cell">
                                <ha-dropdown class="row-actions-dropdown" placement="bottom-end" data-record-id="${record.id}">
                                    <ha-icon-button slot="trigger" label="Actions for record from ${escapeHtml(formatDateTime(this._hass, new Date(record.timestamp)))}"><ha-icon icon="mdi:dots-vertical"></ha-icon></ha-icon-button>
                  ${this._rowActions(record)
                            .map(
                                (action, index) => `<ha-dropdown-item value="${index}"${action.danger ? ' variant="danger"' : ""}>
                    ${escapeHtml(action.label)}
                    <ha-icon slot="icon" icon="${action.icon}"></ha-icon>
                  </ha-dropdown-item>`,
                            )
                            .join("")}
                </ha-dropdown>
              </td>`
                        : "";
                    return `<tr>
            <td>${formatDateTime(this._hass, new Date(record.timestamp))}</td>
            ${cells}
            ${actionsCell}
          </tr>`;
                })
                .join("");
            const colspan = tableFields.length + 1 + (showActions ? 1 : 0);

            const tableHtml = `
                <div class="table-scroll">
                <table>
                    <thead><tr><th scope="col">Timestamp</th>${headerCells}${actionsHeader}</tr></thead>
          <tbody>${rows || `<tr><td colspan="${colspan}">No records yet.</td></tr>`}</tbody>
        </table>
                </div>
      `;

            let addRecordHtml = "";
            if (showAddRecord) {
                addRecordHtml = `
        <div class="add-record-actions">
          <ha-button id="open-add-record" appearance="filled">
            <ha-icon slot="start" icon="mdi:plus"></ha-icon>
            Add record
          </ha-button>
        </div>
      `;
            }

            bodyHtml = `${tableHtml}${addRecordHtml}`;
        }

        this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        table { width: 100%; border-collapse: collapse; margin-bottom: 12px; }
        .table-scroll { max-width: 100%; overflow-x: auto; }
        th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--divider-color, #e0e0e0); vertical-align: middle; }
        .actions-cell { text-align: right; }
        .actions-cell ha-icon-button {
          --ha-icon-button-size: 28px;
          --mdc-icon-size: 18px;
          color: var(--secondary-text-color);
        }
        /* Fixed, compact thumbnail size so an image cell never grows the row
           taller than its text siblings - object-fit: cover crops (rather
           than letterboxes) any non-square source to still fill this box. */
        .record-image { width: 32px; height: 32px; object-fit: cover; border-radius: 4px; display: block; cursor: pointer; }
        .record-image:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
        .add-record-actions { display: flex; justify-content: flex-end; }
        .error { color: var(--error-color, red); }
        .visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
      </style>
      <ha-card header="${title}">
        <div class="card-content">${bodyHtml}</div>
      </ha-card>
    `;

        const openAddRecordButton = this.shadowRoot.getElementById("open-add-record");
        if (openAddRecordButton) {
            openAddRecordButton.addEventListener("click", () => this._openAddDialog());
        }
        this.shadowRoot.querySelectorAll(".row-actions-dropdown").forEach((dropdown) => {
            dropdown.addEventListener("wa-select", (event) => {
                const record = this._records.find((r) => r.id === dropdown.dataset.recordId);
                const action = record && this._rowActions(record)[Number(event.detail.item.value)];
                action?.handler();
            });
        });
        this.shadowRoot.querySelectorAll(".record-image").forEach((img) => {
            const record = this._records.find((record) => record.id === img.dataset.recordId);
            const openEnlarged = () => this._openImageDialog(img.src, img.alt, img.naturalWidth, img.naturalHeight, record);
            img.addEventListener("click", openEnlarged);
            img.addEventListener("keydown", (event) => {
                if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    openEnlarged();
                }
            });
        });
    }

    static getStubConfig() {
        return { type: "custom:custom-records-card", record_type: "" };
    }

    static getConfigElement() {
        return document.createElement("custom-records-card-editor");
    }
}

const EDITOR_FIELD_LABELS = {
    record_type: "Record type",
    title: "Title",
    last: "Last N records (count or duration like 2w)",
    show_add_record: "Show add-record form",
    show_actions: "Show row actions menu",
    image_header_field: "Image dialog header field",
};

/**
 * Visual editor for custom-records-card, using HA's built-in <ha-form>.
 *
 * Exposes every card config option (record_type, title, last, show_add_record,
 * show_actions) as a form field, and reports changes back to the
 * dashboard editor via the standard `config-changed` event. The `<ha-form>`
 * element is created once and reused across updates (its `.data`/`.schema`
 * are updated in place) instead of being recreated on every render, since
 * recreating it would steal input focus while the user is mid-edit (e.g.
 * typing in the `title` field).
 */
class CustomRecordsCardEditor extends HTMLElement {
    constructor() {
        super();
        this._config = {};
        this._hass = null;
        this._recordTypes = [];
        this._recordTypesLoaded = false;
        this._recordTypesLoading = false;
        this._recordTypesError = null;
        this._form = null;
        this._columnsSection = null;
        this._overlaySection = null;
        this._overlaySignature = null;
    }

    setConfig(config) {
        this._config = config || {};
        this._updateForm();
    }

    set hass(hass) {
        this._hass = hass;
        this._loadRecordTypes();
        this._updateForm();
    }

    async _loadRecordTypes() {
        if (!this._hass || this._recordTypesLoaded || this._recordTypesLoading) {
            return;
        }
        this._recordTypesLoading = true;
        this._recordTypesError = null;
        try {
            const response = await this._hass.callWS({ type: "custom_records/list_record_types" });
            this._recordTypes = response.record_types || [];
            this._recordTypesLoaded = true;
        } catch (err) {
            this._recordTypes = [];
            this._recordTypesError = err.message || String(err);
        } finally {
            this._recordTypesLoading = false;
        }
        this._updateForm();
    }

    // Merge in explicit defaults purely for display, so boolean toggles/`last`
    // show their effective (card-default) value for a config that omits them,
    // WITHOUT writing those defaults back into the config until the user
    // actually changes something.
    _displayData() {
        return {
            last: DEFAULT_LAST_COUNT,
            show_add_record: true,
            show_actions: true,
            image_header_field: "timestamp",
            ...this._config,
        };
    }

    _schema() {
        return [
            {
                name: "record_type",
                required: true,
                selector: {
                    select: {
                        mode: "dropdown",
                        options: this._recordTypes.map((rt) => ({ value: rt.id, label: rt.name })),
                    },
                },
            },
            { name: "title", selector: { text: {} } },
            { name: "last", selector: { text: {} } },
            { name: "show_add_record", selector: { boolean: {} } },
            { name: "show_actions", selector: { boolean: {} } },
            {
                name: "image_header_field",
                selector: {
                    select: {
                        mode: "dropdown",
                        options: [
                            { value: "timestamp", label: "Timestamp" },
                            ...(this._recordTypes.find((rt) => rt.id === this._config.record_type)?.fields || [])
                                .filter((field) => field.type !== "image" && field.key !== "timestamp")
                                .map((field) => ({ value: field.key, label: field.label })),
                        ],
                    },
                },
            },
        ];
    }

    _ensureForm() {
        if (this._form) {
            return this._form;
        }
        this._form = document.createElement("ha-form");
        this._form.computeLabel = (schema) => EDITOR_FIELD_LABELS[schema.name] || schema.name;
        this._form.addEventListener("value-changed", (event) => {
            event.stopPropagation();
            this.dispatchEvent(
                new CustomEvent("config-changed", {
                    detail: { config: { ...this._config, ...event.detail.value } },
                    bubbles: true,
                    composed: true,
                }),
            );
        });
        this.appendChild(this._form);
        return this._form;
    }

    _updateForm() {
        if (!this._hass) {
            return;
        }
        const form = this._ensureForm();
        form.hass = this._hass;
        form.schema = this._schema();
        form.data = this._displayData();
        this._updateColumnsSection();
                this._updateOverlaySection();
    }

        _updateOverlaySection() {
                if (!this._overlaySection) {
                        this._overlaySection = document.createElement("div");
                        this._overlaySection.className = "overlay-picker";
                        this.appendChild(this._overlaySection);
                }
                const section = this._overlaySection;
                const recordType = this._recordTypes.find((rt) => rt.id === this._config.record_type);
                const fields = (recordType?.fields || []).filter((field) => field.type !== "image");
                const entries = this._config.image_overlay_fields || [];
                const signature = JSON.stringify({ fields, entries });
                if (signature === this._overlaySignature) return;
                this._overlaySignature = signature;
                if (!recordType) {
                        section.replaceChildren();
                        return;
                }
                const available = fields.filter((field) => !entries.some((entry) => entry.field === field.key));
                section.innerHTML = `
                    <style>
                        .overlay-picker { margin-top: 16px; }
                        .overlay-picker h4 { margin: 4px 0 8px; font-size: 0.9em; color: var(--secondary-text-color, #666); }
                        .overlay-picker__list { margin: 0; padding: 0; }
                        .overlay-picker__row { display: flex; align-items: center; gap: 8px; list-style: none; padding: 4px 0; }
                        .overlay-picker__label { flex: 1; min-width: 0; overflow-wrap: anywhere; }
                        .overlay-picker input, .overlay-picker select {
                            box-sizing: border-box; width: 100%; min-width: 0; padding: 8px;
                            font: inherit; color: var(--primary-text-color); background: var(--card-background-color);
                            border: 1px solid var(--divider-color); border-radius: 4px;
                        }
                        .overlay-picker input { display: block; margin-top: 4px; }
                        .overlay-picker__actions { display: flex; flex-shrink: 0; }
                        .overlay-picker ha-icon-button { --ha-icon-button-size: 32px; --mdc-icon-size: 18px; }
                        .overlay-picker select { margin-top: 8px; }
                    </style>
                    <h4>Image overlay fields</h4>
                    <ul class="overlay-picker__list">${entries.map((entry, index) => {
                        const label = fields.find((field) => field.key === entry.field)?.label || entry.field;
                        return `<li class="overlay-picker__row">
                            <label class="overlay-picker__label">${escapeHtml(label)}
                                <input data-index="${index}" value="${escapeHtml(entry.label || "")}" placeholder="${escapeHtml(label)}" aria-label="${escapeHtml(label)} overlay label" />
                            </label>
                            <span class="overlay-picker__actions">
                                <ha-icon-button data-index="${index}" data-action="up" label="Move ${escapeHtml(label)} up" ${index === 0 ? "disabled" : ""}><ha-icon icon="mdi:arrow-up"></ha-icon></ha-icon-button>
                                <ha-icon-button data-index="${index}" data-action="down" label="Move ${escapeHtml(label)} down" ${index === entries.length - 1 ? "disabled" : ""}><ha-icon icon="mdi:arrow-down"></ha-icon></ha-icon-button>
                                <ha-icon-button data-index="${index}" data-action="remove" label="Remove ${escapeHtml(label)}" ><ha-icon icon="mdi:close"></ha-icon></ha-icon-button>
                            </span>
                        </li>`;
                }).join("")}</ul>
                    <select aria-label="Add image overlay field" ${available.length ? "" : "disabled"}>
                        <option value="">Add field</option>
                        ${available.map((field) => `<option value="${escapeHtml(field.key)}">${escapeHtml(field.label)}</option>`).join("")}
                    </select>
                `;
                const updateEntries = (nextEntries, preserveFocus = false) => {
                        this._config = { ...this._config, image_overlay_fields: nextEntries };
                        if (preserveFocus) this._overlaySignature = JSON.stringify({ fields, entries: nextEntries });
                        this._updateForm();
                        this._emitConfigChanged(this._config);
                };
                section.querySelectorAll("input[data-index]").forEach((input) => {
                        input.addEventListener("input", () => {
                                const nextEntries = this._config.image_overlay_fields.map((entry) => ({ ...entry }));
                                const entry = nextEntries[Number(input.dataset.index)];
                                if (input.value) entry.label = input.value;
                                else delete entry.label;
                                updateEntries(nextEntries, true);
                        });
                });
                section.querySelector("select").addEventListener("change", (event) => {
                        if (event.target.value) {
                                updateEntries([...(this._config.image_overlay_fields || []), { field: event.target.value }]);
                        }
                });
                section.querySelectorAll("ha-icon-button[data-action]").forEach((button) => {
                        button.addEventListener("click", () => {
                                const nextEntries = [...this._config.image_overlay_fields];
                                const index = Number(button.dataset.index);
                                const action = button.dataset.action;
                                if (action === "remove") nextEntries.splice(index, 1);
                                else {
                                        const target = action === "up" ? index - 1 : index + 1;
                                        if (target < 0 || target >= nextEntries.length) return;
                                        [nextEntries[index], nextEntries[target]] = [nextEntries[target], nextEntries[index]];
                                }
                                updateEntries(nextEntries);
                        });
                });
        }

    _ensureColumnsSection() {
        if (this._columnsSection) {
            return this._columnsSection;
        }
        this._columnsSection = document.createElement("div");
        this._columnsSection.className = "columns-picker";
        this.appendChild(this._columnsSection);
        return this._columnsSection;
    }

    _emitConfigChanged(config) {
        this.dispatchEvent(
            new CustomEvent("config-changed", {
                detail: { config },
                bubbles: true,
                composed: true,
            }),
        );
    }

    /**
     * Renders the `columns` picker: a "Visible columns" list (in configured
     * order, with up/down/remove controls) and an "Available fields" list
     * (with an add control) for the currently selected record type. Not a
     * plain text field and not backed by `<ha-form>` - built directly as
     * hand-rolled HTML/listeners (same style as CustomRecordsCard itself)
     * since `<ha-form>`'s reorderable multi-select support isn't guaranteed
     * across HA frontend versions, per P0-10's plan.
     */
    _updateColumnsSection() {
        const section = this._ensureColumnsSection();
        const recordType = this._recordTypes.find((rt) => rt.id === this._config.record_type);
        if (!recordType) {
            section.innerHTML = this._recordTypesError
                ? `<p class="columns-picker__error" role="alert">Could not load record types: ${escapeHtml(this._recordTypesError)}</p><ha-button class="columns-picker__retry" appearance="plain">Retry</ha-button>`
                : `<p class="columns-picker__hint">Select a record type to configure columns.</p>`;
            section.querySelector(".columns-picker__retry")?.addEventListener("click", () => this._loadRecordTypes());
            return;
        }

        const allFields = recordType.fields || [];
        const selectedKeys = this._config.columns || allFields.map((f) => f.key);
        const byKey = new Map(allFields.map((f) => [f.key, f]));
        const selectedFields = selectedKeys.map((key) => byKey.get(key)).filter(Boolean);
        const availableFields = allFields.filter((f) => !selectedKeys.includes(f.key));

        const selectedRows = selectedFields
            .map(
                (f, index) => `
          <li class="columns-picker__row" data-key="${f.key}">
            <span>${escapeHtml(f.label)}</span>
            <span class="columns-picker__actions">
              <ha-icon-button data-action="up" data-key="${f.key}" label="Move ${escapeHtml(f.label)} up" ${index === 0 ? "disabled" : ""}><ha-icon icon="mdi:arrow-up"></ha-icon></ha-icon-button>
              <ha-icon-button data-action="down" data-key="${f.key}" label="Move ${escapeHtml(f.label)} down" ${index === selectedFields.length - 1 ? "disabled" : ""}><ha-icon icon="mdi:arrow-down"></ha-icon></ha-icon-button>
              <ha-icon-button data-action="remove" data-key="${f.key}" label="Hide ${escapeHtml(f.label)}"><ha-icon icon="mdi:close"></ha-icon></ha-icon-button>
            </span>
          </li>`,
            )
            .join("");

        const availableRows = availableFields
            .map(
                (f) => `
          <li class="columns-picker__row" data-key="${f.key}">
            <span>${escapeHtml(f.label)}</span>
            <span class="columns-picker__actions">
              <ha-icon-button data-action="add" data-key="${f.key}" label="Show ${escapeHtml(f.label)}"><ha-icon icon="mdi:plus"></ha-icon></ha-icon-button>
            </span>
          </li>`,
            )
            .join("");

        section.innerHTML = `
      <style>
        .columns-picker { margin-top: 8px; }
        .columns-picker__group { margin-top: 8px; }
        .columns-picker__group h4 { margin: 4px 0; font-size: 0.9em; color: var(--secondary-text-color, #666); }
        .columns-picker__list { margin: 0; padding: 0; }
        .columns-picker__row { display: flex; align-items: center; justify-content: space-between; padding: 2px 0; list-style: none; }
        .columns-picker__actions ha-icon-button { --ha-icon-button-size: 32px; --mdc-icon-size: 18px; margin-left: 4px; }
        .columns-picker__hint { color: var(--secondary-text-color, #666); font-size: 0.9em; }
        .columns-picker__error { color: var(--error-color, red); font-size: 0.9em; }
      </style>
      <div class="columns-picker__group">
        <h4>Visible columns</h4>
        <ul class="columns-picker__list">${selectedRows || "<li>(none)</li>"}</ul>
      </div>
      <div class="columns-picker__group">
        <h4>Available fields</h4>
        <ul class="columns-picker__list">${availableRows || "<li>(none)</li>"}</ul>
      </div>
    `;

        section.querySelectorAll("ha-icon-button[data-action]").forEach((button) => {
            button.addEventListener("click", () => {
                const action = button.dataset.action;
                const key = button.dataset.key;
                const newKeys = selectedFields.map((f) => f.key);
                if (action === "add") {
                    newKeys.push(key);
                } else if (action === "remove") {
                    const idx = newKeys.indexOf(key);
                    if (idx !== -1) {
                        newKeys.splice(idx, 1);
                    }
                } else if (action === "up" || action === "down") {
                    const idx = newKeys.indexOf(key);
                    const swapWith = action === "up" ? idx - 1 : idx + 1;
                    if (idx !== -1 && swapWith >= 0 && swapWith < newKeys.length) {
                        [newKeys[idx], newKeys[swapWith]] = [newKeys[swapWith], newKeys[idx]];
                    }
                }
                this._emitConfigChanged({ ...this._config, columns: newKeys });
            });
        });
    }
}

// Registering the custom element immediately at module evaluation time is
// racy: this module is loaded via a dynamic `import()` fired from HA's
// frontend bootstrap script, in parallel with HA's own core/app bundles.
// If our `customElements.define()` call happens to run before HA's frontend
// finishes setting up its custom element registry, our registration is
// silently lost (the class is never retrievable via `customElements.get()`
// afterwards, even though this module ran fine). Deferring registration
// until a core HA element (`home-assistant`, the app's root element) is
// defined ensures the registry is already in its final state.
function registerCustomRecordsCard() {
    if (customElements.get("custom-records-card")) {
        return;
    }
    customElements.define("custom-records-card", CustomRecordsCard);
    customElements.define("custom-records-card-editor", CustomRecordsCardEditor);

    window.customCards = window.customCards || [];
    window.customCards.push({
        type: "custom-records-card",
        name: "Custom Records",
        description: "List and add records for a Custom Records record type.",
    });
}

if (customElements.get("home-assistant")) {
    registerCustomRecordsCard();
} else {
    customElements.whenDefined("home-assistant").then(registerCustomRecordsCard);
}

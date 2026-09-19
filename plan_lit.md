# Plan: Lit/TS migration + conditional built-in card registry

## Decisions locked in (via user Q&A)
- Scope: infra only - no new cards designed yet. Ship with just the existing
  `custom-records-card` registered in a new generic multi-card registry,
  ready for more cards later.
- Tooling: TypeScript + Lit decorators (`@property`/`@state`/`@customElement`),
  bundled with esbuild.
- JS lint/tests: deferred to a later pass - NOT part of this migration.
- Options Flow: confirmed OK to add (reverses config_flow.py's current
  documented "no separate Configure dialog needed" decision).
- Build output (`custom_components/custom_records/www/*.js`) is **not**
  committed to git - built locally for dev, and published as a GitHub
  Release asset via `zip_release` (see "Build/bundling & release strategy"
  below).

## TL;DR
Three independently-verifiable phases: (1) stand up JS build tooling with
zero behavior change, (2) rewrite the existing card + its editor as real
LitElements in TypeScript, (3) generalize frontend.py into a multi-card
registry gated by a new Options Flow ("Configure" gear icon), so disabled
built-in cards are never injected into the HA frontend at all. A separate
section below covers exactly where/when the build happens (local dev vs.
release) and how HACS actually fetches this integration's code.

---

## Phase 1 - JS build tooling foundation (no behavior change)

1. New top-level `frontend/` directory (sibling to `custom_components/`):
   - `frontend/package.json` (private, not published) - devDependencies:
     `lit`, `typescript`, `esbuild`.
   - `frontend/tsconfig.json` - **must** set `"experimentalDecorators": true`
     AND `"useDefineForClassFields": false`. This second flag is a
     well-known Lit+TS gotcha: with the default `true` (implied by modern
     `target`s), native class-field semantics silently override the
     accessor Lit installs for `@property()`/`@state()`, breaking
     reactivity with NO compile error - only a runtime symptom (UI stops
     updating). Must be caught by a manual smoke test (see Phase 2
     Verification), since `tsc` alone won't catch it.
   - `frontend/src/` - TypeScript sources (see Phase 2 for the actual files).
   - Build script (`package.json` `scripts.build`): esbuild bundling the
     entry point to `../custom_components/custom_records/www/*.js`, flags:
     `--bundle --format=esm --minify` (no `--packages=external` - `lit` MUST
     be fully inlined since HACS ships the raw repo file with no build step
     on the user's side; verify the built output has zero remaining bare
     `from "lit"` imports).
     `scripts.typecheck`: `tsc --noEmit` (type-check only, esbuild does the
     actual transpile/bundle).
2. `.gitignore`: add `frontend/node_modules/` **and**
   `custom_components/custom_records/www/*.js` (the build output itself -
   see "Build/bundling & release strategy" for why this is not committed).
3. `scripts/setup`: add `(cd frontend && npm ci && npm run build)` so a
   fresh clone has a working local build immediately.
4. `scripts/lint` (or a new `scripts/build`): add
   `(cd frontend && npm run typecheck && npm run build)`.
5. `.github/workflows/lint.yml`: add a Node job that runs
   `npm ci && npm run typecheck && npm run build` on every PR/push - just
   verifies the build succeeds (nothing is committed/compared, since the
   output isn't tracked in git) - catches breakage before a release is ever
   attempted.
6. Decide on minimal hand-rolled `.d.ts` types for just the `HomeAssistant`/
   card-config shapes actually used (matches the project's existing "small,
   dependency-light" philosophy) rather than pulling in a full community
   types package pinned to one HA release.

**Verification**: `npm run build` produces a behaviorally-equivalent
`custom-records-card.js` that still passes existing `tests/test_frontend.py`;
grep the built file for `"lit"`/`from "lit"` to confirm no unresolved bare
imports remain.

---

## Phase 2 - Rewrite the existing card as real LitElements (*depends on 1*)

1. `frontend/src/format.ts` - port `useAmPm`/`formatDateNumeric`/
   `formatTimeWithSeconds`/`formatDateTime` verbatim (incl. the `bg`+`YMD`
   special case) from the current hand-rolled versions in
   custom_components/custom_records/www/custom-records-card.js.
2. `frontend/src/types.ts` - minimal local interfaces for `HomeAssistant`
   (just `.locale`, `.callWS`, `.connection.subscribeEvents`, `.config`),
   the card config shape, and the record/record-type shapes already
   implicit in the current file.
3. `frontend/src/custom-records-card.ts` - `CustomRecordsCard` as
   `class extends LitElement`:
   - `@property({attribute: false}) hass!: HomeAssistant` - **no custom
     setter** (matches current real HA source precedent confirmed in
     `home-assistant-main.ts`/`notification-manager.ts`, both plain
     `@property` with no setter). Move the current `hass` setter's side
     effects (first-run subscribe, config validation) into `willUpdate`/
     `firstUpdated`, guarded by `changedProps.has("hass")`.
   - `@state()` for all the current plain instance fields that drive
     rendering (`_recordType`, `_records`, `_formValues`, `_loading`,
     `_error`, `_imageUrls`, `_configValid`) - assigning them now
     auto-schedules a re-render, so every current manual `this._render()`
     call site is deleted.
   - `render()` replaces `_render()`'s string-building - table markup, the
     row-actions dropdown (`@wa-select=${...}` inline binding instead of
     the current `querySelectorAll(...).addEventListener(...)` tail),
     "Add record" button. Since lit-html auto-escapes all `${}` text/
     attribute bindings, the current hand-rolled `escapeHtml()` helper can
     be deleted entirely (verify: confirm no call site needs raw/unescaped
     HTML - none do today; do NOT introduce any `unsafeHTML` usage without
     explicit justification, since that would reintroduce the exact class
     of XSS risk `escapeHtml()` existed to prevent).
   - All other methods (`_visibleFields`, `_formatValue`, `_renderCell`,
     `_loadData`, `_validateConfig`, `_resolveImages`, `_handleSubmit`,
     `_rowActions`, `_confirmDeleteRecord`, `getCardSize`,
     `static getStubConfig`) carry over as plain methods/statics, unchanged
     in logic - only their trigger points change (state assignment instead
     of state assignment + manual `_render()`).
4. **Decision needed before starting**: the add-record and confirm dialogs
   are deliberately appended to `document.body` (not this element's own
   shadow root) to escape Lovelace masonry-grid clipping - recommend
   converting each into its own tiny dedicated LitElement
   (`frontend/src/add-record-dialog.ts`, `frontend/src/confirm-dialog.ts`),
   still `document.body`-appended, but with reactive `render()` templates
   instead of the current `innerHTML` string-building + manual
   `querySelectorAll` listener wiring. This is more consistent with the
   "real Lit" goal but is the single biggest chunk of net-new code in this
   phase - flag if you'd rather keep the dialogs as-is (plain vanilla DOM
   builders, unchanged) for a smaller first pass.
5. `frontend/src/custom-records-card-editor.ts` - `CustomRecordsCardEditor`
   as a LitElement: `<ha-form .hass=${...} .schema=${...} .data=${...}>`
   declarative bindings replace the current imperative `form.hass = ...`/
   `form.schema = ...` reuse-the-same-DOM-node workaround (Lit's diffing
   naturally reuses the same element across renders at a stable template
   position, so the focus-stealing workaround comment can be deleted). The
   columns picker's up/down/remove/add list becomes a `repeat()`-keyed
   template instead of hand-rolled `innerHTML` + listener wiring.
6. `frontend/src/register.ts` - the entry point: keeps the EXACT same
   `customElements.whenDefined("home-assistant")` bootstrap-race guard from
   today (this is orthogonal to Lit vs. vanilla - still needed), imports
   the above, calls `customElements.define(...)` + `window.customCards.push(...)`.
7. Builds to the SAME `custom_components/custom_records/www/custom-records-card.js`
   path/filename - `frontend.py`'s `CARD_URL_PATH`/`_card_version_hash()`
   need no changes in this phase (Phase 3 parameterizes them for multiple
   files).

**Verification**:
- Manual smoke test in a real/dev HA instance (`scripts/develop`): table
  renders, add-record dialog opens/submits, delete-with-confirmation works,
  a second browser tab's live update (WS `custom_records_updated` event)
  still triggers a re-render, the visual editor's columns picker still
  reorders/adds/removes correctly.
- Specifically exercise the `useDefineForClassFields` gotcha: change a
  `@state()` value from a test/dev console and confirm the DOM actually
  updates (catches silent reactivity breakage the type-checker won't).
- Existing `tests/test_frontend.py` (registration-only) still passes
  unchanged.

---

## Phase 3 - Multi-card registry + Options Flow + conditional injection (*depends on 1, independent of 2*)

1. `const.py` - add a small `CARD_REGISTRY` (id/filename/display name) with
   exactly one entry today (`custom-records-card`), and
   `CONF_ENABLED_CARDS = "enabled_cards"` for the options key.
2. `frontend.py` - split into two functions:
   - `async_register_static_paths(hass)` - hass-wide, register-once-ever
     (keep the existing `_FRONTEND_REGISTERED_KEY`-style guard - **must**
     stay strictly once-ever, since re-registering the same
     `async_register_static_paths` path a second time is expected to raise
     via aiohttp's router). Iterates `CARD_REGISTRY`, registers a static
     path per file regardless of enabled state (serving a file is harmless;
     only injection matters). Move the call site from `async_setup_entry`
     to the hass-wide `async_setup` in `__init__.py` (static serving isn't
     entry-specific).
   - `async_sync_enabled_cards(hass, entry)` - entry-aware; computes
     `enabled = set(entry.options.get(CONF_ENABLED_CARDS, [c.id for c in CARD_REGISTRY]))`
     (**default-all-enabled when the option is absent** - critical for
     existing installs upgrading with no `enabled_cards` option yet, so the
     card doesn't silently disappear from every dashboard after an
     upgrade). For each registry card: `add_extra_js_url` if enabled, else
     `remove_extra_js_url`. Confirmed safe to call unconditionally every
     time (verified `UrlManager.add`/`.remove` in HA core are frozenset
     union/difference ops - idempotent, no error if already-present/absent).
   - `async_unregister_all_cards(hass)` - calls `remove_extra_js_url` for
     every registry entry unconditionally; wire into both
     `async_unload_entry` and `async_remove_entry` in `__init__.py`. This
     fixes a **latent existing gap**: today, nothing ever calls
     `remove_extra_js_url`, so uninstalling the integration currently leaves
     the card injected into every dashboard until HA is restarted.
3. `__init__.py` - call `async_register_static_paths(hass)` from
   `async_setup`; call `async_sync_enabled_cards(hass, entry)` from
   `async_setup_entry` (replacing the old unconditional
   `async_register_frontend(hass)` call). **No new update listener needed**:
   the existing `_async_update_listener` (registered via
   `entry.add_update_listener`) already triggers a full
   `hass.config_entries.async_reload()` on ANY `entry.options` change,
   including from the new Options Flow below - reload naturally re-runs
   `async_setup_entry` with the fresh options, so the sync function just
   needs to be correct per-call, no diffing against "previous state" needed.
4. `config_flow.py` - add `async_get_options_flow` classmethod to
   `CustomRecordsConfigFlow` + new `CustomRecordsOptionsFlow(config_entries.OptionsFlow)`:
   one step (`async_step_init`) with a
   `selector.SelectSelector(selector.SelectSelectorConfig(multiple=True, options=[... from CARD_REGISTRY ...]))`,
   pre-filled with the entry's current enabled set (default-all, same as
   above), saved via `self.async_create_entry(data={CONF_ENABLED_CARDS: chosen})`.
   Surfaces as the "Configure" (gear icon) button on the integration's entry
   card - a different UI element from the non-extensible 3-dot "System
   options" menu.
5. `strings.json` / `translations/en.json` - new `options.step.init` section
   (label/description for the multi-select), matching the existing
   `config`/`config_subentries` sections' structure and translation-key
   conventions already used for `edit_select_options` etc.
6. `README.md` - add a compact, user-facing note: Settings -> Devices &
   Services -> Custom Records -> **Configure** now lets you choose
   which built-in cards are registered with the frontend.

**Verification**:
- `tests/test_frontend.py`: rewrite/extend for entry-awareness - default-
  all-enabled for an entry with no `enabled_cards` option (upgrade path),
  toggling a card off removes its URL from
  `hass.data[DATA_EXTRA_MODULE_URL].urls`, static paths register exactly
  once even across a reload (no aiohttp duplicate-route error),
  `async_unload_entry`/`async_remove_entry` remove all injected URLs.
- `tests/test_config_flow.py`: new Options Flow coverage - initial defaults,
  saving a reduced selection, re-opening reflects the saved selection.
- Manual: toggle a card off via Configure, refresh a dashboard tab, confirm
  the `<script>` tag for it is gone from the page source; confirm an
  already-open tab (not refreshed) keeps working until its own next reload
  (expected/documented limitation, not a bug).

---

## Build/bundling & release strategy (how/when the build actually happens)

Investigated exactly how HACS fetches integration code, since it directly
determines whether the built JS must be committed. Read HACS's own source
(`hacs/integration` repo, `repositories/base.py` + `repositories/integration.py`)
directly rather than guess:

- **Default behavior (no `zip_release`)**: HACS reads the git TREE directly
  via GitHub's API at whatever ref the user selected (a release tag, or the
  default branch if no release is picked/exists) - i.e. `download_content()`
  walks `self.tree` (a live `git/trees` API call) and downloads each file's
  raw content individually. Nothing is ever built server-side - whatever is
  literally committed at that ref is what gets installed. This means: if we
  don't commit the built JS, users installing this way would get a repo
  with NO working card at all (source `.ts` files aren't a valid Lovelace
  resource).
- **`zip_release: true` + `filename: "<name>.zip"` in hacs.json**
  (confirmed "only supported for integrations" per docs, and we ARE an
  integration): HACS instead calls `download_zip_files()` ->
  `async_download_zip_file()`, which downloads ONE SPECIFIC GitHub Release
  **asset** (matched by exact filename) via `github_release_asset(...)`,
  then does `zip_file.extractall(self.content.path.local)` where
  `content.path.local` is already `.../custom_components/custom_records`
  - **no path-stripping** (unlike the plain-archive fallback path, which
  does strip a wrapping folder). So the release zip's internal paths must
  be exactly `__init__.py`, `manifest.json`, `www/custom-records-card.js`,
  etc. AT THE ZIP ROOT - build it via
  `cd custom_components/custom_records && zip -r ../../custom_records.zip .`
  (not zipping the parent dir, which would nest everything one level too
  deep).
- **This is exactly the mechanism to avoid committing the build**: source
  (`frontend/src/*.ts`) stays in git; the compiled
  `custom_components/custom_records/www/*.js` is `.gitignore`d entirely,
  never committed to any branch. A NEW GitHub Actions workflow
  (`.github/workflows/release.yml`, `on: release: {types: [published]}`)
  builds the frontend (`npm ci && npm run build` inside `frontend/`) against
  the just-tagged commit, zips `custom_components/custom_records/` (built
  JS now included) at zip-root, and uploads it as a release asset via
  e.g. `softprops/action-gh-release`, with the asset filename matching
  hacs.json's `filename` exactly.
- **Important trade-off to accept**: `should_try_releases`
  (repositories/base.py) shows that once `zip_release: true` is set,
  installing/tracking the plain default branch STOPS being a viable HACS
  install path for regular users (there's no release asset to fetch for
  "main") - users effectively must install a tagged release from now on.
  Given hacs.json today has no `hide_default_branch` set and the repo
  otherwise supports default-branch tracking, this is a real behavior
  change worth being intentional about (acceptable/expected once a project
  has a build step - this is the standard trade-off every HACS integration
  with bundled frontend assets makes).

### 1. Local dev/testing
`scripts/develop` runs `hass` directly against the working-tree
`custom_components/custom_records`, NOT a HACS install - entirely
independent of the release/zip mechanism below. Since the built JS is
gitignored, `scripts/setup` runs the frontend build once
(`cd frontend && npm ci && npm run build`) so a fresh on-disk
`www/custom-records-card.js` exists locally before `scripts/develop` starts
HA. Re-run the build (or add a `--watch` dev script) after editing TS
source, before reloading the card in the browser.

### 2. Release process
- `custom_components/custom_records/www/*.js` -> `.gitignore`d entirely,
  never committed on any branch.
- `hacs.json` gets `"zip_release": true, "filename": "custom_records.zip"`.
- New `.github/workflows/release.yml`, triggered on
  `release: {types: [published]}`:
  1. Checkout (defaults to the tagged commit).
  2. `cd frontend && npm ci && npm run build`.
  3. `cd custom_components/custom_records && zip -r ../../custom_records.zip .`
     (zip root = integration dir contents directly - critical, since HACS
     does no path-stripping for `zip_release`).
  4. Upload `custom_records.zip` as a release asset (e.g. via
     `softprops/action-gh-release`), name matching hacs.json's `filename`
     exactly.
- `.github/workflows/lint.yml` gets a plain Node job
  (`npm ci && npm run typecheck && npm run build`) on every PR - just
  verifying the build succeeds, nothing committed/compared - catches
  breakage before a release is ever attempted.

---

## Corner cases & blocking-issue investigation (resolved during research)

- **Upgrade path default**: MUST default to all-enabled when
  `enabled_cards` is absent from `entry.options` - otherwise every existing
  install's dashboards silently lose the card on upgrade. (Handled in
  Phase 3.2/3.4.)
- **Safe Mode**: HA's `IndexView.get()` already zeroes out ALL extra
  modules (ours included) whenever `hass.config.safe_mode` is true,
  regardless of our settings - expected behavior, nothing to build for it,
  just don't be surprised the card "disappears" while troubleshooting in
  Safe Mode.
- **Static path re-registration**: verified `async_register_static_paths`
  must remain a strict once-ever (hass-wide) guard, decoupled from the
  add/remove injection logic which is safe to re-run on every setup/reload.
- **`UrlManager.add`/`.remove` are provably idempotent** (read HA core
  source directly: both are frozenset union/difference operations) - no
  KeyError/duplicate-error risk from calling either unconditionally every
  sync, confirmed NOT an internal/unsupported API (both `add_extra_js_url`
  and `remove_extra_js_url` are public, documented functions).
- **Live effect, no restart needed**: `IndexView.get()` reads
  `hass.data[DATA_EXTRA_MODULE_URL].urls` fresh on every page request (the
  `@lru_cache(maxsize=1)` is keyed on that frozenset) - a toggle takes
  effect on the very next full page load/refresh. Already-open tabs keep
  whatever was injected at their own last load until refreshed - unavoidable,
  document as expected.
- **Uninstall cleanup gap**: found and will fix a latent existing bug -
  nothing today calls `remove_extra_js_url`, so removing the integration
  currently leaves the card injected forever (until HA restart). Fixed via
  `async_unregister_all_cards` wired into unload/remove.
- **Global (not per-dashboard) injection scope**: `add_extra_js_url` affects
  the WHOLE frontend, not a specific dashboard/view - an enabled card still
  loads on every page even where unused. This is an inherent architecture
  limit (no per-view lazy loading available), not something Phase 3 can
  fix - the on/off toggle is still strictly better than "always inject
  everything," just not fully lazy.
- **Built-bundle drift risk**: N/A now that the build output isn't
  committed at all (nothing to drift out of sync with) - superseded by the
  "Build/bundling & release strategy" section above, whose actual safety
  net is (a) a per-PR "does the build succeed" CI job and (b) the release
  workflow itself failing loudly if the build breaks.
- **Lit+TS decorator gotcha**: `useDefineForClassFields: false` in
  `tsconfig.json` is mandatory or `@property()`/`@state()` reactivity
  breaks silently at runtime with zero compile-time error - covered by an
  explicit manual smoke-test step in Phase 2 Verification, not just relying
  on `tsc` succeeding.
- **XSS/escaping regression risk**: removing the manual `escapeHtml()`
  helper is safe/intentional (lit-html auto-escapes `${}` bindings) as long
  as no `unsafeHTML`/raw-`innerHTML` usage is introduced without
  justification - explicit checklist item in Phase 2.
- **`plan_sql.md`**: not applicable - this feature isn't part of that SQL
  migration plan, no update needed there (confirmed, not overlooked).

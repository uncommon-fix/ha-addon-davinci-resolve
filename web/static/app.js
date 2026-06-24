// DaVinci Resolve Postgres addon — Alpine.js component.
//
// Single-page UI. State lives in `davinciApp` (registered via the
// canonical alpine:init event). All HTTP calls go through `api(method,
// path, body)` which adds the X-Session-Id (when claimed) + X-Addon-Version
// (always) headers and decodes the standard `{error: "..."}` shape.

const ingressMeta = document.querySelector('meta[name="ingress-path"]');
const INGRESS_PATH = (ingressMeta && ingressMeta.content) || '';
const appVersionMeta = document.querySelector('meta[name="app-version"]');
const APP_VERSION = (appVersionMeta && appVersionMeta.content) || '';

// Mirror of backend pg_admin.LIBRARY_NAME_RE for client-side gating of
// the Create modal's Submit button. Server re-validates regardless.
const LIB_NAME_RE = /^[a-z][a-z0-9_-]{2,31}$/;
const RESERVED_LIB_NAMES = new Set([
    'postgres', 'pg_signal_backend', 'pg_read_all_data', 'pg_write_all_data',
    'pg_read_server_files', 'pg_write_server_files', 'pg_execute_server_program',
    'pg_monitor', 'pg_read_all_settings', 'pg_read_all_stats',
    'pg_stat_scan_tables', 'admin', 'root', 'template', 'template0', 'template1',
]);

function davinciAppData() {
    return {
        ingressPath: INGRESS_PATH,
        appVersion: APP_VERSION,

        // Wire state
        libraries: [],
        state: {
            pg_up: false, pg_version: '', library_count: 0,
            host_hint: '', port: 5432,
            // alpha.6: traefik addon detection. {installed: bool, slug?: str,
            // version?: str}; populated by /api/admin/state poll.
            traefik: { installed: false },
        },
        // alpha.6: dismiss the traefik banner per-session (and persist via
        // localStorage so it stays dismissed across reloads). The user can
        // re-open the banner by clearing the storage key — intentionally
        // not adding a "re-show" UI for v1, keep it simple.
        traefikBannerDismissed: (() => {
            try { return localStorage.getItem('davinci:traefikBannerDismissed') === '1'; }
            catch (_) { return false; }
        })(),
        loading: true,
        busy: false,                   // in-flight mutation

        // Session model
        sid: '',
        viewMode: 'rw',
        takeoverPrompt: { visible: false, age: 0 },

        // Toasts
        toasts: [],
        _toastUid: 0,

        // Modals
        createModal: {
            open: false,
            stage: 'composing',         // 'composing' | 'credentials'
            name: '',
            nameError: '',
            result: null,
            showPassword: false,
            get canSubmit() { return !davinciApp_thisRef.createModal.nameError && davinciApp_thisRef.createModal.name.length >= 3; },
        },
        deleteModal: { open: false, name: '', typed: '' },
        resetModal: {
            open: false,
            stage: 'confirm',           // 'confirm' | 'credentials'
            name: '',
            result: null,
            showPassword: false,
        },

        // -------------------- lifecycle --------------------

        async load() {
            // 1. Claim the editor session. 409 -> takeover prompt; the read
            //    surfaces still load so the user sees state.
            await this.claimSession();
            await this.refresh();
            this.loading = false;
            // Background poll of /api/admin/state every 5s so the header
            // status strip stays live (pg_up flips back if PG crashes etc.).
            if (!this._statusTimer) {
                this._statusTimer = setInterval(() => this.refreshState(), 5000);
            }
        },

        async refresh() {
            await this.refreshState();
            await this.refreshLibraries();
        },

        async refreshState() {
            try {
                const r = await fetch(this.url('/api/admin/state'));
                if (r.ok) this.state = await r.json();
            } catch (_) { /* polled; tolerate transient errors */ }
        },

        async refreshLibraries() {
            try {
                const r = await fetch(this.url('/api/libraries'));
                if (!r.ok) {
                    const j = await r.json().catch(() => ({}));
                    throw new Error(j.error || `GET /api/libraries -> ${r.status}`);
                }
                const j = await r.json();
                this.libraries = (j.libraries || []).slice().sort((a, b) => a.name.localeCompare(b.name));
            } catch (e) {
                this.toast.error(`Couldn't load libraries: ${e.message}`);
            }
        },

        url(path) {
            return (this.ingressPath || '') + path;
        },

        // -------------------- api wrapper --------------------

        async api(method, path, body) {
            const headers = { 'X-Addon-Version': this.appVersion || '' };
            if (body !== undefined) headers['Content-Type'] = 'application/json';
            if (this.sid && method !== 'GET') headers['X-Session-Id'] = this.sid;
            const r = await fetch(this.url(path), {
                method,
                headers,
                body: body !== undefined ? JSON.stringify(body) : undefined,
            });
            if (r.status === 423) {
                this.viewMode = 'ro';
                const err = new Error('Your session was taken over from another tab.');
                err.code = 'SESSION_LOST';
                throw err;
            }
            if (r.status === 409) {
                const j = await r.json().catch(() => ({}));
                if (j.code === 'VERSION_MISMATCH') {
                    const err = new Error(j.error || 'Addon was updated — reload to continue.');
                    err.code = 'VERSION_MISMATCH';
                    throw err;
                }
                throw new Error(j.error || 'HTTP 409');
            }
            if (!r.ok) {
                const j = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
                throw new Error(j.error || `${method} ${path} -> ${r.status}`);
            }
            return r.status === 204 ? null : await r.json();
        },

        // -------------------- session --------------------

        async claimSession() {
            try {
                const r = await fetch(this.url('/api/session/claim'), { method: 'POST' });
                if (r.status === 409) {
                    const j = await r.json().catch(() => ({}));
                    this.takeoverPrompt = { visible: true, age: Math.max(0, Math.round(j.current_age_s || 0)) };
                    this.viewMode = 'ro';
                    return false;
                }
                if (!r.ok) {
                    this.toast.error(`Couldn't claim editor session: HTTP ${r.status}`);
                    return false;
                }
                const j = await r.json();
                this.sid = j.sid;
                this.viewMode = 'rw';
                return true;
            } catch (e) {
                this.toast.error(`Session claim failed: ${e.message}`);
                return false;
            }
        },

        async takeoverSession() {
            try {
                const r = await fetch(this.url('/api/session/takeover'), { method: 'POST' });
                if (!r.ok) {
                    this.toast.error('Takeover failed.');
                    return;
                }
                const j = await r.json();
                this.sid = j.sid;
                this.viewMode = 'rw';
                this.takeoverPrompt.visible = false;
                this.toast.success('Took over the editor session.');
            } catch (e) {
                this.toast.error(`Takeover failed: ${e.message}`);
            }
        },

        viewReadOnly() {
            this.takeoverPrompt.visible = false;
            this.viewMode = 'ro';
        },

        // -------------------- create flow --------------------

        openCreate() {
            this.createModal = {
                open: true,
                stage: 'composing',
                name: '',
                nameError: '',
                result: null,
                showPassword: false,
                // Computed canSubmit is bound via the get on the data object,
                // but Alpine's reactivity reads from this object so we mirror
                // the getter logic inline via the validate handler below.
                get canSubmit() {
                    return !this.nameError
                        && LIB_NAME_RE.test((this.name || '').trim())
                        && !RESERVED_LIB_NAMES.has((this.name || '').trim());
                },
            };
        },

        closeCreate() {
            // Refresh the library list when the user finishes the credentials
            // stage so the new entry appears immediately even if the polled
            // refresh hasn't fired yet.
            const wasCredentials = this.createModal.stage === 'credentials';
            this.createModal.open = false;
            if (wasCredentials) {
                this.refresh();
            }
        },

        _validateNewName(raw) {
            const v = (raw || '').trim();
            if (!v) return 'Required.';
            if (!LIB_NAME_RE.test(v)) {
                return 'Lowercase letter first, alphanumerics + _ - only, 3–32 chars.';
            }
            if (RESERVED_LIB_NAMES.has(v)) {
                return `Reserved name (${v}) — pick something else.`;
            }
            if (this.libraries.some(l => l.name === v)) {
                return `A library named ${v} already exists.`;
            }
            return '';
        },

        async submitCreate() {
            const name = (this.createModal.name || '').trim();
            const err = this._validateNewName(name);
            this.createModal.nameError = err;
            if (err) return;
            this.busy = true;
            try {
                const j = await this.api('POST', '/api/libraries', { name });
                this.createModal.result = j;
                this.createModal.stage = 'credentials';
                this.toast.success(`Library ${j.name} created.`);
            } catch (e) {
                if (e.code === 'VERSION_MISMATCH') {
                    this.toast.error('Addon was updated — please reload the page.');
                } else if (e.code !== 'SESSION_LOST') {
                    this.toast.error(`Couldn't create library: ${e.message}`);
                }
            } finally {
                this.busy = false;
            }
        },

        // -------------------- delete flow --------------------

        openDelete(name) {
            this.deleteModal = { open: true, name, typed: '' };
        },
        closeDelete() {
            this.deleteModal.open = false;
        },
        async submitDelete() {
            if (this.deleteModal.typed !== this.deleteModal.name) return;
            this.busy = true;
            try {
                await this.api('DELETE', '/api/libraries/' + encodeURIComponent(this.deleteModal.name));
                this.toast.success(`Library ${this.deleteModal.name} deleted.`);
                this.deleteModal.open = false;
                await this.refresh();
            } catch (e) {
                if (e.code === 'VERSION_MISMATCH') {
                    this.toast.error('Addon was updated — please reload the page.');
                } else if (e.code !== 'SESSION_LOST') {
                    this.toast.error(`Couldn't delete library: ${e.message}`);
                }
            } finally {
                this.busy = false;
            }
        },

        // -------------------- reset password flow --------------------

        openResetPassword(name) {
            this.resetModal = {
                open: true,
                stage: 'confirm',
                name,
                result: null,
                showPassword: false,
            };
        },
        closeReset() {
            this.resetModal.open = false;
        },
        async submitReset() {
            const name = this.resetModal.name;
            this.busy = true;
            try {
                const j = await this.api('POST', '/api/libraries/' + encodeURIComponent(name) + '/reset-password');
                this.resetModal.result = j;
                this.resetModal.stage = 'credentials';
                this.toast.success(`New password issued for ${name}.`);
            } catch (e) {
                if (e.code === 'VERSION_MISMATCH') {
                    this.toast.error('Addon was updated — please reload the page.');
                } else if (e.code !== 'SESSION_LOST') {
                    this.toast.error(`Couldn't reset password: ${e.message}`);
                }
            } finally {
                this.busy = false;
            }
        },

        // -------------------- helpers --------------------

        // alpha.6/.7: traefik integration banner helpers.
        get showTraefikBanner() {
            return !!(this.state && this.state.traefik && this.state.traefik.installed)
                && !this.traefikBannerDismissed;
        },
        dismissTraefikBanner() {
            this.traefikBannerDismissed = true;
            try { localStorage.setItem('davinci:traefikBannerDismissed', '1'); }
            catch (_) { /* private mode / quota — drop quietly */ }
        },
        // Build the HA path the user navigates to for the Traefik addon's
        // own ingress UI. HA's addon info page lives at /hassio/addon/<slug>;
        // the ingress UI is /hassio/ingress/<slug>. Either is fine — info
        // gives the user a clear landing with an "Open Web UI" button.
        traefikAddonUrl() {
            const slug = (this.state.traefik && this.state.traefik.slug) || 'local_traefik';
            return `/hassio/addon/${slug}/info`;
        },

        // alpha.7: scaffold-in-traefik state machine. Three possible UI
        // states for the banner action area:
        //   'idle'      — initial; show "Create scaffold" button
        //   'creating'  — in-flight; spinner; buttons disabled
        //   'created'   — success; show "Open Traefik to Apply" instead
        // Errors go to the toast queue + return to 'idle' so the user
        // can retry. Per-session state — we don't persist this since the
        // user might re-create after deleting in Traefik.
        scaffoldState: 'idle',
        scaffoldResult: null,        // {rid, name, message, traefik_slug}

        async createTraefikScaffold() {
            if (this.scaffoldState !== 'idle') return;
            if (this.viewMode === 'ro') {
                this.toast.error('Read-only — take over the session first.');
                return;
            }
            this.scaffoldState = 'creating';
            try {
                const j = await this.api('POST', '/api/admin/scaffold-traefik-route');
                this.scaffoldResult = j;
                this.scaffoldState = 'created';
                this.toast.success(
                    `Route '${j.name}' scaffolded in Traefik's draft. Open Traefik and click Apply to publish.`
                );
            } catch (e) {
                this.scaffoldState = 'idle';
                if (e.code === 'VERSION_MISMATCH') {
                    this.toast.error('Addon was updated — reload to continue.');
                } else if (e.code !== 'SESSION_LOST') {
                    this.toast.error(`Couldn't scaffold route in Traefik: ${e.message}`);
                }
            }
        },

        formatCreated(iso) {
            if (!iso) return '?';
            try {
                const d = new Date(iso);
                // Display in the user's local time; YYYY-MM-DD only (the
                // exact second of creation isn't meaningful to the user).
                return d.toISOString().slice(0, 10);
            } catch (_) {
                return iso;
            }
        },

        async copyToClipboard(text) {
            try {
                await navigator.clipboard.writeText(text);
                this.toast.success('Copied to clipboard.');
            } catch (_) {
                this.toast.error('Clipboard blocked. Select the text manually.');
            }
        },

        copyConnectionBlock(result) {
            // The block matches the order of the on-screen card so the user
            // can paste into a notes app for reference. Lines are
            // "Field: value" so the meaning is preserved out of context.
            const block = [
                `Database type:  PostgreSQL`,
                `Server name:    ${result.host_hint}`,
                `Port:           ${result.port}`,
                `Database:       ${result.db}`,
                `Username:       ${result.user}`,
                `Password:       ${result.password}`,
            ].join('\n');
            this.copyToClipboard(block);
        },

        // -------------------- toast queue --------------------

        _pushToast({ kind, text, sticky = false, ttl = 4000 }) {
            const id = ++this._toastUid;
            this.toasts.push({ id, kind, text, sticky });
            if (!sticky) setTimeout(() => this._dismissToast(id), ttl);
            return id;
        },
        _dismissToast(id) {
            const i = this.toasts.findIndex(t => t.id === id);
            if (i >= 0) this.toasts.splice(i, 1);
        },
        get toast() {
            return {
                success: (text, opts = {}) => this._pushToast({ kind: 'success', text, ...opts }),
                info:    (text, opts = {}) => this._pushToast({ kind: 'info',    text, ...opts }),
                // Errors stick by default; user must dismiss explicitly.
                error:   (text, opts = {}) => this._pushToast({ kind: 'error',   text, sticky: true, ...opts }),
            };
        },
    };
}

// The createModal block above uses `davinciApp_thisRef` for an inline
// `canSubmit` getter to avoid an Alpine reactivity quirk where `this`
// inside a nested object's getter doesn't resolve to the component scope.
// Bind it on alpine:init below.
let davinciApp_thisRef = null;

document.addEventListener('alpine:init', () => {
    window.Alpine.data('davinciApp', () => {
        const data = davinciAppData();
        davinciApp_thisRef = data;
        return data;
    });
});

// Back-compat for any cached HTML that uses x-data="davinciApp()".
window.davinciApp = davinciAppData;

# SignalDesk frontend

React account/project UI for Phase 3. GitHub OAuth signs in through the backend; there is no demo login or authentication bypass. Sign-in errors use safe messages. The app fetches `/api/v1/session`, uses its CSRF token for mutations, and sends same-origin cookies. Owned projects support creation, editing, archive/restore, server-side paging, and an include-archived filter.

Version conflicts display the latest authorized project beside the preserved draft. Explicitly accept the latest version before resubmitting. Transport retries with unchanged payloads reuse an idempotency key; changed payloads get new keys.

Run `npm ci`, then `npm run dev` with Node 22. Vite proxies `/api` and `/health` to `http://127.0.0.1:8000` (override `API_PROXY_TARGET` as needed). Backend APP_ORIGIN must match the browser origin, typically `http://127.0.0.1:5173`. Configure GitHub OAuth credentials/callback before sign-in; missing configuration yields a setup message, never a substitute identity. See the root README for backend/database setup.

Checks: `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`. Mocked API contract tests cover security headers, conflict data, session/network failures, safe OAuth error copy, and retry identity. Browser/live OAuth verification is separate and recorded by the lead/QA.

Public GitHub repository connections and manual bounded collection are implemented in Phase 4. Full review decisions and automatic scheduling/retries are later work. `../prototype` remains a separate design artifact. Production hosting must route `/api` to the backend and serve the SPA for `/projects` and `/sign-in` callback redirects.

Requests have a 15-second timeout. Uncertain timeouts keep their idempotency key. Unsaved drafts stay only in memory on session expiry: sign-in opens a separate tab, then Retry session check restores the original tab. Changing accounts clears previous private UI state. Archived projects must be restored before editing. OAuth callback query strings are removed from the SPA URL after mapping the safe error message.

Cross-tab protection: focus/visibility changes revalidate session while hiding private UI. Request generations discard stale responses. Project page responses include server-derived account_id; mismatches are never rendered with the old identity. Mutations retain session-bound CSRF, so an old draft cannot be submitted under another account's cookie. Same-account revalidation retains draft text; account changes clear it. Readiness uses database_schema scope in Phase 3.

## Phase 4 source workspace

Open Sources & releases on an owned project. Add a public GitHub repository using owner/repository or its HTTPS GitHub URL, with optional prereleases. The server validates/canonicalizes it. Per-source controls save the prerelease preference or pause/resume/remove the subscription with expected-version and idempotency protection. Archived projects expose history only. Removed sources can be included in the list and re-added without discarding history.

Check now requests a durable manual run. The UI polls visible active runs every four seconds, displays provider cooldown/Retry-After, and pages both run and release history. Every scan is bounded to the first 30 entries returned by GitHub, before prerelease filtering; no complete archive or scheduled next-check time is claimed. Pending, connected, attention, partial, failed, empty and unchanged results remain distinct. Release detail is escaped plain text; remote images/scripts are never executed, and original links are restricted to public HTTPS github.com destinations.

All source lists, release detail, and mutations verify server-derived account_id against the verified session and current generation before updating UI. Source draft state remains in memory while session checks hide the component, and is discarded on account change or explicit sign-out. Conflict responses show latest subscription state and preserve an edited preference until the user explicitly reloads the version and retries. Uncertain transport retries keep the same mutation key.

Phase 4 tests cover source/check gating, cooldown metadata, safe link destinations, and actual server-rendered release markup escaping, in addition to session/idempotency contracts. They do not establish live GitHub integration or mounted-component browser behavior; lead/QA record those checks separately.

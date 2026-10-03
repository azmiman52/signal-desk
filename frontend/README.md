# SignalDesk frontend

React account/project UI for Phase 3. GitHub OAuth signs in through the backend; there is no demo login or authentication bypass. Sign-in errors use safe messages. The app fetches `/api/v1/session`, uses its CSRF token for mutations, and sends same-origin cookies. Owned projects support creation, editing, archive/restore, server-side paging, and an include-archived filter.

Version conflicts display the latest authorized project beside the preserved draft. Explicitly accept the latest version before resubmitting. Transport retries with unchanged payloads reuse an idempotency key; changed payloads get new keys.

Run `npm ci`, then `npm run dev` with Node 22. Vite proxies `/api` and `/health` to `http://127.0.0.1:8000` (override `API_PROXY_TARGET` as needed). Backend APP_ORIGIN must match the browser origin, typically `http://127.0.0.1:5173`. Configure GitHub OAuth credentials/callback before sign-in; missing configuration yields a setup message, never a substitute identity. See the root README for backend/database setup.

Checks: `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`. Mocked API contract tests cover security headers, conflict data, session/network failures, safe OAuth error copy, and retry identity. Browser/live OAuth verification is separate and recorded by the lead/QA.

Repository connections, release collection, and review workflows are not implemented. `../prototype` remains a separate design artifact. Production hosting must route `/api` to the backend and serve the SPA for `/projects` and `/sign-in` callback redirects.

Requests have a 15-second timeout. Uncertain timeouts keep their idempotency key. Unsaved drafts stay only in memory on session expiry: sign-in opens a separate tab, then Retry session check restores the original tab. Changing accounts clears previous private UI state. Archived projects must be restored before editing. OAuth callback query strings are removed from the SPA URL after mapping the safe error message.

Cross-tab protection: focus/visibility changes revalidate session while hiding private UI. Request generations discard stale responses. Project page responses include server-derived account_id; mismatches are never rendered with the old identity. Mutations retain session-bound CSRF, so an old draft cannot be submitted under another account's cookie. Same-account revalidation retains draft text; account changes clear it. Readiness uses database_schema scope in Phase 3.

# SignalDesk browser foundation

React 19, TypeScript 6, Vite 8. Node 22.12+ within the Node 22 line. Exact dependencies and transitive integrity are in package.json/package-lock.json.

```sh
npm ci
npm run dev
npm run lint
npm run typecheck
npm test
npm run build
```

Development listens on loopback port 5173 and proxies `/health` to `http://127.0.0.1:8000`. Set the process environment `API_PROXY_TARGET` if the backend uses a different local address. Root Compose uses nginx to serve built assets with same-origin health routing. `npm run preview` previews assets only; it does not provide that backend proxy.

The page checks the real readiness endpoint with a five-second client deadline and shows connected, database unavailable, or API check failed. It labels business features as unfinished. It has no sample inbox or fabricated successful collection.

TypeScript 6.0.3 was selected because typescript-eslint 8.71.0 declares TypeScript <6.1 support; TypeScript 7 was rejected by npm's peer checks. [Vite requirements](https://vite.dev/guide/) support the chosen Node line. ESLint is a separate lint gate, not an alias of type checking.

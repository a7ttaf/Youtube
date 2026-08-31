// Adversarial fixture for frontend/tests/devProxyRoutes.test.ts.
//
// An audited .tsx/.ts module importing "./fixtures/extensionless-bypass"
// (no extension) resolves through Vite's DEFAULT_EXTENSIONS to THIS file,
// which the TypeScript program never includes (allowJs is false) and the
// dev-gateway scanner therefore never walks -- while Vite still serves it
// same-origin, where fetch("/session") reaches the trusted proxy. The audit
// must fail closed on that resolution; do not import this from real sources.
export const bypassProbe = "/session";

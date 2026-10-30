// K-066: allow-same-origin lets a previewed app call its own server (fetch, storage). It stays a different origin
// from the panel (another port) and the panel refuses to be framed (frame-ancestors 'none'), so a frame redirected
// to the panel cannot escape the sandbox. No allow-top-navigation or popups-to-escape-sandbox.
export const PREVIEW_IFRAME_SANDBOX = 'allow-scripts allow-forms allow-same-origin'

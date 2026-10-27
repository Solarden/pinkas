# Vendored files

Served by the app itself (CSP `script-src 'self'`), never from a CDN.

| File | From | Version | Licence |
|---|---|---|---|
| `tiny-mde.tiny.js` | npm `tiny-markdown-editor`, `dist/tiny-mde.tiny.js` (the build without its own toolbar) | 0.2.35 | MIT, `LICENSE-tiny-mde.txt` |

The guide editor's in-place formatting. Its toolbar is ours (`static/pinkas.js`): the library's buttons are
`div`s a keyboard can't reach. Our toolbar calls `setCommandState` and `restoreLastSelection`, which the library's
own toolbar uses but its README doesn't list: check both after any update.

Checked when vendored: the npm tarball's sha512 matched the registry's `dist.integrity`
(`sha512-H2IJYBl9gfElF9ovYOr3SVRMtjWmE3OAMm7IjgARnZ4tJq8xDCOv6TIomc7+Op5izEzRT/HVi16wpqxnXXHldw==`), and the file's
sha256 is `79f30c051f17654f7c4ed61ead24c242a365a346376dcb083bd98539153a0bea`.

To update: download the new tarball, compare its sha512 with `npm view tiny-markdown-editor@<v> dist.integrity`,
copy `dist/tiny-mde.tiny.js`, update the version and both hashes here.

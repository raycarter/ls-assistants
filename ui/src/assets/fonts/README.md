# Material Icons

`material-icons.woff2` is the locally bundled classic Material Icons font (version 145).
Icons use the `material-icons` CSS class and ligature names such as `add_comment`.
The application does not request Google Fonts at runtime.

- Font source: <https://fonts.gstatic.com/s/materialicons/v145/flUhRq6tzZclQEJ-Vdg-IuiaDsNc.woff2>
- Upstream project: <https://github.com/google/material-design-icons>
- License: Apache License 2.0; see `LICENSE.txt` in this directory.

The font is referenced by `src/styles.css`, so Angular bundles it for development and production.
The asset configuration also copies this directory, including its license, into `assets/fonts/`.

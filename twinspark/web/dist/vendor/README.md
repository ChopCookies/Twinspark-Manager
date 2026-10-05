# Vendored front-end libraries

These are shipped (unmodified apart from the removed source-map comment, see below) so the GUI works on a headless machine with no internet access.

| file | package | version | license |
|---|---|---|---|
| `xterm.js`, `xterm.css` | [@xterm/xterm](https://github.com/xtermjs/xterm.js) | 5.5.0 | MIT (`LICENSE-xterm.txt`) |
| `addon-fit.js` | @xterm/addon-fit | 0.10.0 | MIT (same licence text) |

Only the source-map comment at the end of each file was removed (the maps are not shipped).
To update: `npm pack @xterm/xterm@<v> @xterm/addon-fit@<v>` and copy `lib/*.js` and `css/xterm.css` here.

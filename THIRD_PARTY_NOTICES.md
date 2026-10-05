# Third-party notices

TwinSpark Manager is MIT-licensed (see [LICENSE](LICENSE)). It includes the following material from
other projects, under their own licences.

## eugr/spark-vllm-docker — MIT

`twinspark/cookbook/recipes/deepseek-v4-flash-0731-b12x.yaml` is a copy, taken on 2026-09-28, of
`recipes/deepseek-v4-flash-0731.yaml` from <https://github.com/eugr/spark-vllm-docker>.

TwinSpark also reads recipes and mods in that project's format at run time, and its two-subnet QSFP layout
follows that project's networking guide. The recipe file above is the only code or text copied from it.

```
MIT License

Copyright (c) 2026 Eugene Rakhmatulin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## xterm.js and @xterm/addon-fit — MIT

`twinspark/web/{src,dist}/vendor/xterm.js`, `xterm.css` and `addon-fit.js` are the builds of
[@xterm/xterm](https://github.com/xtermjs/xterm.js) 5.5.0 and @xterm/addon-fit 0.10.0, unmodified except
that the source-map comment at the end of the two `.js` files was removed. The MIT licence text with
xterm.js's copyright lines is in
[`twinspark/web/dist/vendor/LICENSE-xterm.txt`](twinspark/web/dist/vendor/LICENSE-xterm.txt);
@xterm/addon-fit carries the same licence, "Copyright (c) 2019, The xterm.js authors".

## Recipe settings from other community projects

The other built-in recipes (`twinspark/cookbook/recipes/*.json`) are TwinSpark's own files. They record
settings and measurements that their authors published, each with a link to the source in its `source`
field. These sources are seanlinmt, himorishige, tonyd2wild, getrefined, MiaAI-Lab and Hugging Face model
cards. No code or text from those projects is included.

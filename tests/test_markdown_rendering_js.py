"""Regression coverage for the browser markdown renderer."""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None


@pytest.fixture(scope="module")
def node_available():
    if not _HAS_NODE:
        pytest.skip("node binary not on PATH")


_PASS_THROUGH_DOM = """{
      readyState: 'loading',
      addEventListener() {},
      createElement(tag) {
        if (tag !== 'template') throw new Error(`unsupported element: ${tag}`);
        return {
          _html: '',
          content: { querySelectorAll() { return []; } },
          set innerHTML(value) { this._html = value; },
          get innerHTML() { return this._html; },
        };
      },
    }"""

# A tolerant-but-real HTML fragment DOM for the sanitizer: parses nested tags
# and attributes, honours remove()/removeAttribute(), and re-serializes the
# surviving tree - enough to exercise sanitizeAllowedHtml's fixpoint
# (reparse-after-serialize) behaviorally under Node without a browser. S9 adds
# the real-browser pass.
_SANITIZER_DOM = """(function makeTreeDom() {
const VOID = /^(area|base|br|col|embed|hr|img|input|link|meta|param|source|track|wbr)$/i;
function parse(html) {
  const root = { tag: null, children: [] };
  const stack = [root];
  const re = /<(\\/?)([A-Za-z][A-Za-z0-9:-]*)([^<>]*?)>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    if (m[4] !== undefined) { stack[stack.length - 1].children.push({ text: m[4] }); continue; }
    const closing = m[1] === '/';
    const tag = m[2];
    if (closing) {
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tag && stack[i].tag.toLowerCase() === tag.toLowerCase()) { stack.length = i; break; }
      }
      continue;
    }
    const selfClosing = /\\/\\s*$/.test(m[3]);
    const attrs = [];
    const ar = /([A-Za-z][A-Za-z0-9:._-]*)(?:\\s*=\\s*("([^"]*)"|'([^']*)'|([^\\s"'=<>`]+)))?/g;
    let am;
    while ((am = ar.exec(m[3] || ''))) {
      attrs.push({
        name: am[1],
        value: am[3] !== undefined ? am[3] : (am[4] !== undefined ? am[4] : (am[5] !== undefined ? am[5] : '')),
        dropped: false,
      });
    }
    const el = {
      tag, tagName: tag, attrs, attributes: attrs, children: [], removed: false,
      remove() { this.removed = true; },
      removeAttribute(name) { const a = this.attrs.find((x) => x.name === name); if (a) a.dropped = true; },
      // Descendant elements, for querySelectorAll('*') only.
      querySelectorAll() {
        const out = [];
        const walk = (nodes) => { for (const n of nodes) if (n.tag && !n.removed) { out.push(n); walk(n.children); } };
        walk(this.children);
        return out;
      },
    };
    stack[stack.length - 1].children.push(el);
    if (!selfClosing && !VOID.test(tag)) stack.push(el);
  }
  return root;
}
function serialize(nodes) {
  let out = '';
  for (const n of nodes) {
    if (n.text !== undefined) { out += n.text; continue; }
    if (n.removed) continue;
    const as = n.attrs.filter((a) => !a.dropped)
      .map((a) => a.name + '="' + String(a.value || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;') + '"')
      .join(' ');
    out += '<' + n.tag + (as ? ' ' + as : '') + '>' + serialize(n.children) + '</' + n.tag + '>';
  }
  return out;
}
function templateDoc(unstable) {
  return {
    readyState: 'loading',
    addEventListener() {},
    createElement(tag) {
      if (tag !== 'template') throw new Error('unsupported element: ' + tag);
      const tpl = {
        _root: { tag: null, children: [] },
        set innerHTML(v) { this._root = parse(v); },
        get innerHTML() {
          // The unstable variant grafts one more surviving element on every
          // serialization - the fixpoint can never converge, so the sanitizer
          // must fall back to escaping (the four-pass bound fails closed).
          const kids = unstable ? [...this._root.children, { tag: 'i', tagName: 'i', attrs: [], attributes: [], children: [], removed: false }] : this._root.children;
          return serialize(kids);
        },
        content: {
          _tpl: null,
          querySelectorAll(sel) {
            const names = sel.split(',').map((s) => s.trim().toLowerCase());
            const all = names.includes('*');
            const out = [];
            const walk = (nodes) => {
              for (const n of nodes) {
                if (n.tag && !n.removed) {
                  if (all || names.includes(n.tag.toLowerCase())) out.push(n);
                  walk(n.children);
                }
              }
            };
            walk(this._tpl._root.children);
            return out;
          },
        },
      };
      tpl.content._tpl = tpl;
      return tpl;
    },
  };
}
return { stable: templateDoc(false), unstable: templateDoc(true) };
})()"""


def _run_markdown_case(markdown: str, render_expr: str = "mod.mdToHtml(input)", with_katex: bool = False,
                       dom_stub: str = "", real_katex: bool = False):
    if not dom_stub:
        dom_stub = _PASS_THROUGH_DOM
    script = textwrap.dedent(
        r"""
        import fs from 'node:fs';

        globalThis.window = { location: { origin: 'http://localhost' }, katex: null };
        if (__WITH_KATEX__) {
          // Minimal stand-in for the CDN katex global: wraps the source so tests
          // can assert what was (or wasn't) handed to KaTeX.
          const katexStub = {
            renderToString(src, opts) {
              const display = !!(opts && opts.displayMode);
              return `<span class="katex" data-display="${display}">${src}</span>`;
            },
          };
          globalThis.window.katex = katexStub;
          globalThis.katex = katexStub;
        }
        if (__REAL_KATEX__) {
          // The vendored KaTeX itself, for tests about the markup it really
          // emits (its <svg> glyphs) rather than what reaches it.
          const { createRequire } = await import('node:module');
          const realKatex = createRequire(process.cwd() + '/')('./static/lib/katex/katex.min.js');
          globalThis.window.katex = realKatex;
          globalThis.katex = realKatex;
        }
        globalThis.document = __DOCUMENT_STUB__;
        globalThis.MutationObserver = class { observe() {} };

        let source = fs.readFileSync('./static/js/markdown.js', 'utf8');
        source = source.replace(
          /import uiModule from ['"]\.\/ui\.js['"];/,
          ''
        );
        source = source.replace(
          /import \{ splitTableRow \} from ['"]\.\/markdown\/tableRow\.js['"];/,
          `function splitTableRow(row) {
            return (row || '').replace(/^\\s*\\|/, '').replace(/\\|\\s*$/, '').split('|').map(c => c.trim());
          }`
        );
        // markdown.js imports the emoji-shortcode helpers relatively (issue #345),
        // which a data: URL module can't resolve. Inline the REAL helpers (minus
        // their export keywords) so the renderer's shortcode pass behaves exactly
        // as it does in the browser.
        const emojiSource = fs.readFileSync('./static/js/emojiShortcodes.js', 'utf8')
          .replace(/^export default .*$/m, '')
          .replace(/export const /g, 'const ')
          .replace(/export function /g, 'function ');
        source = source.replace(
          /import \{ replaceEmojiShortcodes, hasEmojiShortcode \} from ['"]\.\/emojiShortcodes\.js['"];/,
          () => emojiSource
        );
        source = source.replace(
          /var escapeHtml = uiModule\.esc;/,
          `var escapeHtml = (value) => String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');`
        );

        const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
        const mod = await import(moduleUrl);
        const input = JSON.parse(process.argv[1]);
        console.log(JSON.stringify({ html: __RENDER_EXPR__ }));
        """
    ).replace("__RENDER_EXPR__", render_expr).replace(
        "__WITH_KATEX__", "true" if with_katex else "false"
    ).replace(
        "__REAL_KATEX__", "true" if real_katex else "false"
    ).replace("__DOCUMENT_STUB__", dom_stub)
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(markdown)],
        cwd=_REPO,
        capture_output=True,
        timeout=15,
        text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed:\nSTDERR:\n{result.stderr}\nSTDOUT:\n{result.stdout}")
    return json.loads(result.stdout.splitlines()[-1])["html"]


def test_ordered_lists_render_as_one_unwrapped_ol(node_available):
    html = _run_markdown_case(
        "Before\n\n"
        "1. **Check against the home page** — that's the visual reference for how things should feel.\n"
        "2. **Open DevTools** and inspect the element — check fonts, colors, and spacing against this guide.\n"
        "3. **Flag it** — note the page, the section, what's wrong, and what CSS rule you suspect.\n"
        "4. **Small fixes** — if you know the fix (e.g. wrong CSS variable, wrong font), go ahead and change it in the CSS Module file.\n"
        "5. **Big changes** — Talk it through before making wide changes across many pages.\n\n"
        "After"
    )

    assert html.count("<ol>") == 1
    assert html.count("</ol>") == 1
    assert html.count("<li>") == 5
    assert "<ul>" not in html
    assert "<oli>" not in html
    assert "<uli>" not in html
    assert "<p><ol>" not in html
    assert "<p><li>" not in html
    assert "<p>Before</p>" in html
    assert "<p>After</p>" in html


def test_table_separator_row_not_rendered_as_data(node_available):
    html = _run_markdown_case("| A | B |\n|---|---|\n| 1 | 2 |")

    assert html.count("<tr>") == 2
    assert "<th" in html
    assert "<td" in html
    assert "---" not in html


def test_process_with_thinking_handles_gemma4_thought_channel(node_available):
    html = _run_markdown_case(
        "<|channel>thought\ninternal reasoning<channel|>Final answer.",
        "mod.processWithThinking(input)",
    )

    assert "thinking-section" in html
    assert "internal reasoning" in html
    assert "Final answer." in html
    assert "&lt;|channel&gt;" not in html
    assert "<|channel>" not in html


def test_process_with_thinking_strips_empty_gemma4_thought_channel(node_available):
    html = _run_markdown_case(
        "<|channel>thought\n<channel|>Final answer.",
        "mod.processWithThinking(input)",
    )

    assert "thinking-section" not in html
    assert "Final answer." in html
    assert "&lt;|channel&gt;" not in html
    assert "<|channel>" not in html


def test_process_with_thinking_unwraps_gemma4_response_channel(node_available):
    html = _run_markdown_case(
        "<|channel>thought\ninternal reasoning<channel|><|channel>response\nFinal answer.<channel|>",
        "mod.processWithThinking(input)",
    )

    assert "thinking-section" in html
    assert "internal reasoning" in html
    assert "Final answer." in html
    assert "&lt;|channel&gt;" not in html
    assert "<|channel>" not in html


def test_extract_thinking_blocks_handles_thought_tag(node_available):
    result = _run_markdown_case(
        "<thought>internal reasoning</thought>Final answer.",
        "mod.extractThinkingBlocks(input)",
    )

    assert result["thinkingBlocks"] == ["internal reasoning"]
    assert result["content"] == "Final answer."


def test_url_inside_inline_code_is_not_autolinked(node_available):
    # A URL inside a backtick span is preceded by a space, so the bare-URL
    # autolink used to wrap it in an <a> tag (then swap it for an
    # ___ALLOWED_HTML_ placeholder), corrupting the command shown to the user.
    html = _run_markdown_case("Run `$j = irm http://127.0.0.1:3000/x` to fetch.")

    assert "<code>$j = irm http://127.0.0.1:3000/x</code>" in html
    assert "___ALLOWED_HTML_" not in html
    assert "<a " not in html
    assert 'href="http://127.0.0.1:3000/x"' not in html


def test_url_outside_inline_code_is_still_autolinked(node_available):
    # Inline code must not disable autolinking for bare URLs elsewhere in the
    # same line.
    html = _run_markdown_case("Use `irm` then visit https://example.com/page now.")

    assert "<code>irm</code>" in html
    assert 'href="https://example.com/page"' in html


def test_inline_code_content_is_html_escaped(node_available):
    # Inline code is now extracted before the global escape pass, so it must be
    # escaped at extraction time (matching the fenced-code-block handling).
    html = _run_markdown_case("Render `<b>$1 & 'q'</b>` literally.")

    assert "<code>&lt;b&gt;$1 &amp; &#39;q&#39;&lt;/b&gt;</code>" in html
    assert "<b>" not in html


def test_fenced_code_keeps_dollar_ampersand(node_available):
    # Issue #5663: the block-restore pass used a string replacement, so `$&` in a
    # restored block was read as "the matched text" and re-inserted the
    # placeholder. `perl -pe 's/world/$& again/'` rendered as
    # "s/world/___CODE_BLOCK_0___amp; again/" — the trailing "amp;" is the orphan
    # left behind after `$&` consumed the `$&` of the escaped `$&amp;`.
    html = _run_markdown_case(
        "```sh\necho \"hello world\" | perl -pe 's/world/$& again/'\n```"
    )

    assert "___CODE_BLOCK_" not in html
    assert "s/world/$&amp; again/" in html
    assert "amp; again" not in html.replace("$&amp; again", "")


def test_fenced_code_keeps_dollar_backtick_and_quote(node_available):
    # `` $` `` and `$'` splice the text before/after the placeholder into the
    # block. Unlike `$&` these leave no placeholder behind — the characters just
    # vanish — so assert the content survives verbatim.
    html = _run_markdown_case("```sh\nsed \"s/$`/x/\" && sed \"s/$'/y/\"\n```")

    assert "___CODE_BLOCK_" not in html
    assert "s/$`/x/" in html
    assert "s/$&#39;/y/" in html


def test_fenced_code_keeps_double_dollar(node_available):
    # `$$` collapsed to a single `$` in the restored block.
    html = _run_markdown_case('```sh\necho "$$USD and $$"\n```')

    assert "$$USD and $$" in html


def test_mermaid_block_keeps_dollar_ampersand(node_available):
    # The mermaid restore site had the same hazard: a node label containing `$&`
    # re-inserted the ___MERMAID_BLOCK_n___ placeholder into the diagram source,
    # which then fails to parse. The math and allowed-HTML sites are fixed the
    # same way; they need KaTeX/sanitizer conditions this harness doesn't set up.
    html = _run_markdown_case('```mermaid\ngraph TD; A["$&"] --> B;\n```')

    assert "___MERMAID_BLOCK_" not in html
    assert "$&amp;" in html


def test_currency_dollar_amounts_are_not_rendered_as_math(node_available):
    # "$5 to $10" used to pair the two dollar signs as inline-math delimiters
    # and render "5 to" through KaTeX. Pandoc-style rules now reject it: the
    # closing $ is preceded by a space and followed by a digit.
    html = _run_markdown_case(
        "The price rose from $5 to $10 overnight.", with_katex=True
    )

    assert 'class="katex"' not in html
    assert "$5" in html
    assert "$10" in html


def test_inline_math_still_renders_through_katex(node_available):
    html = _run_markdown_case("Pythagoras: $x^2 + y^2 = z^2$ holds.", with_katex=True)

    assert '<span class="katex" data-display="false">x^2 + y^2 = z^2</span>' in html
    assert "$" not in html


def test_display_math_still_renders_through_katex(node_available):
    html = _run_markdown_case("$$\\frac{a}{b}$$", with_katex=True)

    assert 'data-display="true"' in html
    assert "$$" not in html


def test_dotted_python_import_paths_are_not_autolinked(node_available):
    html = _run_markdown_case(
        "from imblearn.combine import SMOTETomek\n"
        "from sklearn.metrics import f1_score\n"
        "from sklearn.compose import ColumnTransformer\n\n"
        "See example.com/docs for normal domain autolinking."
    )

    assert "___ALLOWED_HTML_" not in html
    assert "imblearn.combine" in html
    assert "sklearn.metrics" in html
    assert "sklearn.compose" in html
    assert 'href="https://imblearn.com' not in html
    assert 'href="https://sklearn.me' not in html
    assert 'href="https://example.com/docs"' in html


# ---------------------------------------------------------------------------
# S6 - sanitizer behavior (item 2): the fixpoint must strip event handlers
# and dangerous schemes after a serialize/reparse, drop SVG/MathML roots, and
# fail closed when the reparse never converges.
# ---------------------------------------------------------------------------

_S6_SANITIZER_STABLE = _SANITIZER_DOM + ".stable"
_S6_SANITIZER_UNSTABLE = _SANITIZER_DOM + ".unstable"


def test_sanitizer_strips_event_handlers_and_dangerous_urls(node_available):
    payload = ('<a href="javascript:alert(1)" onclick="go()">x</a>'
               '<img src="y" onerror="alert(2)">'
               '<a href="data:text/html,x">z</a>')
    html = _run_markdown_case(
        payload, "mod.sanitizeAllowedHtml(input)", dom_stub=_S6_SANITIZER_STABLE
    )

    assert "javascript:" not in html
    assert "onclick" not in html
    assert "onerror" not in html
    assert "data:text/html" not in html
    assert "<a>x</a>" in html
    assert '<img src="y">' in html
    assert "<a>z</a>" in html


def test_sanitizer_removes_svg_math_and_foreign_script_roots(node_available):
    payload = ('<svg><script>alert(1)</script><rect></rect></svg>'
               '<math><mi>x</mi></math>'
               '<details open ontoggle="p(1)">hi</details>')
    html = _run_markdown_case(
        payload, "mod.sanitizeAllowedHtml(input)", dom_stub=_S6_SANITIZER_STABLE
    )

    assert "<svg" not in html.lower()
    assert "<math" not in html.lower()
    assert "<script" not in html.lower()  # lower-cased foreign content still trips the guard
    assert "ontoggle" not in html
    assert "<details open" in html
    assert "hi" in html


def test_sanitizer_four_pass_bound_fails_closed(node_available):
    # Every serialization grafts another surviving element, so the document
    # can never reach a fixpoint. After the 4-pass bound the sanitizer must
    # escape rather than trust the last mutated output.
    payload = '<a href="https://ok.example">x</a><br>'
    html = _run_markdown_case(
        payload, "mod.sanitizeAllowedHtml(input)", dom_stub=_S6_SANITIZER_UNSTABLE
    )

    assert "<a" not in html
    assert "&lt;a href=&quot;https://ok.example&quot;&gt;x&lt;/a&gt;&lt;br&gt;" == html


# ---------------------------------------------------------------------------
# KaTeX glyph SVG: KaTeX draws a few glyphs as inline <svg> rather than font
# characters. Sanitising its output must keep them without reopening the SVG
# door the sanitizer shuts for every other fragment.
# ---------------------------------------------------------------------------


def test_katex_svg_glyphs_survive_sanitising(node_available):
    # The radical of \sqrt and the arrow of \vec are <svg><path>. Dropping them
    # rendered "q = 3\sqrt{l}" as "q = 3 l", a gap where the root sign was.
    html = _run_markdown_case(
        r"\(q= 3\sqrt{l}\) and \(\vec{v}\)", real_katex=True, dom_stub=_S6_SANITIZER_STABLE
    )

    assert html.count("<svg") == 2
    assert "<path d=" in html
    assert "<math" not in html  # the MathML copy is still dropped


def test_katex_svg_allowance_admits_only_katex_shaped_svg(node_available):
    # The stub hands the TeX source back as KaTeX output, so each formula below
    # arrives at the sanitizer as the SVG written in it. Only the first is the
    # shape KaTeX emits: <svg> holding <path>/<line> geometry and nothing else.
    formulas = [
        '<svg viewBox="0 0 1 1"><path d="M0,0L1,1"></path></svg>',
        "<svg><script>alert(1)</script></svg>",
        '<svg onload="alert(2)"><path d="M0"></path></svg>',
        '<svg><path d="M0" onclick="alert(3)"></path></svg>',
        '<svg><use href="#x"></use></svg>',
        "<svg><foreignObject><p>smuggled</p></foreignObject></svg>",
    ]
    html = _run_markdown_case(
        "\n\n".join(f"${f}$" for f in formulas), with_katex=True, dom_stub=_S6_SANITIZER_STABLE
    )

    assert html.count("<svg") == 1
    assert '<svg viewBox="0 0 1 1"><path d="M0,0L1,1"></path></svg>' in html
    for banned in ("alert", "onload", "onclick", "<use", "foreignObject", "smuggled", "<script"):
        assert banned not in html, banned


def test_katex_svg_allowance_does_not_reach_other_fragments(node_available):
    # Model-written raw HTML goes through sanitizeAllowedHtml, which still drops
    # SVG outright - even the exact shape KaTeX output may keep.
    html = _run_markdown_case(
        '<svg viewBox="0 0 1 1"><path d="M0,0L1,1"></path></svg><b>x</b>',
        "mod.sanitizeAllowedHtml(input)", dom_stub=_S6_SANITIZER_STABLE,
    )

    assert "<svg" not in html
    assert "<b>x</b>" in html


# ---------------------------------------------------------------------------
# S6 - shared helper round trips (item 1): malicious markdown and escaped
# DOM text must both come out inert through the real mdToHtml pipeline.
# ---------------------------------------------------------------------------


def test_markdown_escaped_markup_stays_literal(node_available):
    # An already-escaped attack string is user-entered TEXT: mdToHtml must
    # re-escape it for display, never decode it back into live markup.
    html = _run_markdown_case(
        "&lt;img src=x onerror=alert(1)&gt; and "
        "&amp;lt;script&amp;gt;alert(2)&amp;lt;/script&amp;gt;"
    )

    assert "<img" not in html
    assert "<script" not in html.lower()
    assert "&amp;lt;img src=x onerror=alert(1)&amp;gt;" in html


def test_markdown_malicious_raw_html_rendered_inert(node_available):
    html = _run_markdown_case(
        '<details><img src=x onerror="alert(2)"></details> '
        '<a href="javascript:alert(3)">click</a> '
        '<img src=y onerror="alert(4)">',
        dom_stub=_S6_SANITIZER_STABLE,
    )

    assert "<details open" in html
    assert '<img src="x">' in html
    assert "<a>click</a>" in html
    assert '<img src="y">' in html
    assert "onerror" not in html
    assert "javascript:" not in html

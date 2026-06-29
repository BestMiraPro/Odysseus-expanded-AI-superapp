"""Regression coverage for KaTeX output in study markdown rendering."""

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


def _render_markdown_with_katex_stub(markdown: str) -> str:
    script = textwrap.dedent(
        r"""
        import fs from 'node:fs';

        function sanitizeFragment(html) {
          return String(html ?? '')
            .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
            .replace(/\s+(href)\s*=\s*(["'])\s*javascript:[\s\S]*?\2/gi, '');
        }

        const katexStub = {
          renderToString(math, opts) {
            if (!opts || opts.throwOnError !== false) {
              throw new Error('expected non-throwing KaTeX options');
            }
            if (math.includes('\\href{javascript:')) {
              return '<span class="katex"><a href="javascript:alert(1)">x</a></span>';
            }
            if (math.includes('\\href{https://')) {
              return '<span class="katex"><a href="https://example.com/safe">safe</a></span>';
            }
            if (math.includes('<script>')) {
              return `<span class="katex"><script>alert(1)</script><span>${math}</span></span>`;
            }
            return `<span class="katex"><span class="mord">${math}</span></span>`;
          },
        };

        globalThis.window = { location: { origin: 'http://localhost' }, katex: katexStub };
        globalThis.katex = katexStub;
        globalThis.document = {
          readyState: 'loading',
          addEventListener() {},
          createElement(tag) {
            if (tag !== 'template') throw new Error(`unsupported element: ${tag}`);
            return {
              _html: '',
              content: { querySelectorAll() { return []; } },
              set innerHTML(value) { this._html = sanitizeFragment(value); },
              get innerHTML() { return this._html; },
            };
          },
        };
        globalThis.MutationObserver = class { observe() {} };

        let source = fs.readFileSync('./static/js/markdown.js', 'utf8');
        source = source.replace(/import uiModule from ['"]\.\/ui\.js['"];/, '');
        source = source.replace(
          /import \{ splitTableRow \} from ['"]\.\/markdown\/tableRow\.js['"];/,
          `function splitTableRow(row) {
            return (row || '').replace(/^\\s*\\|/, '').replace(/\\|\\s*$/, '').split('|').map(c => c.trim());
          }`
        );
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
        console.log(JSON.stringify({ html: mod.mdToHtml(input) }));
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(markdown)],
        cwd=_REPO,
        capture_output=True,
        timeout=15,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed:\nSTDERR:\n{result.stderr}\nSTDOUT:\n{result.stdout}")
    return json.loads(result.stdout.splitlines()[-1])["html"]


def test_study_markdown_sanitizes_katex_output_before_inner_html(node_available):
    html = _render_markdown_with_katex_stub(
        r"$\href{javascript:alert(1)}{x}$"
        "\n\n"
        r"$\text{<script>alert(1)</script>}$"
        "\n\n"
        r"$\href{https://example.com/safe}{safe}$"
        "\n\n"
        r"$\frac{1}{2}$"
    )

    lowered = html.lower()
    assert "javascript:" not in lowered
    assert "<script" not in lowered
    assert "</script" not in lowered
    assert 'href="https://example.com/safe"' in html
    assert 'class="katex"' in html
    assert r"\frac{1}{2}" in html

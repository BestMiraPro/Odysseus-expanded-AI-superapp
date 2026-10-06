// static/js/model/matchKey.js
//
// Pure helper for matching a model name against a set of known keys. No DOM —
// safe to import anywhere and to unit-test under node.

// Return the most specific (longest) key that is a substring of `name`, or null.
// Returning the first match instead made "gpt-4o-mini" match the shorter
// "gpt-4o" key — billing it at gpt-4o rates (~16x) and showing the wrong
// context window.
//
// Version dots are also tried as dashes, so OpenRouter-style ids such as
// "anthropic/claude-opus-4.5" reach the specific 'claude-opus-4-5' key instead
// of falling back to the older, differently priced 'claude-opus-4'.
export function matchModelKey(name, keys) {
  const n = (name || '').toLowerCase();
  const dashed = n.replace(/(\d)\.(?=\d)/g, '$1-');
  let best = null;
  for (const key of keys) {
    if ((n.includes(key) || dashed.includes(key)) && (best === null || key.length > best.length)) {
      best = key;
    }
  }
  return best;
}

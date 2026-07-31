// A dependency-free stand-in for the browser, used by task_authoring_harness.js
// to run dispatcher/server/static/index.html's REAL <script> unmodified.
//
// This file deliberately implements the *browser*, never the application: an
// HTML parser, a node tree, attribute/dataset reflection, a CSS-selector
// subset (`tag`, `#id`, `.class`, `[attr]`, `[attr="v"]`, descendant
// combinator) and event delegation. No rule of the dispatcher UI is encoded
// here — if a test wants to know what the screen decided, it has to ask the
// shipped code, which is the entire point of the exercise.
//
// jsdom would do this better; it is not vendored because this repo installs no
// npm packages for the Python test suite (see tests/test_task_authoring_js.py)
// and the shipped page is plain, well-formed, hand-written HTML that a ~200
// line parser covers exactly.
'use strict';

const VOID = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
  'meta', 'param', 'source', 'track', 'wbr',
]);
// Elements whose content is text, never markup.
const RAW_TEXT = new Set(['script', 'style', 'textarea']);

const ENTITIES = {
  '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"',
  '&#39;': "'", '&apos;': "'", '&nbsp;': ' ',
};
const decode = s =>
  String(s).replace(/&(amp|lt|gt|quot|apos|nbsp|#39);/g, m => ENTITIES[m]);
const encode = s =>
  String(s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.data = text;
    this.parentNode = null;
  }
  get textContent() { return this.data; }
}

class El {
  constructor(tagName) {
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = Object.create(null);
    this.onclick = null;
    this.onchange = null;
    // Properties the page reads/writes directly. Parsed markup seeds the
    // reflected ones (hidden/disabled/value/class) in setAttribute below.
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.className = '';
    this.title = '';
    this.open = false;
    this._rawHTML = null;
  }

  get id() { return this.attributes.id || ''; }
  // Reflected IDL attributes: `link.href = x` must be visible to
  // getAttribute('href'), because that is how the browser behaves and how a
  // test can tell a safely-built element from a string-interpolated one.
  get href() { return this.attributes.href || ''; }
  set href(v) { this.setAttribute('href', v); }
  get target() { return this.attributes.target || ''; }
  set target(v) { this.setAttribute('target', v); }
  get parentElement() { return this.parentNode; }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }

  setAttribute(name, value) {
    const v = String(value);
    this.attributes[name] = v;
    if (name === 'class') this.className = v;
    else if (name === 'hidden') this.hidden = true;
    else if (name === 'disabled') this.disabled = true;
    else if (name === 'value') this.value = v;
    else if (name === 'title') this.title = v;
    else if (name.startsWith('data-')) {
      this.dataset[camel(name.slice(5))] = v;
    }
  }
  getAttribute(name) {
    return name in this.attributes ? this.attributes[name] : null;
  }
  hasAttribute(name) { return name in this.attributes; }

  get classList() {
    const self = this;
    const list = () => self.className.split(/\s+/).filter(Boolean);
    return {
      contains: c => list().includes(c),
      add(c) { if (!list().includes(c)) self.className = [...list(), c].join(' '); },
      remove(c) { self.className = list().filter(x => x !== c).join(' '); },
      toggle(c) {
        if (list().includes(c)) { this.remove(c); return false; }
        this.add(c);
        return true;
      },
    };
  }

  get textContent() {
    return this.childNodes.map(n => n.textContent).join('');
  }
  set textContent(v) {
    this._rawHTML = null;
    this.childNodes = [];
    if (v !== '') this.appendChild(new TextNode(String(v)));
  }

  // The getter returns the exact string last assigned (browsers re-serialize;
  // that normalization would only make assertions about the page's own output
  // less faithful, not more). When the content came from parsed markup instead
  // of an assignment, it is serialized from the tree.
  get innerHTML() {
    return this._rawHTML !== null ? this._rawHTML : serialize(this);
  }
  set innerHTML(v) {
    this._rawHTML = String(v);
    this.childNodes = [];
    for (const n of parseFragment(String(v))) this.appendChild(n);
  }

  insertAdjacentHTML(position, html) {
    if (position !== 'beforeend') throw new Error(`unsupported: ${position}`);
    this._rawHTML = this.innerHTML + String(html);
    for (const n of parseFragment(String(html))) this.appendChild(n);
  }

  appendChild(node) {
    // any structural edit invalidates the raw string the innerHTML getter
    // would otherwise keep returning
    this._rawHTML = null;
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }
  append(...nodes) {
    for (const n of nodes) {
      this.appendChild(typeof n === 'string' ? new TextNode(n) : n);
    }
  }
  replaceChildren(...nodes) {
    this._rawHTML = null;
    this.childNodes = [];
    this.append(...nodes);
  }

  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter(f => f !== fn);
  }

  matches(selector) { return matchesCompound(this, selector.trim()); }
  closest(selector) {
    for (let n = this; n; n = n.parentNode) if (n.matches(selector)) return n;
    return null;
  }
  querySelector(selector) { return querySelectorAll(this, selector)[0] || null; }
  querySelectorAll(selector) { return querySelectorAll(this, selector); }

  scrollIntoView() {}
  focus() {}
  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.childNodes;
    const at = siblings.indexOf(this);
    if (at >= 0) siblings.splice(at, 1);
    this.parentNode._rawHTML = null;
    this.parentNode = null;
  }

  /** The inline `style` attribute, as a live object (display/visibility). */
  get style() {
    const self = this;
    const parse = () => Object.fromEntries(
      (self.attributes.style || '').split(';')
        .map(rule => rule.split(':'))
        .filter(pair => pair.length === 2)
        .map(([k, v]) => [k.trim().toLowerCase(), v.trim().toLowerCase()]));
    const write = decls => {
      self.attributes.style = Object.entries(decls)
        .map(([k, v]) => `${k}: ${v}`).join('; ');
    };
    return {
      get display() { return parse().display || ''; },
      set display(v) { write({...parse(), display: v}); },
      get visibility() { return parse().visibility || ''; },
      set visibility(v) { write({...parse(), visibility: v}); },
    };
  }

  /** Is THIS node hidden, ignoring its ancestors? */
  get selfHidden() {
    if (this.hidden) return true;
    if (this.attributes['aria-hidden'] === 'true') return true;
    const decls = this.attributes.style || '';
    if (/display\s*:\s*none/i.test(decls)) return true;
    if (/visibility\s*:\s*hidden/i.test(decls)) return true;
    return false;
  }

  /**
   * Can a person actually see this node? Checked on the element AND EVERY
   * ancestor, across all four ways a subtree goes off screen: `hidden`,
   * `aria-hidden="true"`, `display: none`, `visibility: hidden`.
   *
   * The harness's oracle used to be bare `textContent`, which made every
   * "the screen says X" claim satisfiable by markup nobody can see — and
   * that is exactly where a real defect (I-A) lived: a mutation outcome
   * written into a panel navigation had already hidden. No number of extra
   * cases could have found it; only a better instrument could.
   */
  get visible() {
    for (let n = this; n; n = n.parentNode) if (n.selfHidden) return false;
    return true;
  }
}

const camel = s => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());

function serialize(el) {
  return el.childNodes.map(n => {
    if (n.nodeType === 3) return encode(n.data);
    const attrs = Object.entries(n.attributes)
      .map(([k, v]) => ` ${k}="${encode(v)}"`).join('');
    const tag = n.tagName.toLowerCase();
    if (VOID.has(tag)) return `<${tag}${attrs}>`;
    return `<${tag}${attrs}>${serialize(n)}</${tag}>`;
  }).join('');
}

// ---- parser ---------------------------------------------------------------

function tagEnd(html, from) {
  let quote = null;
  for (let i = from; i < html.length; i++) {
    const c = html[i];
    if (quote) { if (c === quote) quote = null; continue; }
    if (c === '"' || c === "'") { quote = c; continue; }
    if (c === '>') return i;
  }
  return html.length;
}

const ATTR_RE = /([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>`]+)))?/g;

function parseAttrs(el, raw) {
  ATTR_RE.lastIndex = 0;
  let m;
  while ((m = ATTR_RE.exec(raw)) !== null) {
    const value = m[2] ?? m[3] ?? m[4] ?? '';
    el.setAttribute(m[1], decode(value));
  }
}

// Tags the HTML spec lets you leave open; the shipped page closes everything,
// but a stray unclosed tag must not silently reparent half the document.
const IMPLIED_CLOSE = {
  li: ['li'], option: ['option'], p: ['p'],
  tr: ['tr', 'td', 'th'], td: ['td', 'th'], th: ['td', 'th'],
  tbody: ['td', 'th', 'tr', 'thead'], thead: ['td', 'th', 'tr'],
};

/** Parse an HTML fragment into a flat list of top-level nodes. */
function parseFragment(html) {
  const root = new El('#fragment');
  const stack = [root];
  const top = () => stack[stack.length - 1];
  let i = 0;

  const addText = text => {
    if (text !== '') top().appendChild(new TextNode(decode(text)));
  };

  while (i < html.length) {
    const lt = html.indexOf('<', i);
    if (lt === -1) { addText(html.slice(i)); break; }
    addText(html.slice(i, lt));
    if (html.startsWith('<!--', lt)) {
      const end = html.indexOf('-->', lt);
      i = end === -1 ? html.length : end + 3;
      continue;
    }
    if (html.startsWith('<!', lt)) {
      i = tagEnd(html, lt) + 1;
      continue;
    }
    const gt = tagEnd(html, lt);
    const raw = html.slice(lt + 1, gt);
    i = gt + 1;

    if (raw.startsWith('/')) {
      const name = raw.slice(1).trim().toLowerCase();
      for (let d = stack.length - 1; d > 0; d--) {
        if (stack[d].tagName.toLowerCase() === name) {
          stack.length = d;
          break;
        }
      }
      continue;
    }

    const nameMatch = /^([A-Za-z][^\s/>]*)/.exec(raw);
    if (!nameMatch) continue;
    const name = nameMatch[1].toLowerCase();
    for (const closable of IMPLIED_CLOSE[name] || []) {
      if (top().tagName.toLowerCase() === closable) stack.pop();
    }
    const el = new El(name);
    parseAttrs(el, raw.slice(nameMatch[1].length).replace(/\/\s*$/, ''));
    top().appendChild(el);

    if (VOID.has(name) || /\/\s*$/.test(raw)) continue;
    if (RAW_TEXT.has(name)) {
      const close = html.toLowerCase().indexOf(`</${name}`, i);
      const end = close === -1 ? html.length : close;
      const body = html.slice(i, end);
      if (body !== '') el.appendChild(new TextNode(body));
      i = end === html.length ? end : tagEnd(html, end) + 1;
      continue;
    }
    stack.push(el);
  }
  const nodes = root.childNodes;
  for (const n of nodes) n.parentNode = null;
  return nodes;
}

// ---- selectors ------------------------------------------------------------

function matchesCompound(el, compound) {
  if (el.nodeType !== 1) return false;
  const re = /^(\*|[a-zA-Z][\w-]*)?((?:[#.][\w-]+|\[[^\]]+\])*)$/;
  const m = re.exec(compound);
  if (!m) throw new Error(`unsupported selector: ${compound}`);
  // `*` matches any element — needed to ask "did ANY node acquire an
  // attribute it should not have", which is how attribute breakout shows up
  if (m[1] && m[1] !== '*' && el.tagName.toLowerCase() !== m[1].toLowerCase()) {
    return false;
  }
  const parts = m[2].match(/[#.][\w-]+|\[[^\]]+\]/g) || [];
  for (const p of parts) {
    if (p[0] === '#') {
      if (el.attributes.id !== p.slice(1)) return false;
    } else if (p[0] === '.') {
      if (!el.className.split(/\s+/).includes(p.slice(1))) return false;
    } else {
      const inner = p.slice(1, -1);
      const eq = inner.indexOf('=');
      if (eq === -1) {
        if (!(inner in el.attributes)) return false;
      } else {
        const key = inner.slice(0, eq).trim();
        const want = inner.slice(eq + 1).trim().replace(/^["']|["']$/g, '');
        if (el.attributes[key] !== want) return false;
      }
    }
  }
  return true;
}

function descendants(root, out = []) {
  for (const n of root.childNodes) {
    if (n.nodeType !== 1) continue;
    out.push(n);
    descendants(n, out);
  }
  return out;
}

function querySelectorAll(root, selector) {
  const parts = selector.trim().split(/\s+(?![^[]*\])/);
  const last = parts[parts.length - 1];
  const ancestors = parts.slice(0, -1);
  return descendants(root).filter(el => {
    if (!matchesCompound(el, last)) return false;
    let n = el.parentNode;
    for (let k = ancestors.length - 1; k >= 0; k--) {
      while (n && !matchesCompound(n, ancestors[k])) n = n.parentNode;
      if (!n) return false;
      n = n.parentNode;
    }
    return true;
  });
}

// ---- document -------------------------------------------------------------

class Document {
  constructor(bodyHtml) {
    this.body = new El('body');
    for (const n of parseFragment(bodyHtml)) this.body.appendChild(n);
  }
  getElementById(id) {
    return descendants(this.body).find(el => el.attributes.id === id) || null;
  }
  querySelector(selector) { return querySelectorAll(this.body, selector)[0] || null; }
  querySelectorAll(selector) { return querySelectorAll(this.body, selector); }
  createElement(tag) { return new El(tag); }
  addEventListener() {}
}

/**
 * Fire `type` on `el` and let it bubble, exactly as delegation on an ancestor
 * container expects. Returns the listeners' return values so the caller can
 * await async handlers instead of racing them.
 */
function dispatch(el, type, {force = false} = {}) {
  // A real person can only act on a control that is BOTH visible and
  // enabled. `force` is the structural escape hatch: it exists so a test can
  // drive defence-in-depth code that the UI cannot reach, and every use of it
  // is named as structural at the call site.
  if (!force && (el.disabled || !el.visible)) return [];
  const event = {
    type, target: el, currentTarget: null,
    preventDefault() {}, stopPropagation() {},
  };
  const results = [];
  for (let n = el; n; n = n.parentNode) {
    event.currentTarget = n;
    const inline = n[`on${type}`];
    if (typeof inline === 'function') results.push(inline(event));
    for (const fn of (n.listeners[type] || []).slice()) results.push(fn(event));
  }
  return results;
}

module.exports = {Document, El, TextNode, dispatch, parseFragment};

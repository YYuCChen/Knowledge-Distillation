"""Exercise actual home.js with a small, synthetic DOM in Node's VM.

The fake deliberately blurs an editor during insertBefore and keeps it focused
on moveBefore. It proves reconciliation decisions, identity and local state;
it cannot prove native controls, rendering or real playback continuity.
"""
import subprocess
from pathlib import Path

import pytest


HARNESS = r'''
const vm = require('vm'), fs = require('fs'), assert = require('assert');
const scenario = process.argv[2];
let document, root, incoming;
const moves = [], focusCalls = [], selectionCalls = [];
class Element {
  constructor(name, attrs = {}, children = []) {
    this.nodeType = 1; this.nodeName = name.toUpperCase();
    this._attrs = new Map(Object.entries(attrs)); this.childNodes = [];
    this.value = attrs.value || ''; this.selectionStart = 2; this.selectionEnd = 7;
    this.selectionDirection = 'forward';
    for (const child of children) { child.parentNode = this; this.childNodes.push(child); }
  }
  get id() { return this.getAttribute('id') || ''; }
  get type() { return this.getAttribute('type') || 'text'; }
  get dataset() {
    return Object.fromEntries([...this._attrs].filter(([key]) => key.startsWith('data-'))
      .map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()), value]));
  }
  get attributes() { return [...this._attrs].map(([name, value]) => ({name, value})); }
  get elements() { return {value: this.querySelector('[name="value"]')}; }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() {
    return this.parentNode?.childNodes[this.parentNode.childNodes.indexOf(this) + 1] || null;
  }
  get isConnected() { return this === root || Boolean(this.parentNode?.isConnected); }
  get innerHTML() { return this === root ? 'before' : 'after'; }
  getBoundingClientRect() { return {top: 10, bottom: 40}; }
  hasAttribute(name) { return this._attrs.has(name); }
  getAttribute(name) { return this._attrs.get(name) ?? null; }
  setAttribute(name, value) { this._attrs.set(name, String(value)); }
  removeAttribute(name) { this._attrs.delete(name); }
  contains(node) { return node === this || this.childNodes.some(child => child.contains(node)); }
  matches(selector) {
    return selector.split(',').some(raw => {
      const s = raw.trim();
      if (s === 'input:not([type="hidden"])') return this.nodeName === 'INPUT' && this.type !== 'hidden';
      if (s === 'button:not([disabled])') return this.nodeName === 'BUTTON' && !this.hasAttribute('disabled');
      if (s === 'form[id]') return this.nodeName === 'FORM' && Boolean(this.id);
      if (s === '[name="value"]') return this.getAttribute('name') === 'value';
      const prefix = s.match(/^\[data-sync-key\^="([^"]+)"\]$/);
      if (prefix) return (this.dataset.syncKey || '').startsWith(prefix[1]);
      const key = s.match(/^\[data-sync-key="([^"]+)"\]$/);
      if (key) return this.dataset.syncKey === key[1];
      if (s === '[data-sync-key]') return this.hasAttribute('data-sync-key');
      if (s === '[data-stale-confirmation]') return this.hasAttribute('data-stale-confirmation');
      return /^[a-z]+$/.test(s) && this.nodeName === s.toUpperCase();
    });
  }
  querySelectorAll(selector) {
    return this.childNodes.flatMap(child => [
      ...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)
    ]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector) || null; }
  cloneNode(deep) {
    return new Element(this.nodeName, Object.fromEntries(this._attrs),
      deep ? this.childNodes.map(child => child.cloneNode(true)) : []);
  }
  relocate(child, before, method) {
    if (child === before) return;
    if (before && before.parentNode !== this) throw new Error('invalid reference node');
    moves.push({parent: this, child, method});
    if (method === 'insertBefore' && child.isConnected && child.contains(document.activeElement)) {
      document.activeElement.selectionStart = 0; document.activeElement.selectionEnd = 0;
      document.activeElement.selectionDirection = 'none'; document.activeElement = null;
    }
    if (child.parentNode) {
      child.parentNode.childNodes.splice(child.parentNode.childNodes.indexOf(child), 1);
    }
    child.parentNode = this;
    const index = before ? this.childNodes.indexOf(before) : this.childNodes.length;
    this.childNodes.splice(index, 0, child);
  }
  insertBefore(child, before) { this.relocate(child, before, 'insertBefore'); }
  moveBefore(child, before) { this.relocate(child, before, 'moveBefore'); }
  remove() {
    if (this.contains(document.activeElement)) document.activeElement = null;
    if (this.parentNode) this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
    this.parentNode = null;
  }
  replaceWith(node) { this.parentNode.insertBefore(node, this); this.remove(); }
  focus(options) {
    focusCalls.push({node: this, options});
    if (!this.refuseFocus) document.activeElement = this;
  }
  setSelectionRange(start, end, direction) {
    selectionCalls.push({node: this, start, end, direction});
    this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;
  }
}
const el = (name, attrs, children) => new Element(name, attrs, children);
const audio = el('audio', {'data-audio-identity': 'synthetic', 'data-audio-revision': 'fixed', src: '/synthetic.wav'});
Object.assign(audio, {paused: false, ended: false, currentTime: 19.7772,
  pause() { throw new Error('must not pause retained audio'); },
  load() { throw new Error('must not reload retained audio'); },
  play() { throw new Error('must not autoplay retained audio'); }});
const field = el(scenario === 'textarea-backward' ? 'textarea' : 'input',
  {name: 'value', value: '', type: scenario === 'unsupported-selection' ? 'number' : 'text'});
field.value = 'synthetic local draft';
if (scenario === 'focus-backward' || scenario === 'textarea-backward') field.selectionDirection = 'backward';
if (scenario === 'focus-caret') field.selectionEnd = field.selectionStart;
if (scenario === 'no-selection-api') field.setSelectionRange = undefined;
if (scenario === 'refused-focus') field.refuseFocus = true;
const form = el('form', {id: 'manual', action: '/synthetic-confirm'}, [field]);
const mediaCard = el('section', {'data-sync-key': 'member-media'}, [audio, form]);
const button = el('button', {id: 'fallback'});
const ordinary = el('section', {'data-sync-key': 'member-ordinary'}, [button]);
root = el('main', {id: 'home-results'}, [ordinary, mediaCard]);
incoming = el('main', {id: 'home-results'}, [mediaCard.cloneNode(true), ordinary.cloneNode(true)]);
document = {
  activeElement: field,
  querySelector: s => s === '#home-results' ? root : null,
  querySelectorAll: () => [],
  getElementById: id => [root, ...root.querySelectorAll('form[id]')].find(node => node.id === id) || null,
  addEventListener() {}, fonts: {ready: Promise.resolve()}
};
const context = {
  console, document, Node: {ELEMENT_NODE: 1}, CSS: {escape: value => value},
  DOMParser: class { parseFromString() {
    return {querySelector: s => s === '#home-results' ? incoming : incoming.querySelector(s)};
  } },
  window: {addEventListener() {}, scrollBy() {}, location: {href: '/'}},
  setTimeout() {}, clearTimeout() {}, localStorage: {getItem() { return null; }},
  ResizeObserver: class {observe() {} disconnect() {}},
  fetch() { throw new Error('no network in this harness'); }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
function apply() { context.applyPage('synthetic server response'); }
function retainedMedia() {
  assert.strictEqual(root.querySelector('audio'), audio);
  assert.deepEqual([audio.paused, audio.ended, audio.currentTime], [false, false, 19.7772]);
}
if (scenario === 'audio-direct') {
  root = el('main', {id: 'home-results'}, [ordinary, audio]);
  incoming = el('main', {id: 'home-results'}, [audio.cloneNode(true), ordinary.cloneNode(true)]);
  document.activeElement = null; apply(); retainedMedia();
  assert.equal(moves.find(move => move.child === audio)?.method, 'insertBefore');
  assert.strictEqual(root.firstChild, audio);
} else if (scenario === 'ordinary-move') {
  root.childNodes = [mediaCard, ordinary];
  incoming = el('main', {id: 'home-results'}, [ordinary.cloneNode(true), mediaCard.cloneNode(true)]);
  apply(); retainedMedia();
  assert.equal(moves.find(move => move.child === ordinary)?.method, 'moveBefore');
  assert.strictEqual(root.firstChild, ordinary);
  assert.strictEqual(document.activeElement, field);
  assert.equal(focusCalls.length, 0); assert.equal(selectionCalls.length, 0);
} else if (scenario === 'disconnected-fallback') {
  incoming = el('main', {id: 'home-results'}, [ordinary.cloneNode(true)]);
  apply();
  assert.equal(field.isConnected, false);
  assert.strictEqual(document.activeElement, button);
  assert.equal(focusCalls.length, 1); assert.strictEqual(focusCalls[0].node, button);
  assert.equal(focusCalls[0].options.preventScroll, true);
  assert.equal(selectionCalls.length, 0);
} else if (scenario === 'draft-replacement') {
  incoming.querySelector('form').childNodes = [el('textarea', {name: 'value', value: ''})];
  incoming.querySelector('form').firstChild.parentNode = incoming.querySelector('form');
  apply(); retainedMedia();
  const replacement = root.querySelector('[name="value"]');
  assert.notStrictEqual(replacement, field); assert.equal(field.isConnected, false);
  assert.equal(replacement.value, 'synthetic local draft');
  assert.strictEqual(document.activeElement, button);
  assert.equal(selectionCalls.length, 0);
} else {
  if (scenario === 'non-editor') document.activeElement = mediaCard;
  if (scenario === 'outside-editor') document.activeElement = el('input', {id: 'outside'});
  if (scenario === 'no-active') document.activeElement = null;
  const previous = document.activeElement;
  const expectedSelection = [field.selectionStart, field.selectionEnd, field.selectionDirection];
  apply(); retainedMedia();
  assert.strictEqual(root.firstChild, mediaCard);
  assert.equal(moves.find(move => move.child === mediaCard)?.method, 'insertBefore');
  assert.strictEqual(root.querySelector('[name="value"]'), field);
  assert.equal(field.value, 'synthetic local draft');
  if (['non-editor', 'outside-editor', 'no-active'].includes(scenario)) {
    assert.strictEqual(document.activeElement, scenario === 'non-editor' ? null : previous);
    assert.equal(focusCalls.length, 0); assert.equal(selectionCalls.length, 0);
  } else {
    assert.equal(focusCalls.length, 1); assert.strictEqual(focusCalls[0].node, field);
    assert.equal(focusCalls[0].options.preventScroll, true);
    if (scenario === 'refused-focus') {
      assert.strictEqual(document.activeElement, null); assert.equal(selectionCalls.length, 0);
    } else {
      assert.strictEqual(document.activeElement, field);
      if (['unsupported-selection', 'no-selection-api'].includes(scenario)) {
        assert.equal(selectionCalls.length, 0);
      } else {
        assert.equal(selectionCalls.length, 1); assert.strictEqual(selectionCalls[0].node, field);
        assert.deepEqual([field.selectionStart, field.selectionEnd, field.selectionDirection], expectedSelection);
      }
    }
  }
}
'''


@pytest.mark.parametrize('scenario', [
    'audio-direct', 'ordinary-move', 'focus-forward', 'focus-backward',
    'focus-caret', 'textarea-backward', 'unsupported-selection',
    'no-selection-api', 'refused-focus', 'non-editor', 'outside-editor',
    'no-active', 'disconnected-fallback', 'draft-replacement',
])
def test_media_reconciliation_preserves_local_work(scenario, tmp_path):
    script = Path(__file__).resolve().parents[2] / 'src/knowledge_distiller/v1/static/home.js'
    result = subprocess.run(
        ['node', '-e', HARNESS, str(script), scenario],
        cwd=tmp_path, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr

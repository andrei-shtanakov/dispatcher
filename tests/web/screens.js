// Shared, app-level helpers for harnesses that must open a screen before they
// can touch it. Deliberately thin: it knows the page has tabs, nothing else.
'use strict';

const path = require('path');
const {makeBrowser, dispatch} = require(path.join(__dirname, 'dom.js'));

/** Globals a harness must put in its VM context for the router to work. */
function browserGlobals(initialHash = '') {
  const {window, location, history} = makeBrowser(initialHash);
  return {window, location, history};
}

/**
 * Opens a screen the way a person does: by clicking the real tab button.
 * A harness that flips `hidden` by hand proves nothing about the shipped UI.
 */
async function openScreen(page, id) {
  const tab = page.document.getElementById(`tab-${id}`);
  if (!tab) throw new Error(`no tab button for screen "${id}"`);
  await Promise.all(dispatch(tab, 'click'));
  for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0));
  const panel = page.document.getElementById(`screen-${id}`);
  if (!panel || panel.hidden) throw new Error(`screen "${id}" did not open`);
}

module.exports = {browserGlobals, openScreen};

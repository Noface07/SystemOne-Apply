// The page as it stands, frozen into one static HTML file for replay tests (replay.py): every element keeps the box
// it is drawn in, its visibility and its form state, with no script, stylesheet or site needed to draw it again.
// MHTML can't serve: Chrome disables every form control of an archived page, and the page reader skips disabled
// ones. Each element is placed absolutely at its own offset from its parent, so the geometry the reader measures
// (what is on screen, what covers what) comes back as it was; the scroll position is kept for the replay to restore.
(() => {
  const KEEP = ['display', 'visibility', 'opacity', 'overflow-x', 'overflow-y', 'pointer-events', 'z-index',
                'white-space', 'font-size', 'font-weight', 'color', 'background-color', 'content-visibility', 'cursor'];
  const FLOW = new Set(['HTML', 'HEAD', 'BODY', 'SCRIPT', 'STYLE', 'LINK', 'META', 'TITLE', 'NOSCRIPT', 'TEMPLATE']);
  const root = document.documentElement;
  const live = [root, ...root.querySelectorAll('*')];
  const boxes = new Map(live.map(e => [e, e.getBoundingClientRect()]));
  const clone = root.cloneNode(true);
  const copies = [clone, ...clone.querySelectorAll('*')];
  live.forEach((e, i) => {
    const c = copies[i];
    if (!c || c.tagName !== e.tagName) return;
    const cs = getComputedStyle(e), r = boxes.get(e);
    let style = KEEP.map(p => `${p}:${cs.getPropertyValue(p)}`).join(';') + ';border:0;padding:0;margin:0;box-sizing:border-box';
    if (!FLOW.has(e.tagName)) {
      const parent = e.parentElement && !FLOW.has(e.parentElement.tagName) ? boxes.get(e.parentElement)
        : {left: -scrollX, top: -scrollY};
      style += `;position:absolute;left:${r.left - parent.left}px;top:${r.top - parent.top}px;` +
               `width:${r.width}px;height:${r.height}px`;
    }
    c.setAttribute('style', style);
    if (e.matches('input,textarea')) {
      // A typed value is no text of the page: the replay puts it back into the box (RESTORE in replay.py)
      if (e.tagName === 'TEXTAREA') { c.textContent = ''; c.setAttribute('data-jev-value', e.value || ''); }
      else c.setAttribute('value', e.value || '');
      if (e.checked) c.setAttribute('checked', ''); else c.removeAttribute('checked');
    }
    if (e.tagName === 'OPTION') { if (e.selected) c.setAttribute('selected', ''); else c.removeAttribute('selected'); }
    if (e.matches('input[type=password]')) c.setAttribute('value', '');  // never written down
  });
  for (const x of clone.querySelectorAll('script,link,style,iframe,noscript')) x.remove();
  const body = clone.querySelector('body');
  const h = Math.max(root.scrollHeight, document.body?.scrollHeight || 0);
  const w = Math.max(root.scrollWidth, document.body?.scrollWidth || 0);
  if (body) body.setAttribute('style', `position:relative;margin:0;padding:0;border:0;width:${w}px;height:${h}px`);
  clone.setAttribute('data-jev-scroll', `${scrollX},${scrollY}`);
  clone.setAttribute('data-jev-viewport', `${innerWidth},${innerHeight}`);
  return '<!doctype html>\n' + clone.outerHTML;
})()

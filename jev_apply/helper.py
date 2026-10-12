"""A small bubble in the application tab: what the run is doing, and buttons to pause it, let it carry on, skip
the job, or answer the questions it is waiting for, right there on the page.

It lives in a closed shadow root marked aria-hidden, so the agent's snapshot never sees it (no text, no controls,
no change to the page's fingerprint), and the browser hides it while it reads or clicks the page, so it never
covers a field. Clicks become events the agent drains on each step; the agent sends back what to show."""

import json

BUBBLE = r"""(() => {
  if (window.top !== window || window.__jevHelper) return;
  const host = document.createElement('div');
  host.setAttribute('aria-hidden', 'true');
  host.setAttribute('data-jev-helper', '');
  host.style.cssText = 'all:initial;position:fixed;left:12px;bottom:12px;z-index:2147483647;';
  const root = host.attachShadow({mode: 'closed'});
  root.innerHTML = `<style>
    :host { all: initial; }
    * { box-sizing: border-box; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
    .wrap { display: flex; align-items: flex-end; gap: 8px; }
    .dot { width: 38px; height: 38px; border-radius: 999px; border: 0; cursor: pointer; color: #fff; font-weight: 700;
      background: #6d5efc; box-shadow: 0 4px 14px rgba(0,0,0,.25); font-size: 13px; }
    .dot.paused { background: #d97706; } .dot.waiting { background: #d97706; animation: ping 1.4s infinite; }
    .panel { display: none; width: 300px; max-height: 60vh; overflow: auto; background: #fff; color: #1f2430;
      border-radius: 12px; padding: 12px; box-shadow: 0 10px 30px rgba(0,0,0,.25); font-size: 13px; line-height: 1.4; }
    .wrap.open .panel { display: block; }
    .title { font-weight: 700; margin-bottom: 4px; }
    .status { color: #5b6170; margin-bottom: 10px; word-break: break-word; }
    .row { display: flex; gap: 6px; flex-wrap: wrap; }
    button.b { border: 1px solid #d7dae3; background: #f6f7fb; color: #1f2430; border-radius: 8px; padding: 6px 10px;
      font-size: 12.5px; cursor: pointer; font-weight: 600; }
    button.b.primary { background: #6d5efc; border-color: #6d5efc; color: #fff; }
    button.b.warn { color: #b91c1c; }
    .q { margin: 8px 0; } .q label { display: block; font-weight: 600; margin-bottom: 4px; }
    .q input, .q select { width: 100%; padding: 6px 8px; border: 1px solid #d7dae3; border-radius: 8px;
      font-size: 13px; }
    .note { color: #8a8f9c; font-size: 11.5px; margin-top: 8px; }
    @keyframes ping { 50% { box-shadow: 0 0 0 8px rgba(217,119,6,.25); } }
  </style>
  <div class="wrap"><button class="dot" title="jev-apply: pause, skip or help">jev</button>
    <div class="panel"><div class="title">jev-apply</div><div class="status">Starting…</div>
      <div class="qs"></div>
      <div class="row">
        <button class="b" data-e="pause">Pause</button>
        <button class="b primary" data-e="continue"
          title="You've handled it (answered or typed it on the page): carry on now">Continue</button>
        <button class="b warn" data-e="skip" title="Leave this job and go to the next one">Skip job</button>
      </div>
      <div class="note">Only you see this; the agent ignores it.</div></div></div>`;
  const $ = s => root.querySelector(s);
  const h = {events: [], state: {}};
  $('.dot').onclick = () => $('.wrap').classList.toggle('open');
  root.addEventListener('click', e => {
    const b = e.target.closest('button[data-e]');
    if (!b) return;
    let type = b.dataset.e;
    if (type === 'pause' && h.state.paused) type = 'resume';
    if (type === 'answers') {
      const answers = {};
      root.querySelectorAll('[data-q]').forEach(i => { if (i.value.trim()) answers[i.dataset.q] = i.value.trim(); });
      h.events.push({type, answers});
    } else h.events.push({type});
    b.disabled = true; setTimeout(() => { b.disabled = false; }, 1500);
  });
  h.set = state => {
    h.state = state || {};
    $('.status').textContent = h.state.status || '';
    $('[data-e="pause"]').textContent = h.state.paused ? 'Resume' : 'Pause';
    const dot = $('.dot');
    dot.classList.toggle('paused', !!h.state.paused);
    dot.classList.toggle('waiting', !!h.state.waiting);
    const qs = h.state.questions || [];
    const key = JSON.stringify(qs);
    if ($('.qs').dataset.key !== key) {
      $('.qs').dataset.key = key;
      $('.qs').innerHTML = '';
      qs.forEach(q => {
        const box = document.createElement('div'); box.className = 'q';
        const label = document.createElement('label'); label.textContent = q.question; box.append(label);
        let input;
        if ((q.options || []).length) {
          input = document.createElement('select');
          input.append(new Option('Choose…', ''));
          q.options.forEach(o => input.append(new Option(o, o)));
        } else { input = document.createElement('input'); input.placeholder = 'Your answer'; }
        input.dataset.q = q.question; box.append(input); $('.qs').append(box);
      });
      if (qs.length) {
        const save = document.createElement('button'); save.className = 'b primary'; save.dataset.e = 'answers';
        save.textContent = 'Save answers and carry on'; save.style.margin = '4px 0 10px'; $('.qs').append(save);
        $('.wrap').classList.add('open');
      }
    }
    if (h.state.waiting || h.state.paused) $('.wrap').classList.add('open');
  };
  h.drain = () => h.events.splice(0);
  h.hide = () => { host.style.visibility = 'hidden'; };
  h.show = () => { host.style.visibility = 'visible'; };
  window.__jevHelper = h;
  const mount = () => { (document.body || document.documentElement).appendChild(host); };
  document.body ? mount() : document.addEventListener('DOMContentLoaded', mount, {once: true});
})()"""

DRAIN = "(() => window.__jevHelper ? JSON.stringify(window.__jevHelper.drain()) : '[]')()"
HIDE = "window.__jevHelper && window.__jevHelper.hide()"
SHOW = "window.__jevHelper && window.__jevHelper.show()"


def show_state(state):
    """The expression that shows `state` (status, paused, waiting, questions) in the bubble."""
    return f"window.__jevHelper && window.__jevHelper.set({json.dumps(state)})"

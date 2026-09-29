(() => {
  // A new document is read once loaded (styles applied), but never waited on for more than 5 s of its life.
  if (!document.body || (document.readyState!=='complete' && performance.now()<5000)) return null;
  const cache = window.__jevApply ||= {ids:new WeakMap(), nodes:new Map(), next:1};
  const identity = e => {
    if (!cache.ids.has(e)) cache.ids.set(e,cache.next++);
    const id=cache.ids.get(e); cache.nodes.set(id,e); return id;
  };
  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);
  const clean = s => (s||'').replace(/\s+/g,' ').trim();
  // A name without the site's live character counter ("0 of 20 characters"): the question stays the same text.
  const bare = s => clean(String(s||'').replace(/\b\d+\s*(of|\/)\s*\d+\s*characters?\b/gi,''));
  const safe = e => !['password','hidden'].includes(e.type);
  const styleOf = e => e.ownerDocument.defaultView.getComputedStyle(e);
  // Open shadow roots and same-origin frames are part of the page: every lookup walks all of them.
  const frameDoc = f => { try { const d=f.contentDocument; return d?.body && d.readyState!=='loading' ? d : null; } catch { return null; } };
  const rootsNow = () => {
    const found=[];
    const walk = root => {
      found.push(root);
      for (const e of root.querySelectorAll('*')) {
        if (e.shadowRoot) walk(e.shadowRoot);
        if (e.tagName==='IFRAME' || e.tagName==='FRAME') { const d=frameDoc(e); if (d) walk(d); }
      }
    };
    walk(document);
    return found;
  };
  const ROOTS=rootsNow();
  const all = (sel, roots=ROOTS) => roots.flatMap(r=>[...r.querySelectorAll(sel)]);
  const parentOf = e => e.parentElement || (e.getRootNode?.() instanceof ShadowRoot ? e.getRootNode().host : null);
  const byId = (e,id) => (e.getRootNode?.().getElementById?.(id)) || e.ownerDocument?.getElementById(id) || null;
  // Where a same-origin frame's content sits in the top window: rects and clicks use top-window coordinates.
  const frameOffset = doc => {
    let x=0, y=0;
    try {
      for (let f=doc.defaultView?.frameElement; f; f=f.ownerDocument.defaultView?.frameElement) {
        const r=f.getBoundingClientRect(), s=styleOf(f);
        x+=r.x+f.clientLeft+parseFloat(s.paddingLeft||0); y+=r.y+f.clientTop+parseFloat(s.paddingTop||0);
      }
    } catch {}
    return {x,y};
  };
  const place = (r,doc) => {
    const o=doc===document ? {x:0,y:0} : frameOffset(doc);
    return {x:r.x+o.x, y:r.y+o.y, width:r.width, height:r.height, left:r.left+o.x, right:r.right+o.x,
      top:r.top+o.y, bottom:r.bottom+o.y};
  };
  const rectOf = e => place(e.getBoundingClientRect(), e.ownerDocument);
  cache.rectOf = rectOf;
  const visible = e => !!e && !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const onscreen = r => r.width>0 && r.height>0 && r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth;
  const center = e => { const r=rectOf(e); return {r, x:r.x+r.width/2, y:r.y+r.height/2}; };
  // The element under a point, looking into open shadow roots and same-origin frames.
  const deepHit = (x,y) => {
    let hit=document.elementFromPoint(x,y), ox=0, oy=0;
    for (let i=0; i<12 && hit; i++) {
      if (hit.shadowRoot) {
        const inner=hit.shadowRoot.elementFromPoint(x-ox,y-oy);
        if (inner && inner!==hit) { hit=inner; continue; }
      }
      if (hit.tagName==='IFRAME' || hit.tagName==='FRAME') {
        const d=frameDoc(hit);
        if (d) {
          const r=hit.getBoundingClientRect(), s=styleOf(hit);
          ox+=r.x+hit.clientLeft+parseFloat(s.paddingLeft||0); oy+=r.y+hit.clientTop+parseFloat(s.paddingTop||0);
          const inner=d.elementFromPoint(x-ox,y-oy);
          if (inner) { hit=inner; continue; }
        }
      }
      break;
    }
    return hit;
  };
  const within = (e,n) => { for (; n; n=n.parentNode || n.host) if (n===e) return true; return false; };
  // The element a click lands on must be the control itself, or a label wired to it.
  cache.hittable = (e,x,y) => {
    const hit=deepHit(x,y);
    return !!hit && (within(e,hit) || hit.closest('label')?.control===e);
  };
  const name = (e,seen=new Set()) => {
    if (!e || seen.has(e)) return '';
    seen.add(e);
    const referenced=(e.getAttribute('aria-labelledby')||'').split(/\s+/)
      .map(id=>id && name(byId(e,id),seen)).filter(Boolean).join(' ');
    return bare(referenced || e.getAttribute('aria-label') ||
      [...(e.labels||[])].map(l=>name(l,seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(e.type) ? e.value : '') || e.getAttribute('alt') ||
      (e.tagName==='INPUT' || e.tagName==='SELECT' || e.tagName==='TEXTAREA' ? '' :
        // A label that wraps its field also wraps the field's suggestion box and hidden messages ("No location
        // found", "Loading"): only its own visible words name the field.
        [...e.childNodes].map(n=>n.nodeType===3 ? n.textContent :
          n.nodeType===1 && n.getAttribute('aria-hidden')!=='true' && !n.querySelector?.(FIELDS) &&
            n.checkVisibility?.({checkVisibilityCSS:true}) !== false ? name(n,seen) : '').join(' ')) ||
      e.getAttribute('title') || e.getAttribute('placeholder') || '');
  };
  // The question a control answers: a fieldset legend, a named group, or the nearest short block of text
  // around it. "Yes" alone is meaningless; "Willing to relocate? Yes No" is not.
  const FIELDS='input:not([type=hidden]):not([type=radio]):not([type=checkbox]):not([type=button]):not([type=submit])'+
    ':not([type=reset]):not([type=image]),select,textarea,[role="combobox"],[role="textbox"],[role="searchbox"]';
  // Controls whose own words are answers, not questions ("Yes", "No", "Python", "Remove").
  const CHOICES='input[type=radio],input[type=checkbox],[role="radio"],[role="checkbox"],[role="switch"],'+
    '[role="option"],[aria-pressed],button,[role="button"]';
  const texts=new Map();
  // Text of a block without dropdown options, so a select's question isn't buried under its option list.
  // Open shadow roots inside the block count as part of it.
  const textIn = (root,parts) => {
    const walk=(root.ownerDocument||root).createTreeWalker(root,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT); let n;
    while ((n=walk.nextNode())) {
      if (n.nodeType===1) { if (n.shadowRoot) textIn(n.shadowRoot,parts); continue; }
      const parent=n.parentElement;
      if (parent && !parent.closest('select,[role="listbox"],script,style,noscript,template') && visible(parent))
        parts.push(n.textContent);
    }
  };
  const blockText = e => {
    if (!texts.has(e)) { const parts=[]; textIn(e,parts); texts.set(e, clean(parts.join(' '))); }
    return texts.get(e);
  };
  const customs=new Set();  // clickables without a role, found below: their words are answers too
  const isChoice = e => e.matches(CHOICES) || customs.has(e);
  const context = (e, own) => {
    const group=e.closest('fieldset,[role="radiogroup"],[role="group"]');
    if (group) {
      const legend=group.querySelector('legend');
      const t=clean(legend ? legend.innerText : name(group)) || blockText(group);
      // A group whose words are only its options ("Yes No": LinkedIn puts the question above the fieldset) doesn't
      // say what is asked: look further up, as for any other control.
      let rest=' '+t+' ';
      for (const c of group.querySelectorAll(CHOICES)) {
        const l=name(c); if (l && l!==t) rest=rest.replace(' '+l+' ',' ');
      }
      const onlyOptions=!legend && isChoice(e) && [...group.querySelectorAll('input')].every(x=>(name(x)||'')!==t) &&
        rest.replace(/[\s*:?()]/g,'').length<=1;
      if (t && t!==own && !onlyOptions) return t.slice(0,300);
    }
    // Climb only while the block belongs to this control alone: stop at a block that holds another question's
    // field, so "Current CTC" never inherits the text of the neighbouring "Expected CTC" field. A block holding
    // nothing but the choices' own words ("Yes No") is not the question: keep climbing to the one that asks.
    let options='', top=null;
    for (let i=0, p=parentOf(e); i<6 && p && p!==p.ownerDocument.body; i++, p=parentOf(p)) {
      if ([...p.querySelectorAll(FIELDS)].some(x=>x!==e && visible(x))) break;  // hidden helpers don't count
      if (e.type==='radio' && e.name && [...p.querySelectorAll('input[type="radio"]')].some(x=>x.name!==e.name)) break;
      const t=blockText(p);
      if (t.length>400) break;
      top=p;
      if (t.length<=own.length+2) continue;
      if (!isChoice(e)) return t;
      let rest=' '+t+' ';
      for (const c of [...p.querySelectorAll(CHOICES), ...[...customs].filter(x=>x!==p && p.contains(x))]) {
        const l=name(c) || [...(c.labels||[])].map(x=>clean(x.innerText)).join(' ');
        if (l) rest=rest.replace(l,' ');
      }
      if (rest.replace(/[\s*:?()]/g,'').length>1) return t;
      options=t;
    }
    // Still no question: it often sits just before the block (a label column, a <p> above the choices).
    if (isChoice(e))
      for (let s=top?.previousElementSibling, k=0; s && k<2; s=s.previousElementSibling, k++) {
        if (s.matches(FIELDS+',input') || s.querySelector(FIELDS+',input')) break;
        const q=blockText(s);
        if (q && q.length<=300) return clean(q+' '+(options || own));
      }
    return options;
  };
  const described = e => clean((e.getAttribute('aria-describedby')||'').split(/\s+/)
    .map(id=>id && byId(e,id)?.innerText || '').join(' ')).slice(0,200);
  const roles=['button','link','checkbox','radio','switch','tab','menuitem','menuitemradio','menuitemcheckbox',
    'option','gridcell','combobox','textbox','searchbox','spinbutton'];
  const selector='a[href],button,input,textarea,select,summary,[contenteditable]:not([contenteditable="false"]),'+
    roles.map(role=>'[role="'+role+'"]').join(',');
  const DATES=['date','month','week','time','datetime-local'];
  const role = e => {
    const explicit=e.getAttribute('role');
    if (roles.includes(explicit)) return explicit;
    if (e.tagName==='BUTTON' || e.tagName==='SUMMARY') return 'button';
    if (e.tagName==='A') return 'link';
    if (e.tagName==='SELECT') return 'combobox';
    if (e.tagName==='TEXTAREA' || e.isContentEditable) return 'textbox';
    if (e.tagName==='INPUT') {
      if (['checkbox','radio'].includes(e.type)) return e.type;
      if (['button','submit','reset','image'].includes(e.type)) return 'button';
      if (e.type==='search') return 'searchbox';
      if (e.type==='number') return 'spinbutton';
      if (DATES.includes(e.type)) return 'date';
      if (['text','email','url','tel'].includes(e.type)) return 'textbox';
    }
    return null;
  };
  // A button or clickable that shows it is chosen without aria-pressed: a class or data attribute says so.
  const chosen = e => e.getAttribute('aria-selected')==='true' || e.getAttribute('aria-current')==='true' ||
    /^(on|checked|active|selected|true)$/i.test(e.getAttribute('data-state')||e.getAttribute('data-selected')||'') ||
    /(^|[\s_-])(is-)?(selected|active|checked|chosen)($|[\s_-])/i.test(typeof e.className==='string' ? e.className : '');
  const invalid = e => {
    let bad=e.getAttribute('aria-invalid')==='true';
    try { bad ||= e.matches(':user-invalid'); } catch {}
    const marked='.is-invalid,.invalid,.has-error,.error,[data-invalid="true"]';
    return bad || e.matches(marked) || !!e.parentElement?.matches(marked);
  };
  // Inputs with a typing mask ("DD/MM/YYYY", "__-____"): typed key by key, since the mask reads keystrokes.
  const masked = e => e.tagName==='INPUT' && (/_|#|\b(dd|mm|yy|yyyy|hh)\b\s*[\/.\-]/i.test(e.placeholder||'') ||
    e.hasAttribute('data-mask') || e.hasAttribute('data-inputmask') || /mask/i.test(typeof e.className==='string' ? e.className : ''));
  const standInFor = e => {
    if (e.closest('[aria-hidden="true"],[inert]')) return null;
    const boxed = x => x && x.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) &&
      rectOf(x).width>0 && rectOf(x).height>0 && rectOf(x).height<120 && !x.querySelector('select:not([hidden]) ~ select');
    if (e.checkVisibility({checkVisibilityCSS:true}) && rectOf(e).width>0 && rectOf(e).height>0) {
      const parent=e.parentElement;  // opacity 0 over its box: the box is the parent (or the select's own place)
      return boxed(parent) ? parent : null;
    }
    return [e.nextElementSibling, e.previousElementSibling, e.parentElement].find(boxed) || null;
  };
  // Styled checkboxes/radios often hide the real input and show a label. Click the label; read the input.
  const proxyOf = e => ['checkbox','radio'].includes(e.type) && !visible(e) ?
    ([...(e.labels||[])].find(visible) || null) : drawnFor(e);
  // Workday's date parts: the real <input role="spinbutton"> (Month, Year) is 0x0 inside a box drawn for it, which
  // focuses it when clicked. Click the box; type into and read the input.
  function drawnFor(e) {
    if (e.tagName!=='INPUT' || !(e.getAttribute('role')==='spinbutton' ||
        /date/i.test(e.getAttribute('data-automation-id')||''))) return null;
    const r=rectOf(e);
    if (r.width>1 && r.height>1) return null;
    for (let p=e.parentElement, i=0; p && i<3; p=p.parentElement, i++) {
      const box=rectOf(p);
      if (box.width>1 && box.height>1 && box.height<120 && visible(p)) return p;
    }
    return null;
  }
  cache.pageKey=()=>[performance.timeOrigin,location.href,scrollX,scrollY,innerWidth,innerHeight,
    all('input,textarea,select',rootsNow()).filter(e=>safe(e) && e.type!=='file')
      .map(e=>[identity(e),e.value,e.checked,e.selectedIndex,e.disabled,e.readOnly])];
  cache.guard=e=>{
    if (!e?.isConnected || !(visible(e) || proxyOf(e))) return null;
    const scope=e.closest('form,dialog,[role="dialog"],fieldset,article,li,tr,[role="row"]') || e.parentElement;
    return [identity(e),role(e),name(e),e.value??null,e.checked??null,e.selectedIndex??null,
      e.readOnly??null,e.matches(':disabled'),e.getAttribute('aria-disabled'),
      e.getAttribute('aria-expanded'),e.getAttribute('aria-checked'),e.getAttribute('aria-selected'),
      e.getAttribute('aria-pressed'),e.getAttribute('href'),scope?.innerText?.slice(0,6000)||''];
  };
  // The nearest heading above each control ("Work Experience 2", "Education"): which block a field belongs to.
  const anchor = e => { while (e?.getRootNode?.() instanceof ShadowRoot) e=e.getRootNode().host; return e; };
  const headings=all('h1,h2,h3,h4,h5,h6,[role="heading"]').filter(h=>clean(h.innerText)).map(h=>[anchor(h),h]);
  const section = e => {
    const a=anchor(e);
    for (let i=headings.length-1; i>=0; i--) {
      const [h,heading]=headings[i];
      if (h.ownerDocument===a.ownerDocument && (h.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING))
        return clean(heading.innerText).slice(0,80);
    }
    return '';
  };
  // Site chrome, not the application: a header search box ("Search jobs"), a footer's language picker, unless
  // it sits in a form or dialog.
  const SEARCH_FORM='form[role="search"],form[action*="search" i],form[action$="/jobs"],form[id*="search" i],'+
    'form[class*="search" i]';
  // Job boards draw their search bar with plain <div>s (Naukri's "nI-gNb-search-bar", Indeed's "desktop-search"):
  // an optional box with job-search wording inside a block named search/header/navbar is that bar.
  const SEARCHY=/search|header|navbar|nav-bar|topbar|masthead|gnb/i;
  const JOB_SEARCH=/keyword|designation|job title|title|compan|skills?|city|location|postcode|remote|where|what|experience/i;
  const searchBar = e => {
    if (e.required || e.getAttribute('aria-required')==='true') return false;
    const words=[e.placeholder, e.getAttribute('aria-label'), e.name, e.id].join(' ');
    if (!JOB_SEARCH.test(words)) return false;
    for (let q=e.parentElement, i=0; q && i<9; q=q.parentElement, i++)
      if (SEARCHY.test((typeof q.className==='string' ? q.className : '')+' '+q.id)) return true;
    return false;
  };
  const inNav = e => !e.closest('dialog,[role="dialog"]') && (!!e.closest(SEARCH_FORM) || (!e.closest('form') &&
    (!!e.closest('header,nav,footer,[role="banner"],[role="navigation"],[role="search"],[role="contentinfo"]') ||
      e.type==='search' || searchBar(e))));
  // A chat-style questionnaire (Naukri's chatbot): questions arrive as messages above a reply box or choice chips.
  const chatOf = e => e.closest('[class*="chatbot" i],[id*="chatbot" i],[class*="chat-bot" i],[role="log"]');
  const chatQuestion = (e,chat) => {
    const top=rectOf(e).top+2;
    let last='';
    for (const m of chat.querySelectorAll('div,p,span,li')) {
      if (m.contains(e) || m.querySelector('input,textarea,select,button,[contenteditable]') || !visible(m)) continue;
      const own=[...m.childNodes].some(n=>n.nodeType===3 && n.textContent.trim().length>2);
      if (own && rectOf(m).bottom<=top) last=clean(m.innerText);
    }
    return last.slice(0,300);
  };
  // Required without a mark in the text (LinkedIn draws its "*" with CSS): the group says so, or the site has
  // attached a "This field is required" message to it after a Next/Review click.
  const flaggedRequired = e => {
    const g=e.closest('fieldset,[role="radiogroup"],[role="group"]');
    if (g?.getAttribute('aria-required')==='true') return true;
    if (g && /this field is required|please make a selection|please select an option/i.test(g.parentElement?.innerText||''))
      return true;
    const ids=[g?.getAttribute('aria-describedby'), e.getAttribute('aria-describedby'), e.getAttribute('aria-errormessage')]
      .join(' ').split(/\s+/).filter(Boolean);
    return /required|please (make a )?select|select an option|can'?t be (blank|empty)/i.test(
      ids.map(id=>byId(e,id)?.innerText||'').join(' '));
  };
  const describe = (e, rname, label, base) => {
    const ctx=bare(context(e,label)); if (ctx && ctx!==label) base.context=ctx;
    if (e.matches(FIELDS) && inNav(e)) base.nav=true;  // only fields: an Apply button in a job header stays
    const chat=chatOf(e);
    if (chat) {
      base.chat=true;
      // The question is the latest message above: for a reply box named "Type message here..." and for chips.
      if (isChoice(e) || /type|message|here|answer|reply/i.test(label) || !ctx) {
        const q=chatQuestion(e,chat);
        if (q && !/save|send|submit|skip/i.test(label)) { base.context=q; if (!isChoice(e) || e.type==='radio') base.required=true; }
      }
    }
    const sec=section(e); if (sec) base.section=sec;
    const help=described(e); if (help) base.help=help;
    // Required: the attribute, or the asterisk sites print (in the label, or in a choice's question).
    const question=isChoice(e) ? (ctx||'').replace(label,'') : '';
    if (e.required || e.getAttribute('aria-required')==='true' || /[*✱＊]/.test(label) || /[*✱＊]/.test(question) ||
        flaggedRequired(e))
      base.required=true;
    if (invalid(e)) {
      base.invalid=true;
      const error=byId(e,e.getAttribute('aria-errormessage')||'')?.innerText;
      if (error) base.error=clean(error).slice(0,160);
    }
    return base;
  };
  // Clickable elements without any role (a <div> Yes/No choice, a dropdown's items): where the pointer cursor
  // starts, with a short text of their own and nothing interactive inside.
  let budget=4000;
  for (const e of all('div,span,li,label,td,p')) {
    if (--budget<0) break;
    if ((e.tagName==='LABEL' && e.control) || e.closest(selector) || e.querySelector(selector)) continue;
    const up=parentOf(e);
    if (up && customs.has(up)) continue;
    const r=rectOf(e);
    if (!onscreen(r) || r.width>600 || r.height>120) continue;
    if (styleOf(e).cursor!=='pointer' && !e.hasAttribute('onclick')) continue;
    const label=name(e);
    if (!label || label.length>60 || !visible(e)) continue;
    const x=r.x+r.width/2, y=r.y+r.height/2;
    if (x<0 || y<0 || x>=innerWidth || y>=innerHeight || !cache.hittable(e,x,y)) continue;
    customs.add(e);
  }
  // Suggestions under the field being typed in: a floating list right below it, whatever its markup (Lever's
  // location list is plain <div>s). Its items are options for that field's question.
  let typing=document.activeElement;
  for (let i=0; i<10 && typing; i++) {
    const inner=typing.shadowRoot?.activeElement || (typing.tagName==='IFRAME' ? frameDoc(typing)?.activeElement : null);
    if (!inner || inner===typing) break;
    typing=inner;
  }
  const popup=new Set();
  if (typing && (typing.tagName==='INPUT' || typing.getAttribute?.('role')==='combobox') && visible(typing)) {
    const fr=rectOf(typing);
    const floats = x => {
      for (let q=x, i=0; q && q.nodeType===1 && i<6; q=parentOf(q), i++) {
        if (q.contains(typing)) return false;
        if (['absolute','fixed'].includes(styleOf(q).position)) return true;
      }
      return false;
    };
    for (const e of all('li,div,a,span,p')) {
      if (e.closest(selector) || e.querySelector('input,select,textarea,button')) continue;
      const r=rectOf(e);
      if (!onscreen(r) || r.height>80 || r.top<fr.bottom-4 || r.top>fr.bottom+480 || r.right<fr.left ||
          r.left>fr.right+40) continue;
      const own=clean(e.innerText||'');
      if (!own || own.length>120 || !visible(e) || !floats(e)) continue;
      if ([...e.children].some(c=>clean(c.innerText||'')===own)) continue;  // the child holding the same text
      const x=r.x+r.width/2, y=r.y+r.height/2;
      if (x<0 || y<0 || x>=innerWidth || y>=innerHeight || !cache.hittable(e,x,y)) continue;
      popup.add(e);
    }
    for (const e of popup) if ([...popup].some(x=>x!==e && e.contains(x))) popup.delete(e);  // items, not the list
    for (const e of popup) customs.add(e);
  }
  const typingQuestion=typing && popup.size ? name(typing) : '';
  const actions=[], elements=[];
  for (const e of all(selector)) {
    if (!safe(e) || e.type==='file' || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]')) continue;
    if (e.isContentEditable && e.parentElement?.isContentEditable) continue;  // inside an editor already listed
    const proxy=proxyOf(e);
    // A native dropdown drawn over a styled box (opacity 0) or hidden behind one (Lever, select2): it is set by
    // script, so it counts when the box standing in for it is on screen.
    const standIn=e.tagName==='SELECT' && !visible(e) ? standInFor(e) : null;
    const shown=standIn || proxy || e;
    if (!visible(shown)) continue;
    const {r,x,y}=center(shown), rname=role(e);
    if (!rname || !onscreen(r) || x<0 || y<0 || x>=innerWidth || y>=innerHeight) continue;
    // Covered by a banner/modal, or clipped by a scroll box
    if (!cache.hittable(shown,x,y) && !(standIn && cache.hittable(e,x,y))) continue;
    if (rname==='gridcell' && e.querySelector('button,[role="button"]')) continue;
    // No name of its own (Lever's dropdowns have no <label>): the question around it names it.
    let label=name(e)||(proxy && name(proxy))||bare(context(e,'')).slice(0,150)||rname;
    // LinkedIn names every radio of a group after the question (aria-label) and leaves its <label> empty: the
    // answer ("Yes", "No") is the text beside that one radio, or else its value.
    let asked='', answers=[];
    if (e.type==='radio' && e.name) {
      const peers=[...e.ownerDocument.querySelectorAll('input[type="radio"]')].filter(x=>x.name===e.name);
      const named=x=>name(x)||(proxyOf(x) && name(proxyOf(x)))||'';
      const beside = x => {
        for (let q=x.parentElement, i=0; q && i<4; q=q.parentElement, i++) {
          if ([...q.querySelectorAll('input[type="radio"]')].some(y=>y!==x)) break;
          const t=clean(blockText(q).replace(label,''));
          if (t) return t.slice(0,60);
        }
        const v=clean(x.value);
        return v && v!=='on' ? v.slice(0,60) : '';
      };
      if (peers.length>1 && peers.every(x=>named(x)===label)) {
        answers=peers.map(beside);
        if (answers.every(Boolean) && new Set(answers).size===peers.length) { asked=label; label=beside(e); }
      }
    }
    const base={node:identity(e),role:rname,label,rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
    if (proxy) base.proxy=identity(proxy);
    describe(e,rname,label,base);
    if (asked) base.context=clean(asked+' '+answers.join(' '));
    if (e.placeholder) base.placeholder=e.placeholder;
    // Fields that hold several values: <select multiple>, or a tag input whose list is multi-selectable.
    const owned=(e.getAttribute('aria-controls')||e.getAttribute('aria-owns')||'').split(/\s+/).filter(Boolean)
      .map(id=>byId(e,id));
    if (e.multiple || e.getAttribute('aria-multiselectable')==='true' ||
        owned.some(x=>x?.getAttribute('aria-multiselectable')==='true')) base.multiple=true;
    if (e.tagName==='INPUT' || e.tagName==='TEXTAREA') {
      base.input_type=e.tagName==='TEXTAREA' ? 'textarea' : e.type;
      if (e.maxLength>0) base.maxlength=e.maxLength;
      if (e.type==='number') base.step=e.getAttribute('step') || '1';
      if (masked(e)) base.masked=true;
      if (e.form || e.closest('form')) base.in_form=true;  // Enter here could submit the form
    }
    for (const key of ['checked','selected','expanded','pressed']) {
      const value=e.getAttribute('aria-'+key);
      if (value!==null) base[key]=value;
    }
    if (rname==='button' && base.pressed===undefined && !e.hasAttribute('aria-checked')) base.chosen=String(chosen(e));
    if (['checkbox','radio'].includes(e.type)) base.checked=String(e.checked);
    elements.push(e);
    if (e.tagName==='SELECT') {
      for (const o of e.options) if (!o.selected && !o.disabled && !o.closest('optgroup[disabled]'))
        actions.push({...base,kind:'select',value:o.value,
          current_value:[...e.selectedOptions].map(o=>o.label).join(', '),label:base.label+' → '+o.label});
    } else if (rname==='date') {
      actions.push({...base,kind:'setdate',value:e.value});
    } else {
      const editable=!e.readOnly && e.getAttribute('aria-readonly')!=='true' &&
        (['textbox','searchbox','spinbutton'].includes(rname) ||
          (rname==='combobox' && ['INPUT','TEXTAREA'].includes(e.tagName)));
      const value='value' in e ? String(e.value) :
        e.isContentEditable || rname==='combobox' ? e.innerText.trim() : '';
      actions.push({...base,kind:editable?'fill':'click',value});
      if (editable) actions.push({...base,kind:'click',value,label:'Open '+base.label});
    }
  }
  // The role-less clickables found above, placed in document order among the other controls.
  const OPTIONISH=/option|dropdown-item|menu-?item|suggest|autocomplete|select__/i;  // a list's items, not a form's choices
  for (const e of customs) {
    const r=rectOf(e), label=name(e);
    const optionish=popup.has(e) || OPTIONISH.test((typeof e.className==='string' ? e.className : '')+' '+e.id) ||
      !!e.closest('[role="listbox"]');
    const base={node:identity(e),role:optionish ? 'option' : 'button',label,rect:{x:r.x,y:r.y,w:r.width,h:r.height},
      custom:true};
    describe(e,base.role,label,base);
    if (popup.has(e) && typingQuestion) base.context=typingQuestion;  // a suggestion answers the field typed in
    if (!optionish) base.chosen=String(chosen(e));
    const action={...base,kind:'click',value:''};
    const at=elements.findIndex(x=>x.ownerDocument===e.ownerDocument &&
      (anchor(e).compareDocumentPosition(anchor(x)) & Node.DOCUMENT_POSITION_FOLLOWING));
    const index=at<0 ? actions.length : actions.findIndex(a=>a.node===identity(elements[at]));
    actions.splice(index<0 ? actions.length : index,0,action);
    elements.splice(at<0 ? elements.length : at,0,e);
  }
  // File inputs are usually hidden behind an "Attach" button. Offer them when that visible anchor is on screen.
  for (const e of all('input[type="file"]')) {
    if (e.disabled || e.closest('[inert]')) continue;
    let anchorEl=visible(e) ? e : [...(e.labels||[])].find(visible) || null;
    for (let p=parentOf(e), i=0; !anchorEl && p && p!==p.ownerDocument?.body && i<3; p=parentOf(p), i++)
      if (visible(p) && blockText(p)) anchorEl=p;
    if (!anchorEl) continue;
    const {r}=center(anchorEl);
    if (!onscreen(r)) continue;
    const label=name(e) || name(anchorEl) || 'File upload';
    const action={node:identity(e),role:'file',label,kind:'upload',rect:{x:r.x,y:r.y,w:r.width,h:r.height},
      value:[...(e.files||[])].map(f=>f.name).join(', ')};
    const ctx=context(e,label); if (ctx && ctx!==label) action.context=ctx;
    const sec=section(e); if (sec) action.section=sec;
    if (e.accept) action.accept=e.accept;
    if (e.required || /\*/.test(ctx||'')) action.required=true;
    actions.push(action);
  }
  // Application forms are often embedded from another origin (Greenhouse, Lever, Workday). Same-origin frames
  // are read in place above; a cross-origin one is offered as a page to open in this tab.
  const ATS=/greenhouse|lever\.co|myworkday|icims|smartrecruiters|ashbyhq|jobvite|taleo|successfactors|workable|recruitee|bamboohr|zoho|keka|darwinbox|freshteam|breezy|teamtailor|personio|oraclecloud|eightfold|phenom/i;
  for (const e of all('iframe[src]')) {
    let same=false; try { same=!!e.contentDocument; } catch {}
    if (same) continue;  // read in place above (opened on its own, iCIMS's frame page bounces back to the outer page)
    const r=rectOf(e);
    let host=''; try { host=new URL(e.src).host } catch {}
    // Below the fold is fine: opening it navigates, no click needed (iCIMS puts the job in a frame further down).
    if (!visible(e) || !r.width || !/^https?:/i.test(e.src) || ((r.width<300 || r.height<200) && !ATS.test(host)))
      continue;
    actions.push({node:identity(e),role:'iframe',kind:'frame',label:'Embedded page: '+(e.title||host),value:host,
      rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
  }
  const words=[]; let length=0;
  for (const root of ROOTS) {
    const doc=root.ownerDocument || root, start=root.body || root;
    if (!start || length>=6000) continue;
    const walker=doc.createTreeWalker(start,NodeFilter.SHOW_TEXT), range=doc.createRange(); let node;
    while ((node=walker.nextNode()) && length<6000) {
      const value=node.textContent.trim(), parent=node.parentElement;
      if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
      range.selectNodeContents(node);
      if (onscreen(place(range.getBoundingClientRect(),doc))) { words.push(value); length+=value.length; }
    }
  }
  // Scroll the window, or the scroll box under the viewport centre (a modal application form, a same-origin
  // frame, for example).
  const scrolls = e => /(auto|scroll|overlay)/.test(styleOf(e).overflowY) && e.scrollHeight>e.clientHeight+2;
  let box=null, boxRect=null;
  for (let e=deepHit(innerWidth/2,innerHeight/2); e; e=parentOf(e)) {
    const doc=e.ownerDocument;
    if (e===doc.body || e===doc.documentElement) {
      const frame=doc.defaultView?.frameElement, scroller=doc.scrollingElement;
      if (frame && scroller && scroller.scrollHeight>scroller.clientHeight+2) { box=scroller; boxRect=rectOf(frame); break; }
      if (!frame) break;
      e=frame; continue;
    }
    if (scrolls(e)) { box=e; boxRect=rectOf(e); break; }
  }
  // Nothing scrollable under the centre and the window is at its end (a big window over a small form panel):
  // use the on-screen scroll box around form controls that still hides the most content.
  const windowLeft=document.documentElement.scrollHeight-scrollY-innerHeight;
  if (!box && windowLeft<=2) {
    const seen=new Set(); let most=2;
    for (const control of all('input,select,textarea,button,[role]').slice(0,400))
      for (let e=parentOf(control); e && e!==e.ownerDocument.body && e!==e.ownerDocument.documentElement; e=parentOf(e)) {
        if (seen.has(e)) break;
        seen.add(e);
        if (!scrolls(e)) continue;
        const r=rectOf(e), left=e.scrollHeight-e.clientHeight-e.scrollTop;
        if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && left>most) { box=e; boxRect=r; most=left; }
      }
  }
  const scroll=box ? {y:box.scrollTop,height:box.scrollHeight,view:box.clientHeight,inner:true,
      x:Math.max(1,Math.min(innerWidth-1,boxRect.x+boxRect.width/2)),
      at:Math.max(1,Math.min(innerHeight-1,boxRect.y+boxRect.height/2))} :
    {y:scrollY,height:document.documentElement.scrollHeight,view:innerHeight,inner:false,x:innerWidth/2,at:innerHeight*0.6};
  const text=words.join('\n').slice(0,6000);
  const page_key=cache.pageKey(), guards={};
  for (const a of actions) if (!(a.node in guards)) guards[a.node]=cache.guard(cache.nodes.get(a.node));
  const semantics=actions.map(({rect,...action})=>action);
  const marker=[performance.timeOrigin,location.href,scrollX,scrollY,box?.scrollTop??null,innerWidth,innerHeight,
    document.title,text,semantics,page_key[6]];
  // A dropdown of every country must not push the rest of the form (Submit) out of the list: controls are capped
  // at 250, native dropdown options separately.
  let controls=0, choices=0;
  const kept=actions.filter(a=>a.kind==='select' ? choices++<600 : controls++<250);
  const omitted_actions=actions.length-kept.length;
  actions.splice(0,actions.length,...kept);
  actions.forEach((a,i)=>a.id='e'+(i+1));
  const delta=Math.round(scroll.view*0.7);
  if (scroll.y+scroll.view<scroll.height-2)
    actions.push({id:'scroll_down',kind:'scroll',label:'Scroll down'+(box?' inside the form panel':''),delta,x:scroll.x,y:scroll.at});
  if (scroll.y>0)
    actions.push({id:'scroll_up',kind:'scroll',label:'Scroll up'+(box?' inside the form panel':''),delta:-delta,x:scroll.x,y:scroll.at});
  if (all('[aria-expanded="true"],[role="listbox"],[role="dialog"],dialog[open]').some(visible))
    actions.push({id:'escape',kind:'key',key:'Escape',label:'Press Escape (close an open menu, list or popup)'});
  actions.push({id:'wait',kind:'wait',label:'Wait for the page to update'});
  // Node ids restart in every new document: `doc` scopes them.
  // A visible password box: a sign-in or sign-up wall. Password fields are never offered; the page says why.
  const login=all('input[type="password"]').some(e=>visible(e) && rectOf(e).width>0);
  // Required questions anywhere on the page (above or below the screen too) still without an answer: what the
  // agent lists when it stops, so none is forgotten because it scrolled away.
  // A searchable dropdown (react-select) keeps its text box empty and shows the chosen answer beside it.
  const shownChoice = (e,q) => {
    if (e.getAttribute('role')!=='combobox' && !e.hasAttribute('aria-autocomplete')) return false;
    for (let p=parentOf(e), i=0; p && i<4; p=parentOf(p), i++) {
      if ([...p.querySelectorAll(FIELDS)].some(x=>x!==e && visible(x))) break;
      const rest=blockText(p).replace(q,'').replace(/\b(select|choose|search)\b\W*/gi,'').replace(/[*✱＊\s]/g,'');
      if (rest.length>0) return true;
    }
    return false;
  };
  const required_empty=[], radios=new Set(), open_questions=[];
  // A radio's answer text: its own name, else the text beside it (LinkedIn leaves the <label> empty), else value.
  const answerOf = (x,question) => {
    const own=name(x)||(proxyOf(x) && name(proxyOf(x)))||'';
    if (own && own!==question) return own.slice(0,60);
    for (let q=x.parentElement, i=0; q && i<4; q=q.parentElement, i++) {
      if ([...q.querySelectorAll('input[type="radio"]')].some(y=>y!==x)) break;
      const t=clean(blockText(q).replace(question,''));
      if (t) return t.slice(0,60);
    }
    return x.value && x.value!=='on' ? clean(x.value).slice(0,60) : '';
  };
  for (const e of all('input,select,textarea')) {
    if (!safe(e) || ['file','submit','button','reset','image'].includes(e.type) || e.disabled) continue;
    if (!e.checkVisibility({checkVisibilityCSS:true}) && !(e.tagName==='SELECT' && standInFor(e))) continue;
    const q=clean(name(e) || context(e,'')), asked=clean(context(e,q)) || q;
    if (!(e.required || e.getAttribute('aria-required')==='true' || /[*✱＊]/.test(q+' '+asked.slice(0,200)) ||
        flaggedRequired(e))) continue;
    let empty;
    if (e.type==='radio') {
      if (radios.has(e.name)) continue;
      radios.add(e.name);
      empty=![...e.ownerDocument.querySelectorAll('input[type="radio"]')].some(x=>x.name===e.name && x.checked);
    } else if (e.type==='checkbox') empty=!e.checked;
    else if (e.tagName==='SELECT') empty=e.selectedIndex<0 || !e.value || /^\s*(select|choose|please|--)/i.test(e.options[e.selectedIndex]?.text||'');
    else empty=!String(e.value||'').trim() && !shownChoice(e,q);
    const what=(e.type==='radio' ? asked : q).slice(0,80);
    if (empty && what) {
      required_empty.push(what);
      if (e.type==='radio') {
        const peers=[...e.ownerDocument.querySelectorAll('input[type="radio"]')].filter(x=>x.name===e.name);
        const options=peers.map(x=>answerOf(x,q)).filter(Boolean);
        const question=clean(options.reduce((t,o)=>t.replace(new RegExp('\\s'+o.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'(?=\\s|$)'),''),' '+asked));
        open_questions.push({question:(question||q).slice(0,300), options});
      } else if (e.tagName==='SELECT') {
        open_questions.push({question:q.slice(0,300), options:[...e.options].map(o=>clean(o.text)).filter(t=>t && !/^\s*(select|choose|please|--)/i.test(t)).slice(0,30)});
      } else if (e.type!=='checkbox') open_questions.push({question:q.slice(0,300), options:[]});
    }
  }
  return {doc:String(performance.timeOrigin),url:location.href,title:document.title,w:innerWidth,h:innerHeight,text,
    scroll,actions,marker,page_key,guards,omitted_actions,login,required_empty:[...new Set(required_empty)].slice(0,20),
    open_questions:open_questions.slice(0,20)};
})()

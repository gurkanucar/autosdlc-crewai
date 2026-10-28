(() => {
  'use strict';
  const slides = [...document.querySelectorAll('#stage > .slide')];
  // build.py sets the player's texts for the deck's language; Turkish is the default.
  const L = Object.assign({step:'adım', nextStep:'Sonraki adım', nextSlide:'Sonraki slayt', stepsOn:'Adım adım anlatım açık. Sağ ok sonraki öğeyi gösterir.', stepsOff:'Tüm öğeler gösteriliyor. Sağ ok sonraki slayda geçer.', noNotes:'Bu slaytta konuşmacı notu yok.', slide:'Slayt', found:'slayt bulundu', noFullscreen:'Bu tarayıcı tam ekrana geçişi desteklemiyor. Tarayıcının tam ekran seçeneğini kullanabilirsiniz.', exitFullscreen:'Tam ekrandan çık (F)', fullscreen:'Tam ekran (F)'}, window.DECK_TEXT);
  if (!slides.length) return;
  const $ = id => document.getElementById(id);
  const stageArea = $('stage-area');
  const overview = $('overview');
  const help = $('help');
  const notesPanel = $('notes-panel');
  const search = $('slide-search');
  const grid = $('overview-grid');
  let isReady = false;
  let current = 0;
  let currentStep = 1;
  let stepMode = new URLSearchParams(location.search).get('steps') !== 'all';
  const maxStep = slide => Math.max(1, ...[...slide.querySelectorAll('[data-step]')].map(e=>Number(e.dataset.step)));
  function paintStep(animate = true) {
    const slide = slides[current];
    const max = maxStep(slide);
    slide.dataset.currentStep = stepMode ? currentStep : max;
    window.Whiteboard?.reveal(slide, stepMode ? currentStep : Infinity, animate);
    $('step-status').textContent = stepMode && max > 1 ? `${L.step} ${currentStep} / ${max}` : '';
    $('step-mode').setAttribute('aria-pressed', String(stepMode));
    $('previous').disabled = current === 0 && (!stepMode || currentStep <= 1);
    $('next').disabled = current === slides.length - 1 && (!stepMode || currentStep >= max);
    $('next').setAttribute('aria-label', stepMode && currentStep < max ? `${L.nextStep} (${currentStep + 1} / ${max})` : L.nextSlide);
  }
  function advance(direction, skipSteps = false) {
    if (!isReady) return;
    if (stepMode && !skipSteps) {
      if (direction > 0 && currentStep < maxStep(slides[current])) { currentStep++; paintStep(); return; }
      if (direction < 0 && currentStep > 1) { currentStep--; paintStep(false); return; }
    }
    const target = current + direction;
    if(target < 0 || target >= slides.length) return;
    // Going back returns to a slide already seen: show its last state at once instead of replaying it.
    show(target, true, direction < 0 && stepMode ? maxStep(slides[target]) : 1, direction > 0);
  }
  function toggleSteps() {
    stepMode = !stepMode;
    currentStep = stepMode ? 1 : maxStep(slides[current]);
    paintStep(stepMode);
    $('announcement').textContent = stepMode ? L.stepsOn : L.stepsOff;
  }
  let overviewBuilt = false;
  let touchStart = null;
  let searchQuery = '';
  // <br> has no textContent separator. Preserve readable titles in all controls.
  const readableTitle = slide => slide.querySelector('h1,h2').innerHTML.replace(/<br\s*\/?\s*>/gi, ' ').replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
  const fold = value => value.toLocaleLowerCase('tr-TR').normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ı/g, 'i');
  function resize() {
    const padding = getComputedStyle(stageArea);
    const width = stageArea.clientWidth - parseFloat(padding.paddingLeft) - parseFloat(padding.paddingRight);
    const height = stageArea.clientHeight - parseFloat(padding.paddingTop) - parseFloat(padding.paddingBottom);
    const scale = Math.max(.05, Math.min(width / 1600, height / 900));
    document.documentElement.style.setProperty('--stage-scale', scale);
  }
  function updateNotes() {
    $('notes-meta').textContent = `${current + 1} / ${slides.length} · ${slides[current].dataset.chapter}`;
    $('notes-title').textContent = readableTitle(slides[current]);
    $('notes-content').innerHTML = slides[current].querySelector('.speaker-notes')?.innerHTML || `<p>${L.noNotes}</p>`;
    notesPanel.scrollTop = 0;
  }
  function show(index, writeHash = true, step = 1, animate = true) {
    const next = Math.max(0, Math.min(index, slides.length - 1));
    slides.forEach((slide, i) => {
      if (!slide.hidden) window.Whiteboard?.deactivate(slide);
      slide.hidden = i !== next;
      slide.classList.toggle('is-active', i === next);
      slide.setAttribute('aria-hidden', String(i !== next));
    });
    current = next;
    currentStep = Math.min(step, maxStep(slides[current]));
    slides[current].querySelectorAll('[data-step]').forEach(e=>e.classList.remove('is-revealed'));
    window.Whiteboard?.activate(slides[current], stepMode ? currentStep : Infinity, animate);
    paintStep(animate);
    $('slide-position').textContent = `${String(current + 1).padStart(2, '0')} / ${slides.length}`;
    $('chapter-name').textContent = slides[current].dataset.chapter;
    $('progress').style.width = `${(current + 1) / slides.length * 100}%`;
    $('progress-track').setAttribute('aria-valuenow', String(current + 1));
    document.title = `${readableTitle(slides[current])} · AutoSDLC`;
    $('announcement').textContent = `${L.slide} ${current + 1} / ${slides.length}: ${readableTitle(slides[current])}`;
    if (writeHash && location.hash !== `#${slides[current].id}`) {
      try { history.replaceState(null, '', `#${slides[current].id}`); }
      catch { location.hash = slides[current].id; }
    }
    if (!notesPanel.hidden) updateNotes();
    if (overviewBuilt) grid.querySelectorAll('.overview-item').forEach(button => {
      const selected = Number(button.dataset.index) === current;
      button.setAttribute('aria-current', String(selected));
    });
  }
  function fromHash() {
    if (!isReady) return;
    const id = location.hash.slice(1);
    const index = slides.findIndex(slide => slide.id === id);
    show(index >= 0 ? index : 0, false);
  }
  function toggleNotes() {
    notesPanel.hidden = !notesPanel.hidden;
    $('toggle-notes').setAttribute('aria-pressed', String(!notesPanel.hidden));
    if (!notesPanel.hidden) updateNotes();
    requestAnimationFrame(resize);
  }
  function scalePreviews() {
    grid.querySelectorAll('.overview-item:not([hidden]) .preview').forEach(preview => {
      preview.firstElementChild.style.transform = `scale(${preview.clientWidth / 1600})`;
      window.Whiteboard?.activate(preview.firstElementChild, Infinity, false);
    });
  }
  function buildOverview() {
    if (overviewBuilt) return;
    const fragment = document.createDocumentFragment();
    slides.forEach((slide, index) => {
      const button = document.createElement('button');
      button.className = 'overview-item';
      button.type = 'button';
      button.dataset.index = index;
      button.dataset.search = fold(`${readableTitle(slide)} ${slide.dataset.chapter} ${slide.querySelector('.slide-content').textContent}`);
      button.setAttribute('aria-label', `${index + 1}. ${readableTitle(slide)}`);
      button.setAttribute('aria-current', String(index === current));
      const preview = document.createElement('div');
      preview.className = 'preview';
      preview.setAttribute('aria-hidden', 'true');
      preview.inert = true;
      const clone = slide.cloneNode(true);
      clone.hidden = false;
      clone.classList.remove('is-active');
      clone.removeAttribute('id');
      clone.removeAttribute('aria-labelledby');
      clone.querySelector('.speaker-notes')?.remove();
      clone.querySelectorAll('[id]').forEach(el => el.removeAttribute('id'));
      clone.querySelectorAll('a').forEach(a => a.removeAttribute('href'));
      preview.append(clone);
      const label = document.createElement('div');
      label.className = 'overview-label';
      const number = document.createElement('span');
      number.textContent = String(index + 1).padStart(2, '0');
      const text = document.createElement('span');
      text.textContent = readableTitle(slide);
      label.append(number, text);
      button.append(preview, label);
      button.addEventListener('click', () => { overview.close(); show(index); });
      fragment.append(button);
    });
    grid.append(fragment);
    grid.querySelectorAll('.preview .slide').forEach(clone => clone.classList.add('no-motion'));
    overviewBuilt = true;
  }
  function filterOverview() {
    searchQuery = search.value;
    const query = fold(searchQuery.trim());
    let visible = 0;
    grid.querySelectorAll('.overview-item').forEach(button => {
      button.hidden = !!query && !button.dataset.search.includes(query);
      if (!button.hidden) visible++;
    });
    $('no-results').hidden = visible !== 0;
    $('search-status').textContent = `${visible} ${L.found}`;
    requestAnimationFrame(scalePreviews);
  }
  function openOverview() {
    buildOverview();
    overview.showModal();
    filterOverview();
    search.focus();
    if (!searchQuery) requestAnimationFrame(() => grid.querySelector('[aria-current="true"]')?.scrollIntoView({block:'center'}));
  }
  async function fullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen();
      else return;
    } catch {
      $('announcement').textContent = L.noFullscreen;
    }
  }
  $('previous').addEventListener('click', event => advance(-1, event.shiftKey));
  $('next').addEventListener('click', event => advance(1, event.shiftKey));
  $('step-mode').addEventListener('click', toggleSteps);
  $('open-overview').addEventListener('click', openOverview);
  $('close-overview').addEventListener('click', () => overview.close());
  $('toggle-notes').addEventListener('click', toggleNotes);
  $('open-help').addEventListener('click', () => help.showModal());
  $('close-help').addEventListener('click', () => help.close());
  $('fullscreen').addEventListener('click', fullscreen);
  if (!document.documentElement.requestFullscreen) $('fullscreen').hidden = true;
  $('print-deck').addEventListener('click', () => window.print());
  search.addEventListener('input', filterOverview);
  overview.addEventListener('click', event => { if (event.target === overview) overview.close(); });
  help.addEventListener('click', event => { if (event.target === help) help.close(); });
  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey) return;
    if (overview.open || help.open) return; // Native dialog handles Escape and traps focus.
    if (event.target.closest('input,textarea,select,[contenteditable="true"]')) return;
    // Enter/space must continue activating focused controls and links normally.
    if ((event.key === ' ' || event.key === 'Enter') && event.target.closest('button,a,summary')) return;
    const key = event.key.toLowerCase();
    if (['arrowright','arrowdown','pagedown',' '].includes(key)) { event.preventDefault(); advance(1, event.shiftKey); }
    else if (['arrowleft','arrowup','pageup'].includes(key)) { event.preventDefault(); advance(-1, event.shiftKey); }
    else if (key === 'home') { event.preventDefault(); show(0); }
    else if (key === 'end') { event.preventDefault(); show(slides.length - 1); }
    else if (key === 'a') { event.preventDefault(); toggleSteps(); }
    else if (key === 'g') { event.preventDefault(); openOverview(); }
    else if (key === 'n') { event.preventDefault(); toggleNotes(); }
    else if (key === 'f') { event.preventDefault(); fullscreen(); }
    else if (key === '?') { event.preventDefault(); help.showModal(); }
    else if (key === 'escape' && !notesPanel.hidden) toggleNotes();
  });
  $('stage').addEventListener('touchstart', event => {
    if (event.touches.length !== 1 || event.target.closest('a,button')) { touchStart = null; return; }
    touchStart = {x:event.touches[0].clientX, y:event.touches[0].clientY};
  }, {passive:true});
  $('stage').addEventListener('touchend', event => {
    if (!touchStart || event.changedTouches.length !== 1 || overview.open || help.open) return;
    const dx = event.changedTouches[0].clientX - touchStart.x;
    const dy = event.changedTouches[0].clientY - touchStart.y;
    if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.5) advance(dx < 0 ? 1 : -1);
    touchStart = null;
  }, {passive:true});
  $('stage').addEventListener('touchcancel', () => { touchStart = null; }, {passive:true});
  const started = performance.now();
  setInterval(() => {
    const seconds = Math.floor((performance.now() - started) / 1000);
    $('timer').textContent = `${String(Math.floor(seconds / 60)).padStart(2,'0')}:${String(seconds % 60).padStart(2,'0')}`;
  }, 1000);
  window.addEventListener('hashchange', fromHash);
  window.addEventListener('resize', () => { resize(); if (overview.open) scalePreviews(); });
  document.addEventListener('fullscreenchange', () => {
    $('fullscreen').setAttribute('aria-pressed', String(!!document.fullscreenElement));
    $('fullscreen').setAttribute('aria-label', document.fullscreenElement ? L.exitFullscreen : L.fullscreen);
    resize();
  });
  new ResizeObserver(resize).observe(stageArea);
  $('stage').dataset.loading = 'true';
  resize();
  document.fonts.ready.then(() => {
    isReady = true;
    $('stage').dataset.loading = 'false';
    fromHash();
    resize();
  });
})();

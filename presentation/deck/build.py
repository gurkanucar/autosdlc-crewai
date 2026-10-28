#!/usr/bin/env python3
"""Bundle editable HTML slides into one offline, dependency-free presentation."""
from pathlib import Path
from html import escape
import base64
import mimetypes
import json
import re

ROOT = Path(__file__).resolve().parent

def icon(paths):
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{paths}</svg>'

# The English deck: player texts, then the shell's Turkish strings replaced one by one (each must be found).
TEXT_EN = {'step': 'step', 'nextStep': 'Next step', 'nextSlide': 'Next slide',
           'stepsOn': 'Step by step is on. The right arrow shows the next item.',
           'stepsOff': 'Every item is shown. The right arrow goes to the next slide.',
           'noNotes': 'This slide has no speaker notes.', 'slide': 'Slide', 'found': 'slides found',
           'noFullscreen': 'This browser cannot switch to full screen. Use the browser\'s own full screen option.',
           'exitFullscreen': 'Exit full screen (F)', 'fullscreen': 'Full screen (F)'}
SHELL_EN = [
    ('<html lang="tr">', '<html lang="en">'),
    ('AutoSDLC: AI agent ekibiyle fikirden test edilmiş projeye. Çevrimdışı sunum.', 'AutoSDLC: from an idea to a tested project with a team of AI agents. Offline presentation.'),
    ('aria-label="Sunum başlığı"', 'aria-label="Presentation title"'), ('id="chapter-name">Başlangıç', 'id="chapter-name">Start'),
    ('aria-label="Geçen sunum süresi" title="Sayfa açıldığından bu yana geçen süre"', 'aria-label="Elapsed time" title="Time since the page was opened"'),
    ('aria-label="Klavye kısayolları" title="Klavye kısayolları (?)"', 'aria-label="Keyboard shortcuts" title="Keyboard shortcuts (?)"'),
    ('aria-label="Tam ekran (F)" aria-pressed="false" title="Tam ekran (F)"', 'aria-label="Full screen (F)" aria-pressed="false" title="Full screen (F)"'),
    ('<main class="stage-area" id="stage-area" aria-label="Sunum">', '<main class="stage-area" id="stage-area" aria-label="Presentation">'),
    ('aria-label="Konuşmacı notları" hidden><h2>Konuşmacı notları</h2>', 'aria-label="Speaker notes" hidden><h2>Speaker notes</h2>'),
    ('aria-label="Slayt kontrolleri"', 'aria-label="Slide controls"'), ('aria-label="Sunum ilerlemesi"', 'aria-label="Progress"'),
    ('title="Slayt dizini (G)">', 'title="Slide index (G)">'), ('<span class="button-label">Slaytlar</span>', '<span class="button-label">Slides</span>'),
    ('title="Konuşmacı notları (N)">', 'title="Speaker notes (N)">'), ('<span class="button-label">Notlar</span>', '<span class="button-label">Notes</span>'),
    ('aria-label="Önceki slayt" title="Önceki slayt (←)"', 'aria-label="Previous slide" title="Previous slide (←)"'),
    ('aria-label="Slayttaki adım"', 'aria-label="Step on the slide"'),
    ('aria-label="Sonraki slayt" title="Sonraki adım veya slayt (→)"', 'aria-label="Next slide" title="Next step or slide (→)"'),
    ('title="Adım adım anlatımı aç / kapat (A)"', 'title="Step by step on / off (A)"'), ('<span class="step-mode-label">Adım adım</span>', '<span class="step-mode-label">Step by step</span>'),
    ('Shift + → sonraki slayt', 'Shift + → next slide'), ('aria-label="Sunumu yazdır" title="Tüm slaytları yazdır"', 'aria-label="Print the presentation" title="Print every slide"'),
    ('<h2 id="overview-title">Slaytlar</h2>', '<h2 id="overview-title">Slides</h2>'),
    ('placeholder="Başlık, konu veya terim ara…" aria-label="Slaytlarda ara"', 'placeholder="Search titles, topics or terms…" aria-label="Search the slides"'),
    ('aria-label="Slayt dizinini kapat"', 'aria-label="Close the slide index"'), ('Bu aramayla eşleşen slayt bulunamadı.', 'No slide matches this search.'),
    ('<h2 id="help-title">Klavye kısayolları</h2>', '<h2 id="help-title">Keyboard shortcuts</h2>'), ('aria-label="Kısayolları kapat"', 'aria-label="Close the shortcuts"'),
    ('<span>Önceki / sonraki adım</span>', '<span>Previous / next step</span>'), ('<span>Sonraki adım</span><kbd>Boşluk</kbd>', '<span>Next step</span><kbd>Space</kbd>'),
    ('<span>Adımları geç, sonraki slayt</span>', '<span>Skip the steps, next slide</span>'), ('<span>Adım adım anlatımı aç / kapat</span>', '<span>Step by step on / off</span>'),
    ('<span>İlk / son slayt</span>', '<span>First / last slide</span>'), ('<span>Slayt dizini ve arama</span>', '<span>Slide index and search</span>'),
    ('<span>Konuşmacı notları</span><kbd>N</kbd>', '<span>Speaker notes</span><kbd>N</kbd>'), ('<span>Tam ekran</span><kbd>F</kbd>', '<span>Full screen</span><kbd>F</kbd>'),
    ('<span>Açık paneli kapat</span>', '<span>Close the open panel</span>'), ('<span>Telefonda gezin</span><span>Sağa / sola kaydır</span>', '<span>On a phone</span><span>Swipe left / right</span>'),
    ('Slaytlar sıralı gösteriliyor. Etkileşimli sunum için JavaScript’i etkinleştirin.', 'The slides are shown in order. Enable JavaScript for the interactive presentation.'),
]
LANGUAGES = {'tr': ('slides', 'index.html', 'title'), 'en': ('slides-en', 'index.en.html', 'title_en')}

def build(lang='tr'):
    folder, target, title_key = LANGUAGES[lang]
    meta = json.loads((ROOT / 'deck.json').read_text())
    meta['title'] = meta[title_key]
    order = meta['order']
    if len(order) != len(set(order)):
        raise ValueError('Duplicate slide IDs in deck.json')
    fragments = []
    for index, sid in enumerate(order):
        if not re.fullmatch(r'[a-z][a-z0-9-]*', sid):
            raise ValueError(f'Invalid slide ID: {sid}')
        source = (ROOT / folder / f'{sid}.html').read_text()
        match = re.search(r'<section\b[\s\S]*?</section>', source)
        if not match or f'id="{sid}"' not in match[0]:
            raise ValueError(f'Missing slide section: {sid}')
        # Hide before JavaScript runs to prevent a flash of all slides.
        section = match[0]
        if index:
            section = section.replace('<section ', '<section hidden ', 1)
        def embed_image(match):
            asset = ROOT / match[1].removeprefix('../')
            if not asset.resolve().is_relative_to((ROOT / 'assets').resolve()):
                raise ValueError('Image must be inside deck/assets')
            mime = mimetypes.guess_type(asset.name)[0] or 'application/octet-stream'
            data = base64.b64encode(asset.read_bytes()).decode('ascii')
            return f'src="data:{mime};base64,{data}"'
        section = re.sub(r'src="(\.\./assets/[^\"]+)"', embed_image, section)
        fragments.append(section)
    license_text = '\n\n'.join((ROOT / file).read_text() for file in ['fonts/Kalam-OFL.txt', 'fonts/Nunito-OFL.txt', 'vendor/rough-LICENSE'])
    license_text = '\n'.join(line.rstrip() for line in license_text.splitlines())
    css = '/* Bundled font and library licenses:\n' + license_text.replace('*/', '* /') + '\n*/\n' + (ROOT / 'styles.css').read_text()
    # Bundle fonts too: the supplied design must also match without internet.
    def embed_font(match):
        font_path = ROOT / match[1]
        encoded = base64.b64encode(font_path.read_bytes()).decode('ascii')
        return f'url("data:font/ttf;base64,{encoded}")'
    css = re.sub(r'url\("(fonts/[^\"]+)"\)', embed_font, css)
    js = '\n'.join((ROOT / file).read_text() for file in ['vendor/rough.js', 'whiteboard.js', 'player.js'])
    if re.search(r'</style', css, re.I) or re.search(r'</script', js, re.I):
        raise ValueError('Unsafe inline closing tag in player sources')
    prev = icon('<path d="m14 6-6 6 6 6"/>')
    nxt = icon('<path d="m10 6 6 6-6 6"/>')
    overview = icon('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>')
    notes = icon('<path d="M5 3h14v18H5zM8 7h8M8 11h8M8 15h5"/>')
    full = icon('<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>')
    close = icon('<path d="m6 6 12 12M6 18 18 6"/>')
    question = icon('<circle cx="12" cy="12" r="9"/><path d="M9.5 8.5a2.5 2.5 0 0 1 5 .5c0 2-2.5 2-2.5 4M12 17h.01"/>')
    printer = icon('<path d="M7 8V3h10v5M7 17H4V9h16v8h-3M7 14h10v7H7zM17 11h.01"/>')
    html = f'''<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="description" content="AutoSDLC: AI agent ekibiyle fikirden test edilmiş projeye. Çevrimdışı sunum.">
<meta name="theme-color" content="#fdfeff">
<link rel="icon" href="data:,">
<title>{escape(meta['title'])}</title>
<style>{css}</style>
</head>
<body class="player">
<header class="player-header" aria-label="Sunum başlığı">
  <div class="brand"><p>{escape(meta["title"])}</p></div>
  <span class="header-separator" aria-hidden="true"></span><span class="current-chapter" id="chapter-name">Başlangıç</span>
  <div class="header-actions"><span id="timer" class="timer" aria-label="Geçen sunum süresi" title="Sayfa açıldığından bu yana geçen süre">00:00</span><button class="icon-button" id="open-help" aria-label="Klavye kısayolları" title="Klavye kısayolları (?)">{question}</button><button class="icon-button" id="fullscreen" aria-label="Tam ekran (F)" aria-pressed="false" title="Tam ekran (F)">{full}</button></div>
</header>
<div class="workspace">
<main class="stage-area" id="stage-area" aria-label="Sunum">
<div class="stage-shell"><div class="stage" id="stage">
\0SLIDES\0
</div></div>
</main>
<aside class="notes-panel" id="notes-panel" aria-label="Konuşmacı notları" hidden><h2>Konuşmacı notları</h2><p class="notes-meta" id="notes-meta"></p><h3 id="notes-title"></h3><div id="notes-content"></div></aside>
</div>
<nav class="player-controls" aria-label="Slayt kontrolleri">
  <div id="progress-track" class="progress-track" role="progressbar" aria-label="Sunum ilerlemesi" aria-valuemin="1" aria-valuemax="{len(order)}" aria-valuenow="1"><div class="progress-bar" id="progress"></div></div>
  <div class="control-group"><button class="text-button" id="open-overview" aria-haspopup="dialog" aria-controls="overview" title="Slayt dizini (G)">{overview}<span class="button-label">Slaytlar</span></button><button class="text-button" id="toggle-notes" aria-pressed="false" aria-controls="notes-panel" title="Konuşmacı notları (N)">{notes}<span class="button-label">Notlar</span></button></div>
  <div class="control-group"><button class="icon-button" id="previous" aria-label="Önceki slayt" title="Önceki slayt (←)" disabled>{prev}</button><span class="slide-position" id="slide-position">01 / {len(order)}</span><span class="step-status" id="step-status" aria-label="Slayttaki adım"></span><button class="icon-button" id="next" aria-label="Sonraki slayt" title="Sonraki adım veya slayt (→)">{nxt}</button></div>
  <div class="control-group"><button class="text-button" id="step-mode" aria-pressed="true" title="Adım adım anlatımı aç / kapat (A)"><span aria-hidden="true">✎</span><span class="step-mode-label">Adım adım</span></button><span class="keyboard-hint">Shift + → sonraki slayt</span><button class="icon-button" id="print-deck" aria-label="Sunumu yazdır" title="Tüm slaytları yazdır">{printer}</button></div>
</nav>
<dialog class="overview-dialog" id="overview" aria-labelledby="overview-title"><header class="dialog-header"><h2 id="overview-title">Slaytlar</h2><input type="search" class="search-input" id="slide-search" placeholder="Başlık, konu veya terim ara…" aria-label="Slaytlarda ara"><button class="icon-button" id="close-overview" aria-label="Slayt dizinini kapat">{close}</button></header><div class="overview-body"><div class="overview-grid" id="overview-grid"></div><p class="no-results" id="no-results" hidden>Bu aramayla eşleşen slayt bulunamadı.</p></div><span class="live-region" id="search-status" role="status"></span></dialog>
<dialog class="help-dialog" id="help" aria-labelledby="help-title"><header class="dialog-header"><h2 id="help-title">Klavye kısayolları</h2><button class="icon-button" id="close-help" aria-label="Kısayolları kapat">{close}</button></header><div class="help-body"><div class="shortcut"><span>Önceki / sonraki adım</span><span><kbd>←</kbd> <kbd>→</kbd></span></div><div class="shortcut"><span>Sonraki adım</span><kbd>Boşluk</kbd></div><div class="shortcut"><span>Adımları geç, sonraki slayt</span><kbd>Shift + →</kbd></div><div class="shortcut"><span>Adım adım anlatımı aç / kapat</span><kbd>A</kbd></div><div class="shortcut"><span>İlk / son slayt</span><span><kbd>Home</kbd> <kbd>End</kbd></span></div><div class="shortcut"><span>Slayt dizini ve arama</span><kbd>G</kbd></div><div class="shortcut"><span>Konuşmacı notları</span><kbd>N</kbd></div><div class="shortcut"><span>Tam ekran</span><kbd>F</kbd></div><div class="shortcut"><span>Açık paneli kapat</span><kbd>Esc</kbd></div><div class="shortcut"><span>Telefonda gezin</span><span>Sağa / sola kaydır</span></div></div></dialog>
<p class="live-region" id="announcement" role="status" aria-live="polite" aria-atomic="true"></p>
<noscript><style>.reveal[data-step]{{visibility:visible!important;opacity:1!important}}.workspace{{height:auto}}.stage-area{{display:block}}.stage-shell,.stage{{position:static;width:auto;height:auto;transform:none}}.stage>.slide[hidden]{{display:flex!important}}body.player{{overflow:auto;height:auto}}.player-controls{{display:none}}</style>Slaytlar sıralı gösteriliyor. Etkileşimli sunum için JavaScript’i etkinleştirin.</noscript>
<script>{js}</script>
</body>
</html>
'''
    if lang == 'en':
        for turkish, english in SHELL_EN:
            if turkish not in html:
                raise ValueError(f'Shell text not found for the English deck: {turkish}')
            html = html.replace(turkish, english)
        html = html.replace('<script>', '<script>window.DECK_TEXT=' + json.dumps(TEXT_EN) + ';\n', 1)
    html = html.replace('\0SLIDES\0', '\n'.join(fragments))
    (ROOT / target).write_text(html)
    print(f'Built {len(order)} slides → {ROOT / target} ({len(html.encode()):,} bytes)')

if __name__ == '__main__':
    for language in LANGUAGES:
        build(language)

# AutoSDLC sunumu

**Sunumu aç:** [`index.html`](index.html) (Türkçe) · [`index.en.html`](index.en.html) (English). Her biri tek dosyadır; fontlar, görseller ve oynatıcı içine gömülüdür, internet gerekmez. Uygulamada **Sunum** sayfasında da açılır; arayüz diline göre Türkçe ya da İngilizce olanı gösterilir.

## Kontroller

- **← / →**, **boşluk**: önceki / sonraki anlatım adımı. **Shift + →**: sonraki slayt.
- **A**: adım adım anlatımı aç / kapat. **G**: slayt listesi ve arama. **N**: konuşmacı notları. **F**: tam ekran.
- `#hata-turkce` gibi adresler doğrudan o slaydı açar.

## Düzenleme

- `slides/*.html` (Türkçe) ve `slides-en/*.html` (English): her slayt ayrı dosyadır, iki dilde aynı kimlik ve adımlarla; konuşmacı notları `<aside class="speaker-notes">` içindedir.
- `deck.json`: slayt sırası. `styles.css`: tasarım (sondaki "AutoSDLC deck additions" bölümü bu sunuma özgüdür).
- `player.js`, `whiteboard.js`, `vendor/rough.js`, `fonts/`: oynatıcı ve el çizimi görünüm.

Değişiklikten sonra proje kökünden `python3 presentation/deck/build.py` çalıştırın; `index.html` ve `index.en.html` yeniden oluşur. Oynatıcının İngilizce metinleri `build.py` içindeki `TEXT_EN` ve `SHELL_EN` listelerindedir.

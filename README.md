# AutoSDLC

AutoSDLC, bir proje fikrini CrewAI Flow ve sekiz ajanla **Planlama → Analiz → Tasarım → İnceleme → Kodlama → Test → Dağıtım → Bakım** aşamalarından geçirir. Orkestrasyon Python ile yazılmıştır; **üretilen proje Python veya FastAPI ile sınırlı değildir**. Proje türünü ve teknoloji yığınını kullanıcı seçer (ör. Go/Gin, Node/Express, Python/Django, bir CLI veya kütüphane).

## Kurulum

Python 3.11–3.13, OpenCode Go erişimi ve üretilecek projelerin seçilen araçları gerekir. Docker Compose yalnızca konteynerle çalıştırma için gereklidir. Windows (Git Bash/PowerShell), macOS ve Linux desteklenir. macOS'ta `make` Xcode Command Line Tools ile gelir (`xcode-select --install`); Python için `brew install python@3.13` yeterlidir:

```bash
make run
```

İlk kullanımda Makefile `.venv` oluşturup paketi ve Playwright Chromium'u kurar; ardından tarayıcı arayüzünü otomatik açar. Yalnızca üç Make hedefi vardır: `setup`, `run`, `test`. İstersen kurulumu önceden `make setup` ile yapabilirsin. Aşağıda `VENV_PY`, sanal ortamın yorumlayıcısıdır: Windows'ta `.venv/Scripts/python.exe`, macOS/Linux'ta `.venv/bin/python`. `make` yoksa `python3 -m venv .venv` (Windows: `python -m venv .venv`), `VENV_PY -m pip install -e .` ve `VENV_PY -m playwright install chromium` kullan. Yerel `.env` içindeki `OPENCODE_API_KEY` otomatik okunur; `.env` Git tarafından yok sayılır. `.env.example` örnek biçimdir. Anahtarı YAML dosyalarına veya üretilen projeye yazma.

## Çalıştırma

`make run` tarayıcı arayüzünü açar. Proje fikrini, türünü, teknoloji yığınını,
paket yöneticisini ve diğer teknik kısıtları formda yazarsın. Arka uç/tam yığın
için veritabanı, önbellek ve gözlemlenebilirlik; arka uç için isteğe bağlı web
arayüzü seçilir.

Arayüzün yaşam döngüsü diyagramı sekiz aşamayı dört evrede (gereksinim
mühendisliği, tasarım ve doğrulama, gerçekleştirme, operasyon) gösterir. Her
düğüm canlı durumunu (sırada, çalışıyor, yanıt bekliyor, tamamlandı, başarısız)
ve ürettiği belgeyi taşır. Turuncu oklar geri besleme döngüleridir:
**İnceleme → Tasarım** (revizyon) ve **Test → Kodlama** (onarım); döngü
çalıştıkça sayaçları artar. Ana sayfa yeni projeyi başlatır ve aktif çalışmayı
canlı izletir. **Projeler** sayfasında her projenin sekmeleri vardır:

- **Yaşam döngüsü:** diyagram, metrikler (aşama, inceleme turu, test, onarım, FR kapsamı), ajan soruları, durdur/devam et.
- **Belgeler:** tüm SDLC belgelerini okuma ve düzenleme. **Kaydet ve sonraki aşamadan yeniden üret** düzenlenmiş belgeyi girdi kabul ederek sonraki aşamaları yeniden çalıştırır (ör. gereksinimleri elle düzelt → tasarımdan itibaren yeniden üret).
- **Kod:** üretilen dosyaları görüntüleme ve projeyi ZIP olarak indirme.
- **Olaylar:** aşama olaylarının zaman çizelgesi ve ajan soruları.
- **Yönet:** fikri ve kısıtları düzenleme, herhangi bir aşamadan itibaren yeniden üretme, projeyi silme.

Bir ajan kritik karar isterse ilgili aşama duraklar, soru görünür; yanıtı yazıp
**Yanıtla ve devam et** dersin. **Çalışmayı durdur** ile duraklatıp **Devam et**
ile tamamlanmış aşamaları yeniden üretmeden sürdürebilirsin; durdurma o sırada
süren ajan çağrısı bitince uygulanır. Çalışan iş her 10 saniyede bir kalp atışı
yazar. Arayüz kapanır ya da bilgisayar uyursa, iş kesintiye uğramış kabul edilip
duraklatılır; kayıt kilitli kalmaz, devam ettirilebilir veya silinebilir.
Terminal yalnızca yerel arayüz sunucusunu çalıştırır; kapatmak için `Ctrl+C`.
Tarayıcı kendiliğinden açılmazsa `http://127.0.0.1:8501` adresine git.

Varsayılan proje konumu bu deponun `projects/<proje-adı>-<run-id>/` klasörüdür.
`projects/` otomatik oluşturulur ve Git tarafından yok sayılır. Başka bir klasör istiyorsan
formdaki **Yeni proje klasörü** alanını doldur veya **Gözat…** ile mevcut üst
klasörü seçip yeni klasör adını yaz; hedef klasör henüz mevcut
olmamalı. Her çalıştırma ayrı proje ve SDLC belgeleri oluşturur, geçmiş
`runs/history.sqlite3` içinde saklanır. Sorular [`questions.yaml`](src/autosdlc/config/questions.yaml) dosyasındadır.
Arayüz, OpenCode Go'nun canlı listesinden bu uygulamanın API biçimiyle uyumlu
modelleri seçtirir. Yerel `.env` anahtarı varsa alan maskeli gösterilir; gerçek
değer tarayıcıya gönderilmez. İstersen farklı bir anahtar girebilirsin.

Kararları görünür bir YAML dosyasında tutmak istersen kendi `request.yaml` dosyanı oluştur:

```yaml
idea: Stok takibi için HTTP API
output: ~/projeler/stok-uygulamasi   # Windows: D:\projeler\stok-uygulamasi
decisions:
  project_type: fullstack
  stack: Node.js backend + sade HTML/CSS/JS arayüz
  constraints: Paket yöneticisi pnpm olsun; frontend çatısı kullanma.
  database: postgres
  cache: none
  observability: openobserve
```

YAML ile otomatik çalıştırma gerekiyorsa `VENV_PY -m autosdlc run --config request.yaml --no-prompt` komutunu kullan. Bu ayrı bir zorunlu adım değildir; normal kullanım tarayıcı arayüzüdür.

```bash
VENV_PY -m autosdlc run --idea "Kitap API'si" --project_type backend --stack "Go/Gin" --database postgres --cache redis --observability otel --ui none --no-prompt
```

`VENV_PY -m autosdlc history` ve `show <run-id>` komutları otomasyon için geçmişi verir; aynı bilgi arayüzde de görünür.

## Çıktılar ve belgeler

Her çalışmanın raporları `runs/<run-id>/` altında saklanır. Üretilen uygulama
varsayılan olarak `projects/<proje-adı>-<run-id>/` içindedir; oradaki `README.md`
belgeleri ve çalıştırma yolunu bağlar. Aynı belgelerin kopyası projenin `docs/`
klasöründedir:

| Aşama | Dosya | İçerik |
| --- | --- | --- |
| Kararlar | `decisions.yaml` | Proje türü, yığın ve mimari seçimler |
| Aşama soruları | `clarifications.yaml` | Ajanların istediği ek kararlar ve kullanıcı yanıtları; yalnızca soru sorulursa |
| Planlama | `planning.md` | Kapsam, paydaşlar, takvim, riskler |
| Analiz | `requirements.md` | FR/NFR kimlikleri, user story, kabul kriterleri, test izlenebilirliği |
| Tasarım | `architecture.md` | Bileşen/sekans Mermaid diyagramları, varsa ER şeması ve API sözleşmesi |
| İnceleme | `design_review.md` | Bağımsız mimari inceleme: önem dereceli bulgu tablosu ve ONAY/REVİZYON kararı |
| Kodlama | `project/` | Seçilen teknolojiyle kaynak kod ve `project.yaml` |
| Test | `traceability.md` | FR/NFR → test dosyası izlenebilirlik matrisi ve kapsama oranı |
| Test | `test_report.md`, `test_attempts/` | Gerçek komut sonuçları, başarısızlık ve düzeltme döngüsü |
| Dağıtım | `deployment.md`, `project/Dockerfile`, `project/compose.yaml` | Çalıştırma, ortam değişkenleri, sağlık ve geri alma adımları |
| Bakım | `maintenance_report.md` | İzleme, olay müdahalesi, yedekleme, iyileştirme listesi |

CrewAI Flow sıralamayı yönetir. Ajan rolleri [`agents.yaml`](src/autosdlc/config/agents.yaml), görevleri ve belge gereksinimleri [`tasks.yaml`](src/autosdlc/config/tasks.yaml) içindedir. `project.yaml` seçilen yığına ait kurulum, API, UI, birim test ve başlatma komutlarını listeler. Komutlar kabuk kullanılmadan proje klasöründe çalıştırılır. Backend/fullstack için API testi zorunludur; UI varsa Playwright testi canlı uygulamaya karşı çalışır. Başarısız test sonucu Developer Agent'a gider; en fazla iki onarım denemesi yapılır. Rapor gerçek test sonucunu gösterir.

### Kalite kapıları ve izlenebilirlik

- **Mimari inceleme (V&V):** Mimari İnceleme Ajanı tasarımı gereksinimlere (her FR'nin karşılığı, NFR'ler, API/veri tutarlılığı, kararlara uyum) göre denetler. Kritik veya yüksek bulgu varsa `KARAR: REVİZYON` verir; Tasarım Ajanı bulgulara göre belgeyi yeniden yazar ve inceleme tekrarlanır (en fazla 2 tur). Onay çıkmazsa açık bulgular kaydedilerek devam edilir ve kodlama ile bakım ajanlarına aktarılır.

- **Aşama kapıları:** Analiz belgesi FR-xx ve NFR-xx kimlikleri, tasarım belgesi en az iki Mermaid diyagramı (veritabanı seçildiyse `erDiagram`) içermelidir. Kapıdan geçemeyen belge, eksiği yazılı olarak ajana geri verilir ve bir kez yeniden yazdırılır; yine geçemezse çalışma durur.
- **İzlenebilirlik matrisi:** Test aşaması `traceability.md` üretir. Gereksinim kimlikleri test dosyalarındaki referanslarla (`FR-01`, `test_fr01_...`) eşleştirilir ve FR/NFR kapsama oranı test raporuna ve dağıtım/bakım ajanlarına aktarılır.
- **Testlerin korunması:** Onarım döngüsünde Geliştirme Ajanı `tests/` ve `test/` dosyalarını değiştiremez; bu tür değişiklikler reddedilir ve olay olarak kaydedilir. Böylece testler değiştirilerek "geçirilemez".
- **Aşamaya özgü bağlam:** Her ajan yalnızca ilgili önceki belgeleri alır (ör. Kodlama: tasarım + gereksinimler). Kullanıcı yanıtları ve gerçek test sonucu bağlamın başına konur; uzun belgeler kırpılsa bile kaybolmaz.

## Docker Compose

Her proje bir Dockerfile ve Compose dosyası alır. Veritabanı olarak Postgres seçilirse `db`, Redis seçilirse `redis`, OpenTelemetry seçilirse Collector, OpenObserve seçilirse Collector ve OpenObserve servisleri eklenir. Gerekli değişkenler `project/.env.example` içindedir. Değerleri `project/.env` içine koy:

Proje klasörüne girip `docker compose config`, `docker compose up --build -d` ve gerektiğinde `docker compose down` kullan.

OpenObserve seçilirse `OPENOBSERVE_BASIC_AUTH`, `ZO_ROOT_USER_EMAIL:ZO_ROOT_USER_PASSWORD` dizgesinin Base64 kodlanmış hâlidir. Uygulama telemetriyi `otel-collector:4318` adresine göndermelidir; seçilen dilin SDK ve enstrümantasyonunu Developer Agent sağlar. Yayımlamadan önce Dockerfile, Compose ve uygulama kodunu gözden geçir.

## Arayüz ve yönetim API'si

Tarayıcı arayüzü `make run` ile açılır. Yönetim API'si gerekiyorsa `VENV_PY -m uvicorn autosdlc.api:app --host 127.0.0.1 --port 8000` kullanabilirsin.

Streamlit arayüzü fikir ve mimari seçimleri toplar; çalışma geçmişini ve belgeleri gösterir. Yönetim API'sinde `GET /questions`, `POST /runs`, `GET /runs`, `GET /runs/{id}` ve `GET /health` bulunur. `POST /runs` isteği `decisions` alanında gerekli seçimleri alır ve çalışma bitene kadar bekler. API belgeleri `http://127.0.0.1:8000/docs` adresindedir.

## Geliştirme doğrulaması

```bash
make test
```

AI tarafından üretilen kod ve testler yerel alt süreçte çalışır. Bu araç üretilen kod için güvenlik sandbox'ı sağlamaz; güvenilmeyen fikir veya promptları çalıştırmadan ve dağıtmadan önce incele.

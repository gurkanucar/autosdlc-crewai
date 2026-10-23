# AutoSDLC

AutoSDLC, bir proje fikrini CrewAI Flow ve yedi ajanla **Planlama → Analiz → Tasarım → Kodlama → Test → Dağıtım → Bakım** aşamalarından geçirir. Orkestrasyon Python ile yazılmıştır; **üretilen proje Python veya FastAPI ile sınırlı değildir**. Proje türünü ve teknoloji yığınını kullanıcı seçer (ör. Go/Gin, Node/Express, Python/Django, bir CLI veya kütüphane).

## Kurulum

Python 3.11–3.13, OpenCode Go erişimi ve üretilecek projelerin seçilen araçları gerekir. Docker Compose yalnızca konteynerle çalıştırma için gereklidir. Git Bash veya PowerShell'de:

```bash
make run
```

İlk kullanımda Makefile `.venv` oluşturup paketi ve Playwright Chromium'u kurar, ardından proje fikrini ve kararları sorar. Yalnızca üç Make hedefi vardır: `setup`, `run`, `test`. İstersen kurulumu önceden `make setup` ile yapabilirsin. `make` yoksa `python -m venv .venv`, `.venv/Scripts/python.exe -m pip install -e .` ve `.venv/Scripts/python.exe -m playwright install chromium` kullan. Yerel `.env` içindeki `OPENCODE_API_KEY` otomatik okunur; `.env` Git tarafından yok sayılır. `.env.example` örnek biçimdir. Anahtarı YAML dosyalarına veya üretilen projeye yazma.

## Çalıştırma

```powershell
make run IDEA="Bir kitap takip uygulaması"
```

CLI sırasıyla proje türünü, teknoloji yığınını ve ilgili mimari kararları sorar. Backend/fullstack için veritabanı, önbellek ve gözlemlenebilirlik; backend için isteğe bağlı web arayüzü sorulur. Frontend/fullstack projelerde arayüz zaten vardır. Sorular [`questions.yaml`](src/autosdlc/config/questions.yaml) içindedir. Etkileşimsiz kullanım:

Varsayılan proje konumu `runs/<run-id>/project` olur. Başka bir klasör istiyorsan (klasör henüz bulunmamalı):

```powershell
make run IDEA="Bir kitap takip uygulaması" OUTPUT="D:\\software_projects\\kitap-uygulamasi"
```

Bu araç tekrar kullanılabilir: her çalıştırma ayrı bir proje ve SDLC belgeleri oluşturur, geçmiş `runs/history.sqlite3` içinde saklanır. `--output` yalnızca proje klasörünün konumunu değiştirir; çalışma raporları yine `runs/<run-id>/` içinde de saklanır.

Kararları görünür bir YAML dosyasında tutmak istersen kendi `request.yaml` dosyanı oluştur:

```yaml
idea: Stok takibi için HTTP API
output: D:\software_projects\stok-uygulamasi
decisions:
  project_type: backend
  stack: Go / Gin
  database: postgres
  cache: redis
  observability: otel
  ui: none
```

Ardından `make run REQUEST=request.yaml` çalıştır. Dosyadaki her anahtar isteğe bağlıdır; eksik mimari kararları CLI sorar. API veya Streamlit kullanıyorsan aynı seçimleri formda/JSON isteğinde belirtirsin.

```powershell
python -m autosdlc run --idea "Kitap API'si" --project_type backend --stack "Go/Gin" --database postgres --cache redis --observability otel --ui none --no-prompt
```

`make run` için ek bayraklar `ARGS="--project_type ..."` ile verilebilir. `python -m autosdlc history` ve `python -m autosdlc show <run-id>` geçmişi gösterir.

## Çıktılar ve belgeler

Her çalışma `runs/<run-id>/` altında saklanır. Üretilen uygulama `project/` içindedir; `project/README.md` belgeleri ve çalıştırma yolunu bağlar. Aynı belgelerin kopyası `project/docs/` içindedir:

| Aşama | Dosya | İçerik |
| --- | --- | --- |
| Kararlar | `decisions.yaml` | Proje türü, yığın ve mimari seçimler |
| Planlama | `planning.md` | Kapsam, paydaşlar, takvim, riskler |
| Analiz | `requirements.md` | FR/NFR kimlikleri, user story, kabul kriterleri, test izlenebilirliği |
| Tasarım | `architecture.md` | Bileşen/sekans Mermaid diyagramları, varsa ER şeması ve API sözleşmesi |
| Kodlama | `project/` | Seçilen teknolojiyle kaynak kod ve `project.yaml` |
| Test | `test_report.md`, `test_attempts/` | Gerçek komut sonuçları, başarısızlık ve düzeltme döngüsü |
| Dağıtım | `deployment.md`, `project/Dockerfile`, `project/compose.yaml` | Çalıştırma, ortam değişkenleri, sağlık ve geri alma adımları |
| Bakım | `maintenance_report.md` | İzleme, olay müdahalesi, yedekleme, iyileştirme listesi |

CrewAI Flow sıralamayı yönetir. Ajan rolleri [`agents.yaml`](src/autosdlc/config/agents.yaml), görevleri ve belge gereksinimleri [`tasks.yaml`](src/autosdlc/config/tasks.yaml) içindedir. `project.yaml` seçilen yığına ait kurulum, API, UI, birim test ve başlatma komutlarını listeler. Komutlar kabuk kullanılmadan proje klasöründe çalıştırılır. Backend/fullstack için API testi zorunludur; UI varsa Playwright testi canlı uygulamaya karşı çalışır. Başarısız test sonucu Developer Agent'a gider; en fazla iki onarım denemesi yapılır. Rapor gerçek test sonucunu gösterir.

## Docker Compose

Her proje bir Dockerfile ve Compose dosyası alır. Veritabanı olarak Postgres seçilirse `db`, Redis seçilirse `redis`, OpenTelemetry seçilirse Collector, OpenObserve seçilirse Collector ve OpenObserve servisleri eklenir. Gerekli değişkenler `project/.env.example` içindedir. Değerleri `project/.env` içine koy:

Proje klasörüne girip `docker compose config`, `docker compose up --build -d` ve gerektiğinde `docker compose down` kullan.

OpenObserve seçilirse `OPENOBSERVE_BASIC_AUTH`, `ZO_ROOT_USER_EMAIL:ZO_ROOT_USER_PASSWORD` dizgesinin Base64 kodlanmış hâlidir. Uygulama telemetriyi `otel-collector:4318` adresine göndermelidir; seçilen dilin SDK ve enstrümantasyonunu Developer Agent sağlar. Yayımlamadan önce Dockerfile, Compose ve uygulama kodunu gözden geçir.

## Arayüz ve yönetim API'si

Streamlit arayüzünü `python -m streamlit run streamlit_app.py`, yönetim API'sini `python -m uvicorn autosdlc.api:app --host 127.0.0.1 --port 8000` ile açabilirsin.

Streamlit arayüzü fikir ve mimari seçimleri toplar; çalışma geçmişini ve belgeleri gösterir. Yönetim API'sinde `GET /questions`, `POST /runs`, `GET /runs`, `GET /runs/{id}` ve `GET /health` bulunur. `POST /runs` isteği `decisions` alanında gerekli seçimleri alır ve çalışma bitene kadar bekler. API belgeleri `http://127.0.0.1:8000/docs` adresindedir.

## Geliştirme doğrulaması

```powershell
make test
```

AI tarafından üretilen kod ve testler yerel alt süreçte çalışır. Bu araç üretilen kod için güvenlik sandbox'ı sağlamaz; güvenilmeyen fikir veya promptları çalıştırmadan ve dağıtmadan önce incele.

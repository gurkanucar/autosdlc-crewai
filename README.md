# AutoSDLC

CrewAI ajanlarıyla SDLC sürecini çalıştıran ders projesi. Kullanıcı küçük bir
FastAPI + SQLite uygulaması fikri verir; sistem planlama, analiz, tasarım, kod,
test, dağıtım ve bakım çıktılarını oluşturur. Test başarısız olursa hata çıktısı
Developer Agent'a döner. En fazla iki düzeltme yapılır.

## Akış

```text
Fikir → Planning → Analysis → Design → Developer → Tester + pytest
                                          ↑                │
                                          └── hata/düzeltme ┘
                                              │
                                        Deployment → Maintenance
```

CrewAI Flow aşama sırasını yönetir. Beş CrewAI ajanının rolü ve hedefi
[`agents.yaml`](src/autosdlc/config/agents.yaml), görevleri ve beklenen çıktıları
[`tasks.yaml`](src/autosdlc/config/tasks.yaml) içindedir. Bu dosyaları düzenleyerek
ajan talimatlarını değiştirebilirsin. Dağıtım ve bakım belgelerini kontrollü
Python adımları oluşturur.

## Gereksinimler

- Python 3.11–3.13 (önerilen: 3.12)
- OpenCode Go üyeliği ve API anahtarı
- Paket kurulumu ve AI çağrıları için internet
- Üretilen uygulamayı konteynerde denemek istersen Docker

## Kurulum (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

PowerShell aktivasyonu engellerse aşağıdaki komutlarda `python` yerine
`.\.venv\Scripts\python.exe` kullan.

## Gerçek CrewAI çalıştırması

Anahtarı yalnızca yerel ortam değişkeninde tut. Repoya veya YAML dosyalarına
yazma. PowerShell'de:

```powershell
$env:OPENCODE_API_KEY = Read-Host "OpenCode Go API key"
autosdlc run --idea "Kütüphane kitaplarını takip eden küçük bir API"
autosdlc history
```

Varsayılan model `glm-5.3-flash` ve OpenCode Go'nun Chat Completions API'si
kullanılır. Model değiştirmek için `--model <model-id>` veya `AUTOSDLC_MODEL`
kullan. Modelin [OpenCode Go belgelerinde](https://dev.opencode.ai/v2/docs/console/go)
Chat Completions endpoint'inde listelenmesi gerekir.

Bir çalıştırma `runs/<run-id>/` klasöründe şu çıktıları oluşturur:

| SDLC aşaması | Çıktı |
| --- | --- |
| Planlama | `planning.md` |
| Analiz | `requirements.md` |
| Tasarım | `architecture.md` |
| Geliştirme | `project/app/*.py` |
| Test | `project/tests/*.py`, `test_report.md`, `test_attempts/` |
| Dağıtım | `project/Dockerfile`, `deployment.md` |
| Bakım | `maintenance_report.md` |

`runs/history.sqlite3` her çalıştırmanın durumunu ve aşama olaylarını saklar.
Bir kaydı incelemek için `autosdlc show <run-id>` komutunu kullan.

## Arayüz ve API

```powershell
streamlit run streamlit_app.py
uvicorn autosdlc.api:app --reload
```

Streamlit ekranında fikir, model ve API anahtarını girip çalıştırabilirsin.
Aşama geçmişi ve Markdown çıktılar ekranda görünür. FastAPI yönetim API'si
`GET /health`, `POST /runs`, `GET /runs`, `GET /runs/{id}` sunar;
`http://127.0.0.1:8000/docs` üzerinden denenebilir. `POST /runs` iş bitene
kadar bekler. Sunucuyu repo kökünden aç, böylece aynı `runs/` klasörünü kullanır.
Bu akademik API'de kimlik doğrulama yok; internete açık sunucu olarak kullanma.

## Üretilen uygulamayı çalıştırma

Başarılı bir çalıştırmanın `project/` klasöründe:

```powershell
cd runs\<run-id>\project
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

Uygulamanın API belgeleri `http://127.0.0.1:8000/docs` adresindedir. Docker
ile denemek için aynı klasörden:

```powershell
docker build -t autosdlc-example .
docker run --rm -p 8000:8000 -v task_data:/data autosdlc-example
```

## Geliştirici doğrulaması

```powershell
python -m pytest -q
```

Gerçek AI çıktısı her seferinde değişebilir. Testler iki düzeltmeden sonra hâlâ
başarısızsa sonuç `failed` olur ve hata ayrıntıları `test_report.md` içindedir.
Üretilen Python kodu ve testleri yerel alt süreçte çalışır. Bu akademik proje
bir güvenlik sandbox'ı değildir; üretilen kodu paylaşmadan veya dağıtmadan önce
incele.

# AutoSDLC

[English](README.md) · [Türkçe](README.tr.md)

AutoSDLC takes a project idea through **Planning → Analysis → Design → Review → Development → Testing → Deployment → Maintenance** with CrewAI Flow and eight agents. The orchestration is written in Python, but **the generated project is not limited to Python or FastAPI**. The user chooses the project type and technology stack (e.g. Go/Gin, Node/Express, Python/Django, a CLI or a library). Generated code is always English (variable, function, class and file names and comments). The documents are written in the language you choose for the project (English or Turkish, see [Languages](#languages)); established technical terms such as backend, fullstack and endpoint are never translated. These rules live in the `language_rules` block of [`tasks.yaml`](src/autosdlc/config/tasks.yaml).

## Setup

You need Python 3.11–3.13, OpenCode Go access and the tools of the projects you generate. Docker Compose is only needed to run a project in containers. Windows (Git Bash/PowerShell), macOS and Linux are supported. On macOS `make` comes with the Xcode Command Line Tools (`xcode-select --install`); `brew install python@3.13` is enough for Python:

```bash
make run
```

On first use the Makefile creates `.venv`, installs the package and Playwright Chromium, and then opens the browser UI automatically. There are only three Make targets: `setup`, `run`, `test`. You can do the setup beforehand with `make setup`. Below, `VENV_PY` is the virtual environment's interpreter: `.venv/Scripts/python.exe` on Windows, `.venv/bin/python` on macOS/Linux. Without `make`, use `python3 -m venv .venv` (Windows: `python -m venv .venv`), `VENV_PY -m pip install -e .` and `VENV_PY -m playwright install chromium`. `OPENCODE_API_KEY` in the local `.env` is read automatically; `.env` is ignored by Git and `.env.example` shows the format. Never write the key into YAML files or the generated project.

## Languages

AutoSDLC is fully localized; English is the default and Turkish is available. There are two independent settings:

- **Interface language:** the **Interface language** selector in the sidebar switches the whole UI at once (also remembered in the URL as `?lang=tr`). For the command line use the `AUTOSDLC_LANG` environment variable.
- **Document language:** a project decision (**Document language** in the new-project form, `language` in `decisions`; `--language` on the command line). Every agent writes its documents in that language (Turkish project → Turkish plan, requirements, design, review, deployment and maintenance documents), and the reports the application writes itself (test report, traceability matrix, changelog, project README) follow it too. Agent prompts are English; the language is added to every prompt as a rule. By default the document language follows the interface language until you pick one.

**Technical jargon is never translated, in any language.** Terms such as backend, frontend, fullstack, CLI, library, framework, API, endpoint, HTTP request/response, middleware, pipeline, container, package manager, mock, commit, branch and repository stay English even in Turkish text (never "arka uç" or "tam yığın"). The list lives in [`glossary.yaml`](src/autosdlc/config/glossary.yaml); it feeds the agent prompts, and a test checks that no translation catalog changes these terms. Protocol tokens the agents write (`VERDICT`, `START`, `SKIP`, `QUESTION`) are language neutral; the Turkish tokens of older runs are still accepted.

To add a language: create `src/autosdlc/config/locales/<code>/` with `English text: translation` mappings (copy `tr/` as a template), add the code to `LANGUAGES` and `PROMPT_NAMES` in [`i18n.py`](src/autosdlc/i18n.py) and to the `language` options in `questions.yaml`. A missing translation falls back to English, and `tests/test_i18n.py` lists every string that still needs one.

## Running

`make run` opens the browser UI. You write the project idea, type, technology stack, package manager and other technical constraints in the form. For backend/fullstack you choose database, cache and observability; for backend an optional web UI.

The UI's lifecycle diagram shows the eight stages in four phases (requirements engineering, design and verification, implementation, operations). Every node carries its live state (pending, running, waiting for an answer, completed, failed, skipped) and the document it produces. The orange arrows are feedback loops: **Review → Design** (revision) and **Testing → Development** (repair); their counters rise as the loops run.

On the **New project** page you can fill the form yourself or pick an **Example project template** and press **Apply**. The templates are in [`templates.yaml`](src/autosdlc/config/templates.yaml). When you start, the **Projects** page opens; projects are listed in the sidebar. The workspace updates itself without refreshing the page:

- **Diagram and stage picker:** clicking a stage opens that stage's detail. If you do not pick one, the view follows the active stage.
- **Metrics:** completed stages, total agent time, review rounds, test runs, repairs and FR test coverage. A stage's detail shows its duration and how many times it ran (repair/revision rounds included). The time an agent waits for your answer is not counted.
- **Stage detail:** in a running stage the agent's output **streams live** (the LLM response is received as a stream). In a finished stage the produced document (Mermaid diagrams drawn), in Development a file viewer and ZIP download are shown. Documents can be edited with **✏️ Whole**; saving marks later stages out of date.
- **Agent feed (chat):** the agents' progress messages, questions and your answers. If an agent needs a decision the question appears here and the text you type in the chat box goes back as the answer. If there is no question, what you type is saved as a **steering note** and added to the context of later stages (e.g. "the UI should use a dark theme").
- **Top bar:** ⏸ Stop, ▶ Resume, ↻ Retry, 📝 Changes, 📘 README, 🕓 Versions, 📂 Folder and ⚙️ Manage (edit the idea/constraints, regenerate from a stage, delete the project).

**Downloading outputs:** from the **Download** menu next to the README you can download the whole project and SDLC outputs as a single ZIP, or only the documents/reports, or only the source code, as separate bundles. In the full bundle `project/` holds the sources and `sdlc/` all stage outputs. For unfinished runs the files produced so far are taken. Local `.env` files and dependency folders are left out; `.env.example` is kept. Every document also has a single-file download button next to **Enlarge**. When the README is open at full width you can use the **Download** button at the top.

You can open the lifecycle node graph with **Enlarge**, adjust its size and save it as an image with **Download SVG**. In the enlarged view the states update live. Documents and Mermaid diagrams expand to their content height; read long documents with the page scrollbar.

Stopping takes effect when the running agent call finishes; when you resume, completed stages are not regenerated. A running job writes a heartbeat every 10 seconds. If the UI closes or the computer sleeps, the job is considered interrupted and paused; the record is not left locked and can be resumed or deleted. The terminal only runs the local UI server; press `Ctrl+C` to close it. If the browser does not open by itself go to `http://127.0.0.1:8501`.

The default project location is the `projects/<project-name>-<run-id>/` folder of this repository. `projects/` is created automatically and ignored by Git. If you want another folder fill in the **Project folder** field of the form, or use **Browse…** to pick an existing parent folder and type the new folder name; the target folder must not exist yet. Every run creates separate project and SDLC documents; the history is kept in `runs/history.sqlite3`. The questions are in [`questions.yaml`](src/autosdlc/config/questions.yaml). The UI lets you choose, from OpenCode Go's live list, models compatible with this application's API format. If a local `.env` key exists the field is shown masked; the real value is not sent to the browser. You can enter a different key if you want.

If you want to keep the decisions in a visible YAML file, create your own `request.yaml`:

```yaml
idea: HTTP API for stock tracking
output: ~/projects/stock-app   # Windows: D:\projects\stock-app
decisions:
  project_type: fullstack
  stack: Node.js backend + plain HTML/CSS/JS interface
  constraints: Use pnpm as the package manager; no frontend framework.
  database: postgres
  cache: none
  observability: openobserve
  scale: normal
  language: en            # document language: en or tr
```

If you need to run it automatically from YAML use `VENV_PY -m autosdlc run --config request.yaml --no-prompt`. This is not a separate required step; normal use is the browser UI.

```bash
VENV_PY -m autosdlc run --idea "Book API" --project_type backend --stack "Go/Gin" --database postgres --cache redis --observability otel --ui none --language en --no-prompt
```

The `VENV_PY -m autosdlc history` and `show <run-id>` commands give the history for automation; the same information is also in the UI.

## Outputs and documents

Every run's reports are stored under `runs/<run-id>/`. The generated application is by default in `projects/<project-name>-<run-id>/`; the `README.md` there links the documents and the way to run it. Copies of the same documents are in the project's `docs/` folder:

| Stage | File | Content |
| --- | --- | --- |
| Decisions | `decisions.yaml` | Project type, stack and architecture choices |
| Stage questions | `clarifications.yaml` | Extra decisions the agents asked for and the user's answers; only if a question was asked |
| Planning | `planning.md` | Scope, stakeholders, schedule, risks |
| Analysis | `requirements.md` | FR/NFR ids, user stories, acceptance criteria, test traceability |
| Design | `architecture.md` | Component/sequence Mermaid diagrams, ER schema and API contract if any |
| Review | `design_review.md` | Independent architecture review: findings table by severity and the APPROVE/REVISE verdict |
| Development | `project/` | Source code with the chosen technology and `project.yaml` |
| Testing | `traceability.md` | FR/NFR → test file traceability matrix and coverage ratio |
| Testing | `test_report.md`, `test_attempts/` | Real command results, failures and the repair loop |
| Deployment | `deployment.md`, `project/Dockerfile`, `project/compose.yaml` | Running, environment variables, health checks and rollback steps |
| Maintenance | `maintenance_report.md` | Monitoring, incident response, backup, improvement list |

CrewAI Flow manages the sequence. The agent roles are in [`agents.yaml`](src/autosdlc/config/agents.yaml), the tasks and document requirements in [`tasks.yaml`](src/autosdlc/config/tasks.yaml). `project.yaml` lists the setup, API, UI, unit test and start commands of the chosen stack. The commands run in the project folder without a shell. An API test is mandatory for backend/fullstack; with a UI the Playwright test runs against the live application. A failing test result goes to the Development Agent; at most two repair attempts are made. The report shows the real test result.

### Quality gates and traceability

- **Project scale:** the **Scale** option of the form can be `Normal` or `Small` (the quick demo, CLI and library templates come as `Small`). In a small project every document is limited to about 3500 characters and the review sends the design back only on a **Critical** finding; high/medium findings stay in the report and are passed to the maintenance list.
- **Architecture review (V&V):** the Architecture Review Agent audits the design against the requirements (a counterpart for every FR, NFRs, API/data consistency, compliance with decisions). If there is a critical or high finding it gives `VERDICT: REVISE`; the Design Agent rewrites only the sections of the document affected by the findings and the review is repeated (at most 2 rounds). If no approval comes, the open findings are recorded and passed to the development and maintenance agents.
- **Stage gates:** the analysis document must contain FR-xx and NFR-xx ids, the design document at least two Mermaid diagrams (`erDiagram` if a database is chosen). A document that fails the gate is given back to the agent with the gap in writing and rewritten once; if it still fails the run stops.
- **Traceability matrix:** the testing stage produces `traceability.md`. Requirement ids are matched with the references in the test files (`FR-01`, `test_fr01_...`) and the FR/NFR coverage ratio is passed to the test report and the deployment/maintenance agents.
- **Protected tests:** in the repair loop the Development Agent cannot change the `tests/` and `test/` files; such changes are rejected and recorded as events. So the tests cannot be "made to pass" by changing them.
- **Stage-specific context:** every agent receives only the relevant earlier documents (e.g. Development: design + requirements). User answers and the real test result are put at the head of the context; they are not lost even if long documents are truncated.
- **Piece-by-piece code generation:** in the first development the Development Agent first produces a file plan and then writes the files in groups of four; every group sees the earlier files. Big (e.g. fullstack) projects do not hit the model's output limit in a single response. If a response is still cut, this is reported clearly and the agent is asked for a shorter response.
- **Configuration repair:** if `project.yaml`/`Dockerfile` cannot be validated the error is given back to the Development Agent to fix once; unused commands may be left empty (`ui_test: []`).
- **Test coverage check:** if the `project.yaml` test commands do not run some test files (e.g. a command targets a single file) this shows as a warning in the test report and the events; a test that does not run does not count as passed.

## Change management

The SDLC continues after the project is finished. There are three kinds of intervention.

**1. Section-level intervention in a document.** In a stage's document switch to **🎯 Section** mode; pick a heading (`§`) or a single requirement (`📌 FR-02`):

| Action | What it does |
| --- | --- |
| 💬 Ask | Asks that stage's agent a question about the section; the document does not change, the answer also lands in the agent feed. |
| ✍️ Rewrite | Has the section rewritten according to the instruction (e.g. "make the acceptance criteria Given/When/Then"). |
| ➕ Add | Has new content written below the section (e.g. a new FR; the number is automatically the next one). |
| 🗑️ Delete / ✏️ Manual | Deletes the section or you edit it by hand. |

The agent's proposal is shown as a **diff**; if you **Accept** it is saved. The document's previous version is backed up under `runs/<id>/versions/`.

**2. Impact: "out of date".** When a document changes, later stages are not regenerated automatically; they are marked in yellow **↻ Out of date** in the diagram. **Update affected stages** in the warning offers two options:
- **Delta:** the existing documents and code are kept; the agents return not the whole document but only the changed or new sections, which are placed into the document by heading match (changed places are tagged `[CR-xxx]`). Because the output stays short the iteration is fast; only changed files are written in the code, then all tests run as regression.
- **Regenerate:** these stages are written from scratch.

Changing the idea/constraints marks all stages "out of date".

**3. Change request (new feature / bug / change).** **📝 Changes** in the top bar:
1. Create a request (`CR-001`, its type and description).
2. **Impact analysis:** the Impact Analysis Agent reports the affected requirements, design, files and tests through the traceability chain (FR → design → file → test) and suggests a starting stage (new/changed requirement → Analysis, design only → Design, code/bug only → Development). It also suggests the **stages not affected** by the change (`SKIP: review, deployment, maintenance`); this list can be edited on the approval screen. A skipped stage appears in the diagram with a dashed outline as **↷ Skipped** and keeps its previous document. Development and testing are never skipped.
3. **Open questions:** the impact analysis lists unclear points as `QUESTION:` lines (e.g. "Can Kelvin be negative?"). It cannot be applied until all are answered; the answers are saved on the request and go to all stages as firm decisions. So clarification is not left to whether an agent happens to ask.
4. **Approve and apply:** the delta iteration runs from the starting stage on; earlier stages stay as they are.
   - **New feature:** the new requirement is added as `FR-0n [CR-001]`, design and code are updated, new tests are added to the existing ones.
   - **Bug (TDD):** the Test Agent first writes a regression test that reproduces the bug and it is verified that the test **fails**; the Development Agent fixes the code with that failing test output; then all tests run.
5. The result is written to `CHANGELOG.md`; the request becomes **Completed** or **Failed**. In the diagram the loop-backs (revision, repair) stay visible permanently with their counters; stages that ran more than once carry a ×N badge.

**Version history.** Every finished iteration — first generation, every CR, every bugfix, successful or failed — is stored as a version (`v1 · Initial version`, `v2 · CR-001 · New feature: …`). With the version, a copy of the documents and code at that moment (`runs/<id>/iterations/v<n>/`) and the event range are kept. In the **🕓 Versions** window of the top bar, picking a version shows the lifecycle diagram at that moment, the agent time spent in that version, the request that produced it with its clarifications, the documents and the **diff against the previous version**; a version can be downloaded as a ZIP. The header shows the project's current version.

Notes written in the chat box also enter the context of later stages (e.g. "the UI should use a dark theme").

## Docker Compose

Every project gets a Dockerfile and a Compose file. If Postgres is chosen as the database a `db` service is added, for Redis `redis`, for OpenTelemetry the Collector, for OpenObserve the Collector and OpenObserve services. The required variables are in `project/.env.example`. Put the values into `project/.env`:

Enter the project folder and use `docker compose config`, `docker compose up --build -d` and, when needed, `docker compose down`.

If OpenObserve is chosen, `OPENOBSERVE_BASIC_AUTH` is the Base64-encoded form of the string `ZO_ROOT_USER_EMAIL:ZO_ROOT_USER_PASSWORD`. The application must send telemetry to `otel-collector:4318`; the Development Agent provides the SDK and instrumentation of the chosen language. Review the Dockerfile, Compose and application code before publishing.

## UI and management API

The browser UI opens with `make run`. If you need the management API use `VENV_PY -m uvicorn autosdlc.api:app --host 127.0.0.1 --port 8000`.

The Streamlit UI collects the idea and architecture choices; it shows the run history and documents. The management API has `GET /questions`, `POST /runs`, `GET /runs`, `GET /runs/{id}` and `GET /health`. The `POST /runs` request takes the required choices in the `decisions` field and waits until the run finishes. The API documentation is at `http://127.0.0.1:8000/docs`.

## Development verification

```bash
make test
```

AI-generated code and tests run in a local subprocess. This tool does not provide a security sandbox for generated code; review untrusted ideas or prompts before running and deploying them.

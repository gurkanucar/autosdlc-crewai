# AutoSDLC

[English](README.md) · [Türkçe](README.tr.md)

**Turn a software idea into a tested, documented project with a team of AI agents.** AutoSDLC coordinates eight agents through the software development lifecycle with CrewAI Flows, and gives you a live view of their work. Choose the stack for each project: the generator is Python, but the applications it creates can use other languages and frameworks.

![AutoSDLC end to end: template, live agents, a question, review and repair loops, a failed API call and Retry](docs/media/workflow.gif)

*One run from start to finish: pick a template, watch the agents work live, answer an agent's question, see the review send the design back and the tests trigger a repair, then retry after an API outage. The recording is the real interface driven by a scripted demo engine; all project data and conversations here are fictional and no model was called.*

![AutoSDLC project workspace](docs/media/workspace.png)

![Create a project](docs/media/new-project.png)

### Workflow outcomes

| Successful run | Test failure |
| --- | --- |
| ![Successful run: all stages passed with revision and repair loops](docs/media/success.png) | ![Failed run: API health check failed during testing](docs/media/failure.png) |

**Retry in progress:** the failed API check is being rerun from the Testing stage; earlier stages are kept.

![Retry in progress from the Testing stage](docs/media/retry.png)

These status examples use the same fictional project data shown above. The retry state is simulated; screenshots do not represent a real model run.

### Generated documents

Every stage writes a Markdown document that you can read, edit and download. These are cropped views of the example project (English interface, fictional data):

| Requirements (Analysis Agent) | Architecture with Mermaid diagrams (Design Agent) |
| --- | --- |
| ![Requirements document with FR and NFR lists](docs/media/doc-requirements.png) | ![Architecture document with component and sequence diagrams](docs/media/doc-architecture.png) |
| **Architecture review (Review Agent)** | **Traceability matrix (Test Agent)** |
| ![Review report with a severity table and a verdict](docs/media/doc-review.png) | ![Traceability matrix linking every FR and NFR to tests](docs/media/doc-traceability.png) |

**Edit any section, not just the whole file.** Pick a heading or a single requirement, then ask a question, rewrite, add, delete or edit it by hand. The agent's proposal is shown as a diff that you accept or reject; accepting it marks the later stages as out of date.

![Section-level edit: rewrite FR-02 and review the proposed diff](docs/media/doc-section-edit.png)

## What it does

- Plans, analyzes requirements, designs and reviews architecture, writes code, runs tests, prepares deployment, and documents maintenance.
- Shows stage status, agent output, review and repair loops, and questions in a live workspace.
- Lets you answer an agent, review document changes, and continue the workflow with your decisions.
- Exports the project, source code, or SDLC documents as ZIP files.
- Generates API and browser tests when the selected project needs them, and can include Docker Compose services for databases, caches, and observability.

## Get started

Requirements: Python 3.11–3.13, [OpenCode Go](https://opencode.ai/go) access, and `make`. Docker is only needed to run generated projects in containers.

```bash
git clone https://github.com/<owner>/autosdlc.git
cd autosdlc
```

Add your OpenCode Go key to `.env` (the file is ignored by Git):

```dotenv
OPENCODE_API_KEY=your_key_here
```

Start the app. The first run creates a virtual environment, installs dependencies, and opens the browser UI:

```bash
make run
```

Describe your project, choose its language, stack, and infrastructure, then select **Create project**. Without a custom path, the project is saved under `projects/`. Set a project-specific **Document language** in the form if needed; the interface language can be changed separately.

## Example: a decision during design

An agent can stop at an unclear architecture decision and ask you in the project workspace:

> Should inventory changes be stored as an audit history, or is the current quantity enough?

You can answer in the agent chat. The answer is saved with the run and passed to the next stages. The question above is an illustrative example; actual questions depend on your project.

## Development

```bash
make setup  # install the app and Playwright Chromium
make test   # run the test suite
```

The generated project's tools and services are chosen per project. Review AI-generated code before running or deploying it; generated commands execute as local subprocesses.

## Project references

[CrewAI Flows](https://docs.crewai.com/concepts/flows) · [OpenCode Go](https://opencode.ai/go) · [Streamlit](https://streamlit.io/) · [FastAPI](https://fastapi.tiangolo.com/) · [Pytest](https://docs.pytest.org/) · [Playwright](https://playwright.dev/python/)

Workflow tasks and agent roles are defined in [`tasks.yaml`](src/autosdlc/config/tasks.yaml); starter project ideas live in [`templates.yaml`](src/autosdlc/config/templates.yaml).

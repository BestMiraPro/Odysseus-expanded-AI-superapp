<p align="center">
  <img src="assets/branding/odysseus-wordmark.png" alt="Odysseus" width="238">
</p>

<p align="center">
  A self-hosted AI workspace — chat, agents, research, documents, email, notes and calendar —
  <br>
  extended here with a <b>spaced-repetition Study system</b> and a <b>multi-agent Omnigent bridge</b>.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="#bring-your-own-api-key">Bring your own API key</a> ·
  <a href="#the-study-app">Study</a> ·
  <a href="#omnigent">Omnigent</a> ·
  <a href="website/setup.md">Setup Guide</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

<p align="center">
  <img src="assets/branding/odysseus-browser.jpg" alt="Odysseus interface">
</p>

---

## What this fork is

This is a fork of [**odysseus-dev/odysseus**](https://github.com/odysseus-dev/odysseus).
Everything upstream does, this does — see [Upstream features](#upstream-features).

On top of that it adds two things:

| Addition | What it is |
| --- | --- |
| **[Study](#the-study-app)** | A spaced-repetition study system. Feed it your course PDFs and past papers; it extracts the questions, groups them into chapters and themes, and drills you on them with FSRS scheduling, confidence calibration, AI grading of written answers, and hints that cost you. |
| **[Omnigent](#omnigent)** | A multi-agent orchestrator bundled into the image and bridged into the UI, so agent crews (including Claude Code and Codex as sub-agents) run alongside the workspace. |

Upstream is the place to go for the core workspace. Come here for Study and Omnigent.

## Quick Start

Requires [Docker](https://docs.docker.com/get-docker/) with Compose, and nothing else:
Python and Node live in the image, and the models are whatever you point it at.

```bash
git clone https://github.com/BestMiraPro/Odysseus-expanded-AI-superapp.git
cd Odysseus-expanded-AI-superapp
cp .env.example .env
docker compose up -d --build
```

The first build takes a while (it bakes in Omnigent, the Claude Code and Codex CLIs,
and the image-model wheels). When it finishes:

1. Open **http://localhost:7000**.
2. Get your generated admin password:

   ```bash
   docker compose logs odysseus | grep -A1 "Initial admin user"
   ```

   It logs `Temporary password: …` once, on first boot only. Log in as `admin` and
   change it under Settings → Account.
3. Give it a model — see below. Nothing AI-powered works until you do.

> The default branch is `dev` and gets changes first. `main` is the same content,
> promoted only after a full green test run.

## Bring your own API key

Odysseus talks to anything that speaks the **OpenAI-compatible chat-completions API**, so
most providers work, and so does anything you run locally. Anthropic's Messages API and
Ollama's native API are detected and handled directly, so those work too. Keys are entered
**in the UI, not in `.env`** — they are stored per endpoint, and one key can back several
models.

**Settings → Services → Add API Models (Endpoint)**

1. Give the endpoint a name (e.g. `OpenAI`, `OpenRouter`, `Together`, `W&B`).
2. Paste the base URL. For OpenAI-compatible providers this is the part ending in
   `/v1`; Anthropic and Ollama are recognised from their host and need no suffix.

   | Provider | Base URL |
   | --- | --- |
   | OpenAI | `https://api.openai.com/v1` |
   | OpenRouter | `https://openrouter.ai/api/v1` |
   | Together | `https://api.together.xyz/v1` |
   | Groq | `https://api.groq.com/openai/v1` |
   | Anthropic | `https://api.anthropic.com` |
   | Local (Ollama) | `http://host.docker.internal:11434/v1` |
   | Local (LM Studio / vLLM / llama.cpp) | `http://host.docker.internal:1234/v1` |

3. Paste your API key and press **Test**. It fetches the model list, which is how you
   know the key and URL are both right.
4. Save, then pick which model does what in **Settings → Services**:

   | Setting | Used for |
   | --- | --- |
   | **Default Chat Model** | new chat sessions |
   | **Utility Model** | background work — summaries, auto-naming, memory retrieval. A small/cheap or local model is the point here. |
   | **Study Model** | question extraction, chapter/theme grouping, answer grading, hints |
   | **Vision** | anything involving images, including scanned PDFs in Study |

   Leave any of them unset and it falls back: Study → Utility → Default Chat. So one
   configured endpoint is enough to try everything.

Running fully local? Point the endpoint at Ollama or LM Studio instead of a hosted
provider and skip the key — the rest of the app does not care which it is.

## The Study app

**Tools → Study** in the left sidebar. (It has no rail shortcut; it lives in the Tools list.)

The loop it is built around:

1. **Subjects** — make a subject, then attach material: lecture notes, textbook chapters,
   past papers. PDFs, text and images all work; scanned PDFs go through vision OCR.
2. **Extract questions** — the model reads the material and pulls out the actual
   questions, with their answers, options, difficulty and source page. Extraction also
   groups the bank into chapters and themes as it goes, so it is ready to practise.
3. **Practice** — pick everything, one chapter, or one theme. You state your confidence
   *before* checking, which is what makes the "sure but wrong" count at the end useful.
   Written answers are graded by the model against the source; hints are available but
   reschedule a success sooner.
4. **Review, Plan, Stats** — FSRS scheduling decides what is due. Add an exam date and it
   builds a spaced, interleaved plan, including timed mocks that ask you to predict your
   score before marking.

There is also a **Tutor** tab — an agent grounded in your own materials, so it answers
from your notes rather than from the internet.

Which model Study uses is shown in the panel header (`Model: …`); click it to change it,
or set it in Settings → Services → Study Model.

## Omnigent

**Omnigent** in the left rail, or **Tools → Omnigent**. Its own web UI is bridged to
**http://localhost:6868**.

It runs agent crews with a mix of workers:

- **API models** — anything you configured above. No extra login.
- **Claude Code** and **Codex** — both CLIs are baked into the image so they can act as
  native sub-agents. Each needs a one-time interactive subscription login inside the
  container; the API-model workers need none.

Pin the version with `OMNIGENT_VERSION` in the Dockerfile, and move the bridged port with
`OMNIGENT_UI_PORT` in `.env` if 6868 is taken.

## Upstream features

Everything from [odysseus-dev/odysseus](https://github.com/odysseus-dev/odysseus):

- **Chat + Agents** — local/API models, tools, MCP, files, shell, skills, and memory.
- **Cookbook** — hardware-aware model recommendations, downloads, and serving.
- **Deep Research** — multi-step web research with source reading and report generation.
- **Compare** — blind side-by-side model testing and synthesis.
- **Documents** — writing-first editor with AI edits, suggestions, Markdown, HTML and CSV.
- **Email** — IMAP/SMTP inbox with triage, tags, summaries, reminders, and reply drafts.
- **Notes, Tasks + Calendar** — reminders, todos, scheduled agent tasks, and CalDAV sync.
- **Extras** — gallery/image editor, themes, uploads, web search, presets, sessions, 2FA.

Upstream's hover-to-play tour is on the
[Odysseus landing page](https://odysseus-dev.github.io/odysseus/).

## Going further

- **[Setup guide](website/setup.md)** — native installs, GPU passthrough, Windows and
  macOS, HTTPS, reverse proxies, and the full configuration reference.
- **[DEPLOY.md](DEPLOY.md)** — the `deploy.ps1` build-from-a-clean-branch workflow.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** and **[ROADMAP.md](ROADMAP.md)**.

## Security

Self-hosted, with real tools attached — treat it like something that can act on your behalf.

- Keep `AUTH_ENABLED=true` for any network-accessible deployment.
- Keep `LOCALHOST_BYPASS=false` outside local development.
- Keep private data out of Git. `.env`, `data/` and credential files are gitignored;
  a CI job scans the whole history for anything that slips through.
- Do not publish raw model/service ports (7000, 6868, 8100, …) straight to the internet.

See [SECURITY.md](SECURITY.md) and [THREAT_MODEL.md](THREAT_MODEL.md); deployment
specifics are in the [setup guide](website/setup.md#security-notes).

## License

AGPL-3.0-or-later, inherited from upstream — see [LICENSE](LICENSE) and
[ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md). Upstream Odysseus is the work of
[odysseus-dev](https://github.com/odysseus-dev/odysseus) and its contributors.

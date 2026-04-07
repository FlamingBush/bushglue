# Monorepo Conversion Options for Bush Glue

## Context

The FlamingBush org has 9 repos. The bush pipeline currently spans multiple repos that are deployed independently to the ODROID. The goal is to consolidate pipeline components into a single monorepo for easier development and deployment.

### Current repos in the org

| Repo | Language | Purpose | Pipeline component? |
|------|----------|---------|-------------------|
| **bushglue** | Python | MQTT glue: STT, TTS, T2V bridge, sentiment, discord bot, fire control, utils | Yes (orchestration) |
| **t2v** | Rust + Python (uv) | text-to-verse: Rust CLI/server + HTTP API, ChromaDB queries, Ollama embeddings, affect templates, Python preprocessing (own uv workspace with 3 members) | Yes (core) |
| **t2v_chroma** | Data | ChromaDB embedding database (chroma.tar.gz, ~28MB) | Yes (data) |
| **speech-to-text** | Python | Standalone Vosk STT (mic + file modes) | Superseded — bushglue has its own `stt-service.py` |
| **bbsentimentqq** | Python (uv) | Standalone sentiment HTTP server (torch + transformers) | Superseded — bushglue has `sentiment-service.py` |
| **AIAm-code** | Python | `semantic_jukebox` — older prototype (bible_chat.py, app.py) | Legacy/archive |
| **AIAm-assets** | — | Pictures and videos | Not code |
| **Discussions** | — | Org-level discussions | Not code |
| **machine-elves.art** | Astro | Website (private) | No |

### What actually needs to merge

Only **3 repos** are active pipeline components:
1. **bushglue** — all the Python services, utils, systemd files, relay firmware
2. **t2v** — the Rust text-to-verse server (Cargo project + Python preprocessing + affect templates)
3. **t2v_chroma** — the embedding database archive

The others are either superseded (speech-to-text, bbsentimentqq — their functionality is already in bushglue), legacy (AIAm-code), or non-code (assets, discussions, website).

---

## Option A: Organized Directories, No New Tooling

Merge t2v and t2v_chroma into the bushglue repo. Reorganize into directories. Keep using system Python and `cargo build` separately. No workspace tooling.

```
bushglue/
├── services/
│   ├── stt/
│   │   └── stt_service.py
│   ├── tts/
│   │   └── tts_service.py
│   ├── t2v-bridge/
│   │   └── t2v_service.py           # Python MQTT bridge (was t2v-service.py)
│   ├── sentiment/
│   │   └── sentiment_service.py
│   ├── sound/
│   │   └── sound_service.py
│   ├── audio-agent/
│   │   └── audio_agent.py
│   └── discord/
│       └── discord_bot.py
├── t2v/                              # Rust text-to-verse (merged from t2v repo)
│   ├── Cargo.toml
│   ├── Cargo.lock
│   ├── src/                          # Rust source
│   ├── templates/                    # affect templates
│   ├── preprocessing-biblical/       # Python preprocessing scripts
│   ├── preprocessing-common/
│   ├── preprocessing-generic/
│   └── scripts/
├── data/
│   └── chroma.tar.gz                # embedding DB (merged from t2v_chroma)
├── lib/
│   └── bushutil.py
├── firmware/
│   └── relay-control/...
├── systemd/odroid/...
├── utils/...
├── docs/
│   ├── README.md, PROJECT.md
│   └── mqtt-architecture.*
├── requirements.txt                  # base Python deps
└── LICENSE
```

**Shared code:** `bushutil.py` in `lib/`, exposed via `PYTHONPATH` in systemd units.

**Rust component:** `cd t2v && cargo build --release` — built separately, binary path updated in `t2v-bridge/t2v_service.py`.

**t2v Python preprocessing:** t2v already has its own uv workspace (`preprocessing-biblical`, `preprocessing-common`, `preprocessing-generic`). These stay self-contained under `t2v/` — they're build-time/offline tools, not runtime pipeline components.

**Deployment:** `git pull` → `cargo build --release` (if Rust changed) → restart services. Systemd paths updated once.

**Migration effort:** Low (~2-3 hours). `git mv` bushglue files + copy in t2v and t2v_chroma content + update paths.

| Pros | Cons |
|------|------|
| Everything in one place, easy to browse | `PYTHONPATH` hack for shared Python code |
| No new tooling needed | No lockfile or dependency isolation for Python |
| Rust and Python stay independent | Cargo and pip managed separately |
| Simple deploy: git pull + optional cargo build | chroma.tar.gz bloats the repo (~28MB) |

---

## Option B: uv Workspaces + Cargo (Modern, Structured)

Use `uv` workspaces for all Python components and keep Cargo for Rust. Each Python service gets its own `pyproject.toml`. `bushutil` becomes a proper internal package. The Rust t2v project keeps its own `Cargo.toml`.

```
bushglue/
├── pyproject.toml                    # uv workspace root
├── uv.lock                           # Python lockfile
├── packages/
│   └── bushutil/
│       ├── pyproject.toml
│       └── src/bushutil/__init__.py
├── services/
│   ├── stt/
│   │   ├── pyproject.toml            # deps: paho-mqtt, vosk, numpy, sounddevice, bushutil
│   │   └── src/bush_stt/__init__.py
│   ├── tts/
│   │   ├── pyproject.toml
│   │   └── src/bush_tts/__init__.py
│   ├── t2v-bridge/
│   │   ├── pyproject.toml            # deps: paho-mqtt, bushutil
│   │   └── src/bush_t2v/__init__.py
│   ├── sentiment/
│   │   ├── pyproject.toml            # deps: paho-mqtt, torch, transformers, bushutil
│   │   └── src/bush_sentiment/__init__.py
│   ├── sound/
│   │   ├── pyproject.toml
│   │   └── src/bush_sound/__init__.py
│   ├── audio-agent/
│   │   ├── pyproject.toml
│   │   └── src/bush_audio_agent/__init__.py
│   └── discord/
│       ├── pyproject.toml
│       └── src/bush_discord/__init__.py
├── t2v/                              # Rust project (own Cargo.toml)
│   ├── Cargo.toml
│   ├── Cargo.lock
│   ├── src/
│   ├── templates/
│   ├── preprocessing-*/              # Python preprocessing (could use uv too)
│   └── scripts/
├── data/
│   └── chroma.tar.gz
├── firmware/
│   └── relay-control/...
├── systemd/odroid/...
├── utils/...
└── docs/...
```

**Shared code:** `bushutil` is a proper uv workspace package — `import bushutil` just works.

**Deployment:** `git pull && uv sync && systemctl restart bush-*`. Cargo build only when Rust source changes.

**Migration effort:** Medium (~half day). Create pyproject.toml files, wrap scripts in packages, merge repos.

| Pros | Cons |
|------|------|
| Proper per-service Python dependency declarations | Requires `uv` on ODROID (single binary install) |
| Single lockfile for reproducible Python builds | More boilerplate (pyproject.toml per service) |
| No path hacks for shared code | `src/` layout adds indirection vs. flat scripts |
| Entry points give clean CLI commands | More ceremony for quick edits |
| Foundation for CI | Two package ecosystems (uv + cargo) to manage |
| t2v preprocessing could join the top-level uv workspace | Need to decide: nest t2v's uv workspace or merge into root |

---

## Option C: Organized Flat + Per-Service Requirements (Middle Ground)

Merge everything into one repo. Keep services as plain runnable scripts. Split Python requirements per service. Use a Makefile for convenience.

```
bushglue/
├── services/
│   ├── stt/
│   │   ├── main.py
│   │   └── requirements.txt
│   ├── tts/
│   │   ├── main.py
│   │   └── requirements.txt
│   ├── t2v-bridge/
│   │   ├── main.py
│   │   └── requirements.txt
│   ├── sentiment/
│   │   ├── main.py
│   │   └── requirements.txt         # paho-mqtt, torch, transformers
│   ├── sound/
│   │   ├── main.py
│   │   └── requirements.txt
│   ├── audio-agent/
│   │   ├── main.py
│   │   └── requirements.txt
│   └── discord/
│       ├── main.py
│       └── requirements.txt
├── t2v/                              # Rust project (merged from t2v repo)
│   ├── Cargo.toml, Cargo.lock
│   ├── src/
│   ├── templates/
│   └── preprocessing-*/
├── data/
│   └── chroma.tar.gz
├── lib/
│   └── bushutil.py
├── firmware/relay-control/...
├── systemd/odroid/...
├── utils/...
├── requirements-base.txt
├── Makefile                          # venv + cargo build targets
├── docs/...
└── LICENSE
```

**Shared code:** `bushutil.py` in `lib/`, exposed via `PYTHONPATH`.

**Deployment:** `git pull` + restart (+ `make build-t2v` if Rust changed).

**Migration effort:** Low-medium (~3-4 hours).

| Pros | Cons |
|------|------|
| Everything in one repo, services stay as plain scripts | `PYTHONPATH` for shared code |
| Per-service requirements.txt documents deps | No lockfile |
| No new tools needed on ODROID | Per-service venvs are manual |
| Makefile ties cargo + pip together | Two build systems loosely coordinated |

---

## Recommendation

**Option C** is the best fit. Rationale:
- This is an art installation on an ODROID, not a cloud service — keep it simple
- Services stay as editable scripts with fast restart cycles
- Per-service `requirements.txt` documents what each service needs (the main organizational win)
- The Rust t2v project keeps its own Cargo.toml without additional tooling overhead
- Upgrading to Option B later is straightforward if needs grow
- The `speech-to-text` and `bbsentimentqq` repos don't need merging — their functionality already exists in bushglue

## What to archive

After merging, these repos become redundant:
- **speech-to-text** — superseded by `services/stt/` in bushglue
- **bbsentimentqq** — superseded by `services/sentiment/` in bushglue
- **AIAm-code** — legacy prototype, archive as-is

## Chroma data: Git LFS

`t2v_chroma` contains a ~28MB `chroma.tar.gz`. This will be committed under `data/` and tracked with **Git LFS** to keep clones fast and git history clean.

---

## Implementation Steps (for Option C)

1. Create branch and directory structure
2. `git mv` bushglue services into `services/<name>/main.py`
3. Move `bushutil.py` to `lib/bushutil.py`
4. Move `relay-control/` to `firmware/relay-control/`
5. Move docs to `docs/`
6. Clone and merge `t2v` repo content into `t2v/` directory
7. Set up Git LFS (`git lfs track "*.tar.gz"`) and add `data/chroma.tar.gz` from t2v_chroma
8. Split `requirements.txt` into per-service files + `requirements-base.txt`
9. Update all systemd unit files with new paths + `PYTHONPATH`
10. Update bash utils that reference bushutil or service paths
11. Update deploy skill paths
12. Create Makefile with targets: `venv-<service>`, `build-t2v`, `deploy`
13. Remove legacy files (`stt_t2v.py`, `mqttexample.py`)
14. Update `.gitignore` for `.venv/`, `target/` directories

## Critical Files
- `bushutil.py` — shared module, relocate to `lib/`
- `requirements.txt` — split per-service
- `systemd/odroid/*.service` — all paths must be updated
- `utils/bush-monitor`, `utils/bush-integration-test` — bash scripts importing bushutil
- `.claude/skills/deploy/SKILL.md` — deploy paths must be updated
- `t2v-service.py` — T2V_BIN path must point to `t2v/target/release/text-to-verse`

## Verification
- Each Python service starts without import errors
- `cargo build --release` succeeds in `t2v/`
- t2v-bridge can find and start the Rust binary
- Integration test passes on ODROID after deploy
- `bushutil` imports work from all services and utils

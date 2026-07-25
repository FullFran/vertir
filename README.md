# VertIR

Editor de vídeo **AI-first** para contenido **vertical (9:16)**. El LLM (Claude Code / OpenCode) **planifica** — emite y muta un **Timeline IR declarativo**; un **validador fail-closed** lo revisa; un **renderer determinista (FFmpeg)** lo ejecuta. El humano remata en una **web-tweaker** (navegador, incl. móvil).

- Arquitectura y decisiones: [`docs/informe-editores-ai-video-y-arquitectura.md`](docs/informe-editores-ai-video-y-arquitectura.md)
- Contrato (el IR): [`docs/timeline-ir-v1.md`](docs/timeline-ir-v1.md)

## Estado

**Rebanada 1 (core), funcional.** Pipeline: `ingest → transcript → cortar filler/silencios → auto-reframe 9:16 → subtítulos word-highlight → validar → render proxy + MP4`. Sin dependencias externas: **solo stdlib de Python + ffmpeg**.

Rebanadas siguientes: b-roll + logo (2), placas intro/outro + ducking + loudness (3), export a CapCut (4). El IR ya está diseñado para todas → sin re-arquitectura.

## Requisitos

- Python ≥ 3.11
- `ffmpeg` y `ffprobe` en el PATH

## Uso rápido

```bash
# Demo end-to-end (genera fuente + transcript sintéticos y renderiza un short real)
python -m vertir demo --out ./vertir-out

# Desde tu material:
python -m vertir build --hero hero.mp4 --transcript words.json --out ./out [--bgm music.m4a]

# Retoque humano en el navegador (también desde el celular en la misma red):
python -m vertir web --ir ./out/timeline.ir.json --dir ./out
```

Formato del transcript (`words.json`), tiempos en microsegundos de la fuente:

```json
{ "assetId": "hero",
  "words": [ {"sourceAtUs": 0, "sourceEndUs": 500000, "text": "hola"} ] }
```

## Como MCP para Claude Code

`.mcp.json`:

```json
{ "mcpServers": { "vertir": { "command": "python3", "args": ["-m", "vertir", "mcp"] } } }
```

Tools expuestas: `ingest`, `build_short`, `validate`, `render`, `add_broll`, `add_logo`,
`add_title`, `demo`, y el bucle editorial: `propose_plan` → `validate_plan` → `apply_plan`.

## El plan editorial

El IR dice **qué es** el vídeo. El plan dice **por qué**: cuál es el hook, dónde
aprieta el ritmo, qué momentos llevan un golpe visual, qué palabras cargan el
argumento.

```
transcript ──→ [ LLM ] ──→ plan.json ──→ apply_plan() ──→ IR ──→ render
                          (criterio)     (determinista)
```

El modelo **nunca** emite un IR: emite un plan, y un aplicador determinista lo
convierte en mutaciones del IR. El plan se valida fail-closed antes de tocar nada,
y se guarda junto al render (`plan.json`) porque es el único registro de *por qué*
se cortó así.

```bash
python -m vertir build --hero h.mp4 --transcript t.json --out ./out --plan plan.json
python -m vertir build --hero h.mp4 --transcript t.json --out ./out --baseline-plan
```

Todos los tiempos del plan son **microsegundos de la fuente**, así que un plan es
invariante frente a recortes — igual que los anclajes de b-roll y los subtítulos.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Estructura

```
vertir/
  ir.py          # el contrato (builders + io)          validate.py  # validador fail-closed
  probe.py       # ingest (ffprobe) + sha256            render.py    # FFmpeg + ASS word-highlight
  transcript.py  # transcript + loaders (whisper.cpp)   pipeline.py  # ensamblado core
  edit.py        # cortes, cut-map (source→program)     plan.py      # plan editorial (juicio del LLM)
  cli.py / mcp_server.py / web/
```

## Roadmap

- [x] **Rebanada 1 — core**: filler-cut + reframe 9:16 + captions word-highlight + validador + render + MCP + web-tweaker
- [x] **Rebanada 2** — b-roll (cortes source-anchored) + logo/marca de agua (overlay program-anchored)
- [x] **Rebanada 3** — placas intro/outro (hook cards) + ducking de música (side-chain) + perfil de loudness por plataforma
- [x] **Rebanada 5 — planner editorial**: el LLM decide el corte (hook, drops, beats de punch-in, énfasis, placas) en vez de sólo ejecutarlo
- [ ] **Rebanada 4** — export a draft de CapCut (feature secundaria, desktop)
- [ ] **Rebanada 6** — reconciliar ediciones hechas en CapCut de vuelta al IR
- [ ] **Rebanada 7** — motion graphics vía fábrica de assets (backend Remotion)

## Contribuir

Issues y PRs bienvenidos. El **IR es el contrato** (`docs/timeline-ir-v1.md`): cambios de schema van versionados, nunca rompiendo documentos existentes. Corré los tests antes de un PR: `python -m unittest discover -s tests`.

## Licencia

[MIT](LICENSE) © 2026 FullFran


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
- *(opcional, sólo para el export a CapCut)* [`capcut-cli`](https://github.com/renezander030/capcut-cli) — necesita Node ≥ 18. Sin él, el resto del pipeline funciona igual.

## Export a CapCut

El IR es el contrato; esto es un **adaptador**, no un segundo renderer. FFmpeg
sigue siendo el único compositor de píxeles finales y el modelo nunca toca un draft.

```bash
python -m vertir capcut ./out/timeline.ir.json --out ./out          # escribe el draft
python -m vertir capcut ./out/timeline.ir.json --out ./out --check  # sólo valida
```

Fail-closed por los dos lados: primero el validador del IR, después
`capcut compile --check`, y sólo entonces se escribe.

El mapeo **es lossy y lo decimos**. Un export que descarta en silencio la
normalización de loudness, el ducking side-chain o el resaltado por palabra es
peor que no tener export: el draft parece terminado y no lo está. Cada construcción
que no cruza sale en el informe de pérdidas con su severidad
(`dropped` / `degraded` / `unverified`).

## Reconcile: las ediciones humanas vuelven al IR

```bash
python -m vertir reconcile ./out/timeline.ir.json \
    --draft ./out/capcut-draft --provenance ./out/capcut.provenance.json
```

**Parchea el IR; no lo reconstruye.** Y esa es toda la diferencia.

El IR lleva **intención**; un draft sólo lleva **resultado**:

| IR (intención) | draft de CapCut (resultado) | se perdería |
| --- | --- | --- |
| `reframe {mode, focusX, focusY}` | un rect de crop | "seguí la cara" |
| b-roll anclado a la fuente | un timestamp de programa | el anclaje |
| `duck {enabled, targetDb}` | keyframes de volumen | la regla |

Por eso guardamos un snapshot del draft tal como se exportó más un mapa de
procedencia, le pedimos a `capcut diff` **qué cambió el humano**, y aplicamos
sólo ese delta encima del IR que ya teníamos.

Lo que no sabemos mapear (filtros, efectos, elementos creados en CapCut) sale
como `pinnedInCapCut`: se queda en el draft y se dice en voz alta que el IR no
lo gestiona. Nunca se descarta en silencio ni se finge entenderlo.

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
  anim.py        # keyframes: sample() + expr()         capcut/      # adaptador a CapCut
  cli.py / mcp_server.py / web/
```

## Roadmap

- [x] **Rebanada 1 — core**: filler-cut + reframe 9:16 + captions word-highlight + validador + render + MCP + web-tweaker
- [x] **Rebanada 2** — b-roll (cortes source-anchored) + logo/marca de agua (overlay program-anchored)
- [x] **Rebanada 3** — placas intro/outro (hook cards) + ducking de música (side-chain) + perfil de loudness por plataforma
- [x] **Rebanada 3.5 — keyframes**: `transform` animado (`scale`/`x`/`y`) y `gainDb`, con expresión cerrada de FFmpeg. El zoom va sobre `scale:eval=frame` + `crop` (medido: `zoompan` redondea x/y a enteros y tiembla ~2 px en un punch-in sutil). Reglas de validación §5/§6; lo que el motor aún no ejecuta sale como warning `kf-unrendered` en vez de descartarse en silencio.
- [x] **Rebanada 5 — planner editorial**: el LLM decide el corte (hook, drops, beats de punch-in, énfasis, placas) en vez de sólo ejecutarlo
- [x] **Rebanada 4 — export a CapCut**: el IR se transpila a un draft de CapCut/JianYing vía `capcut-cli`, con informe de pérdidas explícito
- [x] **Rebanada 6 — reconcile**: las ediciones hechas a mano en CapCut vuelven al IR **parcheando**, nunca re-parseando
- [ ] **Rebanada 7** — motion graphics vía fábrica de assets (backend Remotion)

## Contribuir

Issues y PRs bienvenidos. El **IR es el contrato** (`docs/timeline-ir-v1.md`): cambios de schema van versionados, nunca rompiendo documentos existentes. Corré los tests antes de un PR: `python -m unittest discover -s tests`.

## Licencia

[MIT](LICENSE) © 2026 FullFran


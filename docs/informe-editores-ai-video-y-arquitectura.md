# Editores de vídeo AI-first (open source) — Estado del arte y propuesta de arquitectura

> **Objetivo del proyecto:** un editor de vídeo **AI-first** para **contenido vertical (9:16)**, manejado desde un agente (**Claude Code / OpenCode / Claude Cowork / GPT Work**) en lugar de una GUI tradicional, cuyo **entregable principal sea un draft de CapCut** que un humano abre y termina a mano.
>
> **Decisiones de producto (2026-07-23, en orden):**
> 1. Host del agente = **Claude Code** (integración vía MCP server + Skill).
> 2. IR = **propio, modelado sobre el `draft_content.json` de CapCut** (NO OTIO como core; OTIO solo export secundario). Ver §6.
> 3. **Restricción dura descubierta:** el usuario está en **Linux** (sin CapCut Desktop) y su audiencia es **mobile-first**; y CapCut es una **plataforma cerrada sin puerta programática** a cuentas/mobile (ver §9 y §13). → No se puede "PC genera → cuenta de CapCut → celular editable" de forma limpia/automática.
> 4. Por eso: el **retoque humano se hace en una superficie web propia y liviana sobre el IR** (anda en cualquier navegador de celular → mobile-first; dueños del loop). **No es un CapCut desde cero** — es un *tweaker* sobre lo que la IA ya hizo. El **export a draft de CapCut queda como feature secundaria** para power users de desktop.
>
> **Fecha del informe:** 2026-07-23. Basado en un research multiagente (15 agentes, ~730k tokens) + una re-investigación dedicada de OTIO.

---

## 0. TL;DR (léelo si no lees nada más)

1. **Tu idea es un hueco real de mercado.** Ningún editor AI-first exporta un draft de CapCut como entregable de primera clase. El ecosistema de generación de drafts de CapCut y los editores agénticos existen por separado y **nadie los conectó**. No estás copiando; estás llenando un vacío.

2. **La arquitectura robusta ya tiene nombre y consenso: "Model B" (D-over-B).** El LLM **nunca** renderiza: emite un **Timeline IR declarativo** (JSON), un **validador fail-closed** lo revisa, y un **motor determinista** lo ejecuta. Todo expuesto por **MCP** para que Claude Code / OpenCode lo manejen. La academia (LAVE, Aurora, VideoAgent, Prompt-Driven Agentic Editing) converge exactamente ahí.

3. **El IR NO debe ser OTIO.** OTIO modela bien la *estructura* de corte, pero no modela lo que define al vídeo social: keyframes, estilos de subtítulos, transforms por clip, máscaras, efectos. Y **no existe adapter de CapCut para OTIO**. → **IR propio, modelado como un superconjunto casi isomorfo del `draft_content.json` de CapCut**, para que el export sea casi mecánico. De OTIO robamos *patrones de diseño* (tiempo racional, versionado de esquema), no el formato.

4. **El camino a CapCut existe hoy** (pyCapCut / CapCutAPI-VectCutAPI / **jianying-mcp**) **pero es tu mayor riesgo**: formato de ingeniería inversa, "generate, don't round-trip", encriptación en JianYing 6.0+, frágil a updates de la app. **Fijá la versión de CapCut** y aislá esta capa.

5. **Arrancá validando, no construyendo.** Hay un MCP (**jianying-mcp**) que ya genera drafts editables de CapCut con keyframes, texto, transiciones y máscaras. Usalo desde Claude Code en una **Fase 0** para confirmar que el loop "agente → draft → CapCut → humano" funciona en tu versión antes de invertir en el sistema propio.

---

## 1. Estado del arte: los proyectos

### 1.1 Tabla comparativa

| Proyecto | Licencia | Lenguaje | Arquitectura | Estado | ¿AI-first real? | Timeline / IR | MCP | Editor visual | Render | Comunidad (blanda) |
|---|---|---|---|---|---|---|---|---|---|---|
| **Kinocut** | Apache-2.0 | Python (+Node opt) | **B** (plan→IR→render det.) vía D | v1.8–1.9, ~4 meses, muy activo | **Sí** (MCP es primario) | JSON flat propio (EDL-adjacent, sin tracks) | Sí (~151 tools, en registry) | No (headless) | FFmpeg (+Whisper/Hyperframes) | ~88★, 1 maintainer dominante |
| **video-use** (browser-use) | MIT | Python | **B/A híbrido** (transcript→EDL→FFmpeg + self-eval) | Temprano, sin releases | **Sí** (skill de coding-agent es la única UI) | EDL propio desde transcript de palabras | No | No (solo PNG `timeline_view`) | FFmpeg (+Remotion/Manim/PIL) | ~17.6k★ (hype de marca), 6 contribs |
| **dawn-cut** | MIT | TypeScript (~93%) | **B** (LLM con gramática→EditCommand→EDL) | v0.1.1, temprano | **No** (docs: "la GUI es primaria; IA opt-in") | `.dawn` JSON + EDL validado (OTIO en roadmap) | Sí (experimental) | Sí (Electron desktop) | FFmpeg (sidecar) | ~1★, dev único |
| **OpenCut** | MIT | TS (~97%, Rust creciendo) | **C** substrate (agente no shippeado) | v0.3.0, reescritura en curso | **No** (IA es roadmap) | Store Zustand propio, command-pattern | Roadmap | **Sí** (NLE multi-track en browser) | Canvas + Rust/wgpu WASM + FFmpeg.wasm | ~77–78k★ |
| **StoryToolkitAI** | GPL-3.0 | Python | **B** (transcript→EDL/FCP-XML; humano/NLE ejecuta) | v0.25.1, activo/experimental | **No** (asistente GUI-first) | Transcript JSON + Fountain → EDL/FCP-XML | No | Sí (CustomTkinter; corte delegado a Resolve) | Ninguno propio (Resolve renderiza) | ~995★, equipo chico |
| **FireRed-OpenStoryline** | Apache-2.0 | Python (~71%) | **B** vía D (agente→IR Pydantic→render det.) | Temprano, sin tags | **Sí** (agente es primario) | Pydantic `TimelineTracks` (ms), multi-track | Sí (first-class + Claude Code Skills) | No (chat web/CLI) | MoviePy 2.2 + FFmpeg | ~3.1k★, 366 forks, 7 contribs |
| **OpenChatCut** *(descubierto)* | ? (due diligence pendiente) | TS/React19/Electron | **C+B** (agente escribe tracks; JSON inmutable) | v0.1.3 | **Sí** (conversacional primario) | Timeline JSON inmutable | Sí (streamable HTTP) | **Sí** (timeline multi-track) | Remotion + FFmpeg | ~424★, muy activo |
| **OpenMontage** *(descubierto)* | AGPL-3.0 | Python 3.10+ | **A/orquestador** (el coding-assistant ES el orquestador) | Trending | **Sí** (agent-native) | Composición Remotion/HyperFrames | n/a (por skill-files) | No ("production studio") | Remotion + HyperFrames + FFmpeg | ~41k–67k★ (dato dudoso) |
| **OpenCut-AI** *(descubierto, fork)* | ? (fork de OpenCut) | Next.js + FastAPI | **C+B** (NL→plan 19 acciones→ejecuta) | Sin tags, activo | Parcial (Co-Pilot sobre core manual) | Hereda store de OpenCut | ? | Sí (NLE de OpenCut) | FFmpeg | ~158★ |
| **X-CUT** | — | — | — | **NO ENCONTRADO** | — | — | — | — | — | Probablemente OpenChatCut/ChatCut mal nombrado |

> ⚠️ **Los números son blandos.** El fetch de GitHub se contradice a sí mismo (OpenMontage aparece como ~41k y ~67k★). Tratar todo como orden de magnitud, no exacto. Ver §14.

### 1.2 Lo que hay que robarle a cada uno

- **Kinocut** → **el molde Model B**: spec JSON declarativo → `validate` → `dry-run plan` con hash → render determinista → **"Video Receipt"** con SHA-256 de input/output + cursor de reanudación. Sin flags de force/bypass. Guardrails de *preflight* como capa de primera clase. Triple superficie (MCP + CLI + cliente Python) sobre un mismo motor.
- **video-use** → **"el LLM lee, no mira"**: un transcript de palabras compacto (~12KB) como superficie de razonamiento, y PNGs (`timeline_view`: filmstrip + waveform + labels) **solo on-demand** para decisiones visuales ambiguas. Token-efficient. **Loop de auto-evaluación** acotado (re-inspecciona el render en cada corte, arregla y re-renderiza, máx. 3 veces). *Cuidado:* depende de la API paga de ElevenLabs → lo arreglamos con **Whisper.cpp local**.
- **FireRed-OpenStoryline** → **IR tipado multi-track** (video / subtítulos / voz / música), tiempos en ms, validado con Pydantic. **"Style Skills"**: un workflow completo de edición archivado como Markdown legible y **replayable** sobre material nuevo. MCP + Claude Code Skills de fábrica.
- **dawn-cut** → **disciplina de determinismo**: tiempo en **microsegundos enteros**, intervalos **semiabiertos `[inicio, fin)`**, comandos como **funciones puras** que devuelven `{before, after}`, audit log, IR validado por **contigüidad + duración exacta**, y **diffs de dry-run** antes de aplicar. (Recordá: NO es AI-first — la GUI es primaria.)
- **OpenChatCut** → lo más cercano a tu sueño: chat + timeline multi-track + MCP (streamable HTTP) + Remotion, con **JSON de proyecto inmutable** y sesiones de edición basadas en propuestas con aprobación manual/auto. **Estudialo de cerca.**
- **OpenCut** → confirma la decisión: es el "CapCut open source" con ~78k★ y **sigue lejos de CapCut**, y encima **no es AI-first**. Que un proyecto tan popular no alcance a CapCut te dice el tamaño de esa guerra. **No la pelees en el v1.**

---

## 2. Taxonomía de arquitecturas (las 4 arquitecturas del brief)

El eje central es **la frontera plan/ejecución**: qué tan lejos está la libertad del modelo del render, y cuánto determinismo/auditabilidad hay en el medio.

### Patrón A — Chat → FFmpeg (Model A, ejecución directa)
El LLM emite/orquesta FFmpeg turno a turno. No hay timeline persistente inspeccionable.
- **Pro:** mínimas piezas, headless trivial, máximo alcance expresivo, prototipo rápido.
- **Contra:** determinismo más débil ("ruleta de comandos de shell"); el mismo prompt da comandos distintos; x264/x265 multihilo no es bit-exact; `libass`/fontconfig meten no-determinismo oculto. Sin superficie de auditoría. Sin interoperabilidad. Tags de filtergraph/ASS arcanos y propensos a error para un LLM.
- **Proyectos:** `mcp-video-editor` (chandler767) y el impulso de muchos FFmpeg-MCP chicos. **Cada vez más raro** en proyectos serios porque todos sintieron su fragilidad.

### Patrón B — Chat → Timeline JSON (IR) → Renderer determinista (Model B) ⭐
El LLM emite/muta un **IR declarativo** (JSON validado). Un motor fijo lo renderiza igual siempre. El modelo nunca toca el renderer.
- **Pro:** mejor determinismo/reproducibilidad (render = función pura de `IR + entorno pineado`); **auditable** (dry-run, diff, aprobar antes de escribir bytes; reanudar desde el IR); **interoperable** (el IR se exporta a EDL, FCP-XML, **draft de CapCut**, OTIO); los **validadores** viven naturalmente en la frontera del IR; **es el consenso académico**.
- **Contra:** hay que diseñar y versionar un esquema y mantenerlo estable; "determinista" sigue siendo condicional (pinear encoder/fonts/browser); la expresividad está acotada por lo que el IR modela.
- **Proyectos (el cluster fuerte):** Kinocut, video-use, dawn-cut, FireRed-OpenStoryline, StoryToolkitAI, OpenChatCut.

### Patrón C — Chat → Editor API (el agente llama la API de comandos de un editor vivo)
El LLM maneja un editor convencional invocando su API interna de comandos/undo.
- **Pro:** reutiliza un core de edición maduro (undo/redo, preview/commit, no-destructivo); **control dual** humano+agente sobre un mismo modelo (requisito validado de HCI: LAVE, VideoDiff); preview en tiempo real gratis.
- **Contra:** el determinismo depende del estado interno y la versión del editor; suele estar atado a GUI (no headless); el "IR" es interno y raramente portable; secuenciación imperativa (separación plan/exec más débil salvo que captures la lista de comandos como artefacto auditable).
- **Proyectos:** OpenCut (substrate perfecto, pero el agente es roadmap), OpenCut-AI, OpenChatCut (roza C y B), DaVinci Resolve MCP.

### Patrón D — MCP → Editor (superficie estandarizada por protocolo)
Refinamiento de transporte de B o C: el editor expone un **MCP server** (stdio/HTTP) para que *cualquier* cliente MCP (Claude Code, OpenCode, Cursor) lo maneje.
- **Pro:** interop estándar y agnóstico de cliente; **usable desde Claude Code/OpenCode hoy**; tools tipadas con resultados estructurados (desalienta la "ruleta de shell"); desacopla el motor del host del agente.
- **Contra:** MCP es solo un envoltorio — hereda el determinismo de lo que envuelve (un MCP sobre FFmpeg crudo sigue siendo Patrón A por debajo); ecosistema temprano y fragmentado; los MCP atados a app (Resolve Studio, Blender, CapCut) no son headless.
- **Proyectos:** Kinocut (MCP-first sobre motor Model B = combinación ideal), FireRed, dawn-cut, CapCutAPI/VectCutAPI, DaVinci Resolve MCP, Blender MCP.

### Veredicto
**La arquitectura defendible es `D-over-B`:** exponer una **superficie MCP (D)** cuyo backend sea un **Timeline IR declarativo + validador + renderer determinista (B)**. El Patrón A es una trampa de fragilidad; el Patrón C aporta la disciplina de command-bus y la UX de control dual, que B puede tomar prestadas. La autonomía end-to-end pura (sin IR) es empíricamente débil (**~30% en AgenticVBench**).

---

## 3. Nivel de determinismo — Model A vs Model B

Tu intuición es correcta y no es opinión: **Model B es la vía robusta.**

- **Model A** (el LLM ejecuta comandos): no-determinista, no-reproducible, no-testeable, sin auditoría. Un mismo prompt → dos vídeos distintos.
- **Model B** (el LLM planifica, un motor determinista ejecuta): reproducible, validable *antes* de renderizar, preview barato, y —clave para vos— una **representación intermedia** que se exporta a donde quieras, incluido CapCut.

**Matiz honesto sobre "determinista":** ningún renderer da bit-exactness cross-host gratis. Se logra reproducibilidad **solo tras pinear**: settings de encoder + fonts para FFmpeg/MoviePy, y versión de browser + fonts + aleatoriedad seedeada para Remotion/Motion Canvas. El determinismo *perceptual* es fácil; el *byte-exacto* requiere disciplina. Solo **Kinocut** (receipts/hashes) y **dawn-cut** (validación de contigüidad/duración) tratan la reproducibilidad como propiedad verificable de primera clase — el resto la deja implícita.

---

## 4. Representación interna / "Timeline como código"

Cómo representan los proyectos el timeline/clips/pistas/overlays/subtítulos/keyframes/transiciones/efectos:

- **Kinocut:** JSON flat de operaciones tipadas (EDL-adjacent), **sin modelo de tracks**; compositing vía op `composite_layers`. Estado/provenance en el "Video Receipt".
- **video-use:** EDL propio derivado de un transcript de palabras (markdown compacto). Subtítulos quemados; overlays vía Remotion/Manim/PIL; transiciones como filter chains.
- **dawn-cut:** `.dawn` JSON (command-based con undo/redo) + EDL validado; mapeo probable **transcript↔timeline** en ambos sentidos. **OTIO export en roadmap.**
- **FireRed:** Pydantic `TimelineTracks` con 4 tracks (video/subtítulos/voz/música), `TimeWindow` en ms. Sin sistema general de keyframes.
- **OpenChatCut:** timeline como **JSON de proyecto inmutable**.
- **Ninguno** ofrece un **timeline realmente declarativo, multi-track, con keyframes generales, versionado como contrato estable** ("Terraform para vídeo"). **Ese es el hueco central** (§11).

### ¿Y OTIO como "timeline como código"? → No para el core. Ver §6.

---

## 5. Agentes y hosts

- **Claude Code / OpenCode / Cursor / Codex / Cline** son **clientes MCP completos** (stdio + HTTP) → cualquier MCP server estándar es cargable hoy. La restricción real no es el protocolo, son las **dependencias de runtime** (Resolve Studio, Blender, la app de CapCut).
- Patrones de integración vistos: **MCP server** (Kinocut, FireRed, OpenChatCut), **Skill de coding-agent** (video-use, FireRed, OpenMontage), **CLI + cliente** (Kinocut).
- Frameworks de orquestación (LangChain/LangGraph): FireRed usa `langchain` + `langchain_mcp_adapters`. Para tu caso, con Claude Code/OpenCode como host, **el propio agente ES el orquestador** — no necesitás CrewAI/AutoGen en el core.

---

## 6. OpenTimelineIO (OTIO) — análisis y veredicto

> **Veredicto: NO uses OTIO como IR central. IR propio modelado sobre `draft_content.json` de CapCut. OTIO = export secundario (one-way) para interop con NLEs pro.**

### Qué modela BIEN
Estructura de corte (tracks/clips/gaps, anidado, capas), **timing frame-accurate** (`RationalTime`/`TimeRange`, racional, sin drift de float), retiming (`LinearTimeWarp`, `FreezeFrame`), linkeo de media (URLs externas, secuencias de imágenes, refs múltiples), markers/metadata. **Su esqueleto es un buen IR agent-facing.**

### Qué PIERDE (lo decisivo para vídeo social)
La documentación y los maintainers de OTIO son explícitos: **"OTIO tiene soporte muy limitado de efectos."** El schema `Effect` es esencialmente `{effect_name, enabled, metadata}` — un nombre + una bolsa de metadata. **No hay schema estándar** para:
- **Parámetros animados / keyframes** — la animación "queda enteramente a la app host" (Discussion #921). *Este es el gap más grande:* opacidad/escala/posición/zoom keyframeados (Ken Burns), texto animado, máscaras animadas — el vocabulario central del vídeo social — **no tienen representación interoperable.**
- **Estilo de texto/subtítulos** — no hay schema de texto (font, color, stroke, sombra, burbuja de fondo, timing por palabra, karaoke/highlight, safe-area). **Los subtítulos son la feature #1 del vídeo vertical y OTIO no modela ninguna.**
- **Transforms por clip** (posición/escala/rotación/crop/anchor) — esenciales para reframe 9:16 de fuente 16:9. Van a parar a `metadata`.
- **Filtros / color / LUTs, máscaras/mattes, generadores** — sin schema estándar.

**¿Es lossy? Sí, por diseño.** Solo el `.otio` nativo (JSON) es lossless; todo efecto fuera del schema vive en `metadata` app-específica que **no sobrevive** un salto a otro host. → OTIO te daría un contenedor de serialización, **no interoperabilidad semántica real**, justo para las features que más te importan.

### Realidad de adapters (crítico)
- Desde **v0.17 (jun 2024)** los adapters salieron del core al metapaquete `OpenTimelineIO-Plugins` (AAF, EDL/CMX3600, FCP7 XML, `xges`, etc.; FCPXML y Kdenlive en contrib).
- Los que existen llevan **bien el corte y mal los efectos** — la pérdida se concentra exactamente en tus diferenciadores.
- **NO existe adapter de CapCut/JianYing** (ni core, ni plugins, ni contrib). CapCut tampoco importa OTIO. → Rutear por OTIO agrega un salto lossy **y aún así tenés que escribir el writer de `draft_content.json`**. OTIO no te ahorra nada en el camino crítico.
- Resolve (18+), Premiere (beta nativo) y Kdenlive (25.04) sí tienen OTIO **nativo** — pero CapCut, tu target obligatorio, no.

### Madurez / gobernanza
Proyecto **ASWF** (Linux Foundation), Apache-2.0, gobernanza formal, alineado a VFX Reference Platform. Última **v0.18.1 (nov 2025)**; v0.18.0 agregó `enabled` a Effect + primitiva Color. **Sigue pre-1.0** — señal de que el schema (sobre todo efectos) todavía evoluciona. Serialización JSON legible con `OTIO_SCHEMA: "Type.Version"` por objeto y funciones de up/downgrade → **buen patrón de versionado** (esto sí lo robamos). **Riesgo de longevidad: bajo.**

### ¿Hay MCP de OTIO? No.
No existe MCP server de OTIO (solo un tool de diff de ~1★). En cambio, **el lado CapCut ya tiene ecosistema MCP** que escribe `draft_content.json` directo (§9). El ecosistema ya "votó".

### Qué robamos de OTIO igual
`RationalTime`/`TimeRange` (timing racional frame-accurate), el **versionado de schema por tipo (`Type.Version`) con funciones de upgrade**, y la forma de árbol Timeline→Track→Clip/Gap/Transition. Y un **exporter OTIO one-way opcional** (Fase 3+) para quien quiera saltar a Resolve/Premiere, que emite estructura + retiming y **descarta efectos con gracia**.

---

## 7. MCP para video — estado del ecosistema

**Real pero disparejo**, en tres tiers:

- **Tier 1 (maduros):** **Blender MCP** (`ahujasid/blender-mcp`, ~24.7k★ — el más adoptado, pero 3D/compositing, no NLE); **DaVinci Resolve MCP** (`samuelgursky`, ~1.8k★ — el NLE-MCP más maduro, envuelve la API de scripting de Resolve, **requiere Resolve Studio pago corriendo**, no headless); **CapCut/JianYing** (`sun-guannan/VectCutAPI` ~2.1k★ — draft JSON vía HTTP+MCP).
- **Tier 2:** FFmpeg (la categoría más poblada y fragmentada — `egoist/ffmpeg-mcp` ~120★ pero **stale**; **kinocut** ~88★ el más activo con guardrails); Remotion (el **`@remotion/mcp` oficial es de *documentación*, no de edición/render**; los de edición son de comunidad).
- **Tier 3 (experimental):** Kdenlive, Shotcut, MoviePy — todos 1–17★.
- **OTIO:** **no hay MCP general** (solo un diff de ~1★). El gap estructural más grande.

**Veredicto:** no hay un "video-editing MCP" canónico. Los self-contained (FFmpeg/MoviePy/kinocut) son los más CI/agent-friendly; los atados a GUI (Resolve/Blender/CapCut) no son headless.

---

## 8. Renderers deterministas

Para el motor determinista bajo el IR:

| Renderer | Licencia | Determinismo | Vertical 9:16 | Subtítulos animados | Keyframes / fit como target de LLM | Headless | Peso |
|---|---|---|---|---|---|---|---|
| **Remotion** | **BUSL** (paga a escala) | Fuerte por diseño (pinear browser+fonts+seed) | ✅ (mejores templates) | ⭐ **la referencia** (`@remotion/captions` + Whisper.cpp, word-highlight) | Declarativo/keyframe (React) — **mejor fit LLM** | `npx remotion render` (Chrome Headless Shell) | Alto (tax de browser/frame) |
| **Revideo** (fork de Motion Canvas) | MIT | Fuerte | ✅ | Capaz, menos turnkey | Imperativo | Sí (API de render server-side) | Alto |
| **Motion Canvas** | MIT | Fuerte | ✅ | Manual | Imperativo (peor fit LLM) | Débil nativo (usar Revideo) | Alto |
| **FFmpeg** (filtergraph) | LGPL (GPL con x264/x265) | Filtergraph determinista; **bytes dependen del encoder** (pinear `-threads 1`, fonts) | ✅ (`scale`/`pad`/`crop`) | Vía ASS/SSA (karaoke/`\t`) — potente pero **arcano para LLM** | Expresiones de filtro (arcano) | ⭐ CLI nativo | Bajo/rápido |
| **MoviePy** | MIT | Hereda FFmpeg + PIL | ✅ | Manual | Python (moderado) | ✅ trivial | Bajo |
| **MLT XML** (melt) | LGPL | Determinista dentro de versión | ✅ (profile) | Débil | ⭐ **timeline XML declarativo keyframeable** — target de IR subestimado | ✅ `melt` CLI | Bajo |
| **Blender VSE** | GPL | Dentro de versión | ✅ | Manual | `bpy` (pesado) | Sí (virtual display) | Muy alto |

**La trampa de licencia (importante):** **Remotion es BUSL**, no OSI-open-source — se paga *Company License* a partir del 4º empleado / cierta facturación. Para un SaaS agéntico, **planificá este fork desde temprano**: Revideo (MIT) o FFmpeg+ASS.

**Lo lindo de tu decisión CapCut-as-final:** como el editor final es CapCut, **el renderer casi no lo necesitás** — solo para *preview* rápido. → Arrancá con un **proxy barato de FFmpeg** y **posponé** toda la decisión Remotion/licencia. Un problema menos en el v1.

---

## 9. El camino a CapCut (tu camino crítico) — en detalle

### El formato
Un proyecto de CapcCut/JianYing en disco es una **carpeta** (los generados en desktop se prefijan `dfd_...`):
- **`draft_content.json`** — el core: timeline, `tracks[]` (el orden = z-stacking), `materials` (videos/audios/texts/stickers/video_effects/transitions/masks/…), segmentos, keyframes. **Esto es lo que las libs escriben.** Tiempo en **microsegundos** en todo el formato.
- **`draft_meta_info.json`** — metadata de la librería de materiales (lo que se ve en el panel izquierdo).
- `draft_cover.jpg` y otros.

**Modelo de linkeo:** un **segmento** en un track referencia un **material** por id y lleva `target_timerange` (posición/largo en el timeline) + `source_timerange` (recorte del clip fuente), más `speed`, transiciones, animaciones y **keyframes** (curvas por propiedad: opacidad/posición/escala/rotación/volumen). **Texto/captions** son materials con estilo; en algunas versiones los rangos de texto son **por byte-offset** → fragilidad con multibyte/emoji.

### Las herramientas (todas Apache/MIT)
- **pyJianYingDraft** (GuanYixuan, Apache-2.0, ~4k★) — **la implementación de referencia** para **剪映 (JianYing, China)**. Casi todo lo demás es port/fork/wrapper de su conocimiento de schema.
- **pyCapCut** (mismo autor, ~600★) — hermano para **CapCut internacional** (el tuyo). Requiere templates `draft_content.json` **sin encriptar**. One-way.
- **CapCutAPI → VectCutAPI** (sun-guannan, Apache-2.0 la interfaz, ~2k★) — capa **HTTP + MCP** sobre la generación de drafts + render cloud opcional (**closed-source el motor cloud**). Perfiles `capcut_legacy`, `jianying_legacy`, `jianying_pro_10`. Varios forks-espejo.
- **jianying-mcp** (hey-jian-wei, MIT, Python) — ⭐ **el hallazgo clave**: MCP para que asistentes IA creen drafts de JianYing/CapCut con clips, audio, texto, **transiciones, filtros, máscaras, animaciones por keyframe**, fades de audio y efectos de texto, **exportando archivos de proyecto CapCut totalmente editables**. **Es lo más cercano que existe a tu objetivo.**
- **capcut-cli** (renezander030, MIT, ~190★, TypeScript) — lee **y** escribe `draft_content.json` con schema version-aware; mejor opción para **editar drafts existentes**.

### El riesgo #1 del proyecto (sin vueltas)
- **"Generate, don't round-trip".** Escriben un draft nuevo; **no leen de forma confiable** lo que la app guardó después.
- **JianYing 6.0+ encripta** `draft_content.json`. CapCut internacional (~6.x–9.x) es texto-plano-ish → **usá pyCapCut, target CapCut internacional.**
- **Formato de ingeniería inversa, cambia entre versiones.** Un auto-update de la app puede romper el export de un día para el otro. **Regla de oro: pineá la versión de CapCut**, whitelist de resource-IDs de efectos/fonts que resuelvan en el build target, y texto UTF-8-safe.
- **Región dividida:** 剪映 (China) y CapCut (internacional) son builds separados con schemas divergentes.

**Mitigación arquitectónica:** aislá el exporter detrás de una interfaz estable, con **golden-file tests** por versión de CapCut y un **canario** que detecte cuando un update rompió el formato. Esta capa es la que más disciplina de testing necesita.

---

## 10. Investigación académica (2024–2026)

- **LAVE (IUI 2024)** abrió el área: agente LLM que planifica/ejecuta acciones de edición sobre footage auto-indexado, con **control dual** (agente + manipulación directa).
- **Model B es el consenso** en 2025–2026: planner LLM/VLM → IR editable/inspeccionable → ejecución determinista. **Prompt-Driven Agentic Video Editing** (arXiv 2509.16811, Cambridge), **Aurora** (2605.18748, "mapea el request a un edit plan estructurado"), **VideoAgent** (2606.23327, HKUDS, 30+ agentes + graph optimization).
- **AgenticVBench** (2605.27705): los mejores stacks agénticos end-to-end llegan a **~30%** en 100 tareas reales de post-producción → **el IR auditable es ventaja competitiva.**
- **HCI:** intermedios editables/transparentes + control dual son un **requisito validado** (LAVE, VideoDiff CHI 2025, ExpressEdit, Co-Edit).
- **`browser-use/video-use`** es citado como el ejemplo OSS más limpio de "LLM escribe un `edl.json`, FFmpeg lo ejecuta".

> **Caveat honesto:** mi corte de conocimiento es enero-2026. Los papers de 2026 (26xx) fueron verificados por fetch de la página de arXiv (confianza media-alta para Aurora/AgenticVBench/VideoAgent; snippet-only para otros como Goku/LumiVideo). El "~30%" descansa en un solo preprint. No citar como dogma.

---

## 11. Huecos de mercado (dónde está tu oportunidad)

1. **Ningún IR realmente declarativo, multi-track, keyframeable, versionado como contrato.** Kinocut es flat (sin tracks); video-use/dawn-cut usan EDLs internos; FireRed usa Pydantic; OpenChatCut es JSON app-interno. **No existe el "Terraform de timelines".**
2. **No hay MCP general de OTIO** — la capa de interop obvia, sin cablear al ecosistema agéntico.
3. **Export a CapCut ausente de cualquier editor agent-native.** El ecosistema de drafts existe pero está desconectado de los editores Model B. **← tu oportunidad central.**
4. **Separación plan/ejecución pobre** en la mayoría (solo Kinocut y dawn-cut la imponen de verdad).
5. **Capa MCP inmadura y fragmentada.**
6. **Especialización vertical 9:16 + captions animados débil** — solo Remotion tiene el pipeline turnkey; **nadie combina IR declarativo + captions verticales de primera clase + export a CapCut**.
7. **Representación de escena/narrativa fina** — casi nadie modela un sistema general de keyframes/curvas.
8. **Sin garantías de determinismo empaquetadas** para el usuario.
9. **Bus-factor/madurez**: los más interesantes son <6 meses y de un maintainer.
10. **Sin estándar de validador-como-capa.**

---

## 12. Propuesta de arquitectura — reescrita para *CapCut-as-final*

Nombre de trabajo: **VertIR**. Tesis: **el LLM es planner, nunca renderer.** Patrón **D-over-B**. Combina lo mejor del survey. Ajuste clave a tu decisión: **el export a CapCut es la salida principal; el renderer propio es solo preview.**

### 12.1 El Timeline IR ("Terraform para vídeo")
Un documento JSON declarativo, versionado, idempotente — **el contrato**: único write-target del LLM, input del validador, fuente del exporter y del preview. **Modelado como superconjunto casi isomorfo del `draft_content.json` de CapCut**, para que el export sea casi mecánico.

```jsonc
{
  "irVersion": "1.0.0",
  "project": { "id": "uuid", "fps": 30, "canvas": { "w": 1080, "h": 1920 } }, // 9:16 default
  "assets": {                       // content-addressed; el media NO va en el IR
    "hero": { "sha256": "…", "path": "@sources/hero.mp4", "kind": "video" },
    "vo":   { "sha256": "…", "path": "@work/vo.wav",     "kind": "audio" }
  },
  "tracks": [
    { "kind": "video", "z": 0, "clips": [
      { "id": "c1", "asset": "hero",
        "source":   { "startUs": 2150000, "endUs": 6900000 },  // recorte del fuente
        "timeline": { "startUs": 0,       "endUs": 4750000 },  // posición en el output
        "transform": { "scale": 1.0, "x": 0, "y": 0 },
        "keyframes": [
          { "prop": "scale", "atUs": 0,       "v": 1.0,  "ease": "spring" },
          { "prop": "scale", "atUs": 4750000, "v": 1.08 }
        ] } ] },
    { "kind": "caption", "z": 10, "style": "word-highlight",
      "units": [ { "atUs": 120000, "endUs": 360000, "text": "THIS" } ] },
    { "kind": "voiceover", "z": 0, "clips": [ { "asset": "vo", "timeline": { "startUs": 0 } } ] },
    { "kind": "bgm", "z": 0, "clips": [ { "asset": "…", "gainDb": -18, "duck": true } ] }
  ],
  "transitions": [ { "between": ["c1","c2"], "type": "fade", "durUs": 300000 } ]
}
```

Decisiones de diseño:
- **Microsegundos enteros + intervalos semiabiertos** (dawn-cut) → sin drift, contigüidad demostrable. *Además, µs es la unidad nativa de CapCut → mapeo directo.*
- **Assets por SHA-256, media fuera del IR** (Kinocut/OpenChatCut) → IR chico, diffable, versionable, CI-friendly.
- **Z-order multi-track explícito** (FireRed/CapCut) → compositing determinista y **mapeo directo al orden de tracks de CapCut**.
- **Modelo general de keyframes/curvas** — el hueco que casi todos dejan abierto.
- **Versionado de schema estilo OTIO** (`Type.Version` + funciones de upgrade).

### 12.2 Componentes (pipeline reescrito para CapCut-as-final)

```
Claude Code / OpenCode
        │  (MCP stdio/HTTP)  ── Patrón D
        ▼
┌──────────────────────────────────────────────────────────────┐
│ VertIR MCP Server (tools tipadas, resultados estructurados)   │
│   ingest → transcribe(Whisper.cpp) → pack transcript          │
│   ir.propose / ir.mutate   (el LLM SOLO escribe/edita el IR)  │
│   ir.validate  ───────────► VALIDATOR (fail-closed)           │
│   ir.preview   ───────────► proxy FFmpeg (preview + MP4 final)│
│   web.tweaker  ───────────► finish humano en navegador móvil ◄ PRIMARIO
│   ir.export_capcut ───────► draft de CapCut (secundario, desktop)
│   timeline_view ──────────► filmstrip+waveform PNG (self-eval)│
│   [futuro] ir.render_final ► Remotion/Revideo headless        │
│   [futuro] ir.export_otio  ► export one-way a NLEs pro        │
└──────────────────────────────────────────────────────────────┘
```

- **a) Ingest + transcript** — Whisper.cpp local (sin lock-in de API paga; arregla la dependencia de ElevenLabs de video-use). Transcript de palabras empaquetado en markdown compacto que el LLM lee.
- **b) Validator (fail-closed)** — corre **antes** de cualquier export/preview. Chequea: contigüidad + duración exacta; reglas de overlap; que los hashes de assets resuelvan; compatibilidad codec/resolución; **overflow de captions / safe-area 9:16**; tiempo de keyframes monótono; endpoints de transiciones válidos. Devuelve errores estructurados que el LLM debe corregir. **El LLM no puede saltearlo — sin flag de force.**
- **c) Web-tweaker (handoff humano PRIMARIO — mobile-first)** — una web liviana que lee el IR, muestra el preview del proxy y deja hacer ediciones acotadas (ajustar cortes, editar/mover subtítulos, reordenar clips) que se escriben de vuelta al IR. Anda en cualquier navegador de celular. **No es un NLE**: es un formulario/timeline-lite sobre un JSON, porque la IA ya hizo el 90%.

**c-bis) Exporter a CapCut (feature SECUNDARIA — power users de desktop)** — mapea el IR → carpeta `draft_content.json` + `draft_meta_info.json` (`dfd_*`), reutilizando el conocimiento de schema de **pyCapCut / CapCutAPI / jianying-mcp**. Como el IR ya usa µs, z-order y source/timeline windows, el mapeo es **casi mecánico**. **Generate-only, versión pineada, resource-IDs en whitelist, texto UTF-8-safe.** Golden-file tests por versión.
- **d) Preview (proxy FFmpeg)** — render rápido de baja resolución solo para que el humano/agente vea el plan sin round-trip a CapCut. `-threads 1` + fonts bundleadas para reproducibilidad.
- **e) Self-eval acotado** (video-use) — tras el preview, re-corre `timeline_view` en cada corte para detectar saltos/pops/captions mal timeados; el agente corrige el IR y re-genera, máx. 3 loops.
- **f) Video Receipt** (Kinocut) — cada export/preview emite SHA-256 de input/output + cursor de reanudación → auditable y reanudable.

### 12.3 Interfaces
- **Primaria:** MCP server (stdio + HTTP) → cargable en Claude Code / OpenCode hoy.
- **Skill** de Claude Code / OpenCode para install + uso guiado.
- **CLI + cliente Python/TS** sobre el mismo motor (paridad triple estilo Kinocut) para CI/scripting.
- **Style Skills** (FireRed): archivar un workflow completo de generación de IR como Markdown replayable.
- **Después:** preview web read-only (no hace falta GUI de NLE para el v1); GUI web como *visor/editor del IR* (nunca un CapCut desde cero).

### 12.4 Lo que deliberadamente NO copiamos
- Nada de Chat→FFmpeg crudo (Patrón A).
- Nada de dependencia de API de transcripción paga (Whisper.cpp local).
- Nada del framing "GUI-first, IA opt-in" (dawn-cut/OpenCut) — el agente + IR **es** el producto.
- Nada de transiciones AIGC de terceros caras/impredecibles en el core (FireRed).
- Nada de intentar round-trip de drafts encriptados — **solo export**.
- Nada de OTIO como core (§6).

---

## 13. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| **Formato CapCut frágil / encriptación / updates que rompen** (riesgo #1) | Generate-only; pinear versión/región (CapCut internacional); whitelist de resource-IDs; **golden-file tests** por versión; canario de detección de ruptura; aislar el exporter tras interfaz estable. |
| **Determinismo condicional** | Pinear `-threads`/preset/fonts (FFmpeg) y browser/fonts/seed (si se usa Remotion); receipts que **detectan** drift. |
| **El export IR→CapCut es lossy/asimétrico** (captions/keyframes propios sin equivalente limpio) | Bakear lo que no mapea (perdiendo editabilidad en CapCut) o restringir el core a lo que CapCut sí modela; documentar qué sobrevive. |
| **Techo de fiabilidad del LLM (~30%)** | Validator + self-eval; el tool es fuerte en edición mecánica, débil en "gusto"; humano remata en CapcCut. |
| **El LLM no ve frames crudos** | Transcript + PNGs on-demand + self-eval de 3 loops (red de seguridad parcial). |
| **Licencia de Remotion (BUSL)** | Diferido por CapCut-as-final; si se necesita render final, fork a Revideo (MIT) o FFmpeg+ASS. |
| **Diseñar un IR general estable es difícil** | Empezar acotado (talking-head/shorts), modelado sobre CapCut; versionar estilo OTIO; no sobre-modelar. |
| **Bus-factor de dependencias** (<6 meses, un maintainer) | No atarse a un solo proyecto; apoyarse en pyCapCut/CapCutAPI/jianying-mcp como referencia, no como dependencia dura. |
| **Scope creep (la trampa de ~130 tools de Kinocut)** | v1 minimalista y disciplinado (§14). Profundidad > amplitud. |
| **Seguridad** (un agente que maneja FFmpeg = ejecución arbitraria: filtergraph injection, path traversal) | El validator debe cubrir también **safety adversaria**, no solo correctness; sandbox del render. |

---

## 14. Roadmap y próximos pasos

> **Decisiones cerradas:** host = Claude Code (MCP + Skill); IR propio modelado sobre CapCut; **handoff humano = superficie web propia liviana sobre el IR (mobile-first)**; export a draft de CapCut = feature secundaria (desktop power users); motor en Python (tentativo). Ver §0.

### Fase 1 — v1 walking skeleton (el corte más fino end-to-end)
Un caso angosto: **talking-head vertical (1080×1920) → short con subtítulos**.
`ingest → transcript Whisper.cpp → el agente (Claude Code) emite el Timeline IR (cortar filler/silencios en límites de palabra, auto-reframe 9:16, captions word-highlight) → validator fail-closed → render proxy FFmpeg → **web-tweaker**: el humano ajusta cortes/subtítulos en el navegador (del celular) → render final MP4`. Expuesto por MCP + Skill de Claude Code. Con Video Receipts.

### Fase 2
Loop de self-eval, Style Skills, más efectos/captions, música con duck, **export a draft de CapCut** (secundario, desktop).

### Fase 3+
Render headless opcional (Revideo/Remotion) para publicar sin tocar nada; export OTIO one-way (interop Resolve/Premiere); mejoras de la web-tweaker.

### Componentes del v1 (lo que hay que construir)
1. **El Timeline IR** (esquema JSON, §12.1) — el contrato. *Primera cosa a diseñar en detalle.*
2. **El validador fail-closed.**
3. **Ingest + transcript** (Whisper.cpp).
4. **Render proxy** (FFmpeg) — preview + MP4 final.
5. **La web-tweaker** — vista + edición liviana del IR; sirve preview; escribe de vuelta al IR.
6. **El MCP server + Skill** para Claude Code.
7. *(Fase 2)* El exporter a draft de CapCut.

### Decisiones abiertas restantes
1. **Caso de uso exacto del v1** (¿talking-head → short con subtítulos?).
2. **Stack de la web-tweaker** (React vs algo más liviano; preview con `<video>` del proxy vs Remotion Player).
3. **Alcance exacto de edición** que expone la web-tweaker en el v1.
4. **Región/versión de CapCut** para el export secundario (pyCapCut vs pyJianYingDraft).

---

## 15. Honestidad — límites de confianza de este informe

- **Números blandos:** estrellas/commits/fechas vienen de fetch de GitHub que se contradice. Órdenes de magnitud, no exactos. Verificar en vivo antes de citar.
- **Bifurcación temporal:** hechos pre-sept-2025 = alta confianza; todo lo 2026-dated (buena parte de los proyectos flagship y los papers 26xx) descansa en búsqueda en vivo más allá de mi corte (enero-2026) = confianza media.
- **Proyectos jóvenes/volátiles:** Kinocut, dawn-cut, FireRed, OpenChatCut son <6 meses y de un maintainer — cualquier conclusión puede quedar obsoleta en semanas.
- **Sin baseline comercial:** el informe es OSS-only. **Descript (Underlord), Opus Clip, Captions.ai, ChatCut.io, Adobe Firefly, Runway** son el frente comercial y tus **competidores reales** para un creador — no investigados acá. Hueco a cubrir si el objetivo es producto.
- **No cubiertos y adyacentes:** **Auto-Editor** (~3k★, silence/jump-cut OSS canónico) y **ComfyUI** (el ecosistema OSS más grande de "grafo declarativo + ejecución determinista", análogo fuerte de Model B).
- **Schema de CapCut = ingeniería inversa/aproximada** por admisión de las propias fuentes; el export depende de eso.
- **X-CUT no existe** (probable OpenChatCut/ChatCut mal nombrado). **dawn-cut y OpenCut NO son AI-first de verdad.**
- Un agente de research devolvió un placeholder para OTIO; se re-investigó por separado (§6, alta calidad).

---

*Documento generado a partir de research multiagente el 2026-07-23. Es un punto de partida para decidir, no un dogma. Revisar los caveats de §15 antes de tomar decisiones irreversibles.*

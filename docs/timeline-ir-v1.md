# Timeline IR — Especificación v1.1

> **El contrato.** Representación intermedia declarativa: único write-target del agente, input del validador, superficie de edición de la web-tweaker, y fuente del renderer y del export a CapCut.
>
> **Alcance v1 (actualizado):** talking-head vertical (1080×1920) → short. Incluye lo que el creador **siempre** mete: cortar filler/silencios en límites de palabra, auto-reframe 9:16, **subtítulos word-highlight**, música con ducking, **b-roll/cortes de apoyo**, **placas de intro/outro (hook cards)** y **logo/marca de agua**.
>
> **Changelog v1 → v1.1:** incorpora (a) la revisión adversaria (anclaje de subtítulos, perfil de entrega/loudness, orden del stack de transform, campos derivados, ducking materializado, huecos del validador, fps racional, limpiezas de consistencia) y (b) el alcance real del creador (overlays, placas, logo).

---

## 1. Invariantes de diseño

| Invariante | Regla |
|---|---|
| **Tiempo entero** | Todos los **campos de tiempo** en **microsegundos (µs) enteros**. (Solo el *tiempo* es entero; escalas/ganancias/opacidad son floats con precisión definida en §5.) |
| **Intervalos** | Semiabiertos `[startUs, endUs)`. `durUs = endUs − startUs`. |
| **Media** | Assets por **SHA-256 + path**; bytes nunca en el IR. |
| **Compositing** | El orden del array `tracks[]` = z-order (índice 0 = fondo). **No hay campo `z`.** |
| **Determinismo** | `render = f(IR, entorno pineado)`. El IR describe el **entregable completo** (incluye perfil de salida, §2.1). |
| **Campos derivados** | `project.durationUs`, y `timeline.*` del track principal, los **computa el motor**. El LLM **no** los emite (§2.3). |
| **Versionado + forward-compat** | `irVersion` semver de documento. Un validador de major N **ignora campos opcionales desconocidos** (compat hacia adelante en minors). Cualquier objeto admite `ext: {}` (ignorado por validador/render). |

**Regla de oro:** validador *fail-closed*. Sin `force`.

---

## 2. `project`, `assets` y campos derivados

### 2.1 `project` (con perfil de entrega)

```jsonc
{
  "irVersion": "1.1.0",
  "project": {
    "id": "uuid-v4",
    "title": "Mi short",
    "fps": { "num": 30000, "den": 1001 },   // RACIONAL (soporta 29.97/23.976); 30 = {30,1}
    "canvas": { "w": 1080, "h": 1920 },
    "output": {                              // el entregable, NO "entorno"
      "deliveryProfile": "tiktok",           // tiktok | reels | shorts | generic
      "loudnessLufs": -14.0, "truePeakDb": -1.0,
      "videoCodec": "h264", "crf": 18, "pixFmt": "yuv420p",
      "colorPrimaries": "bt709", "transfer": "bt709",
      "audioCodec": "aac", "audioBitrateKbps": 192
    },
    "cutAudioFadeUs": 30000,                 // fade de audio auto en cada corte (política, aplicada por render Y export)
    "durationUs": 0                          // DERIVADO por el motor (ver §2.3); el LLM lo omite
  }
}
```

### 2.2 `assets`

```jsonc
"assets": {
  "hero": { "sha256": "…", "path": "@sources/hero.mp4", "kind": "video",
    "probe": { "durationUs": 95000000, "w": 1920, "h": 1080,
               "fps": { "num": 30000, "den": 1001 }, "hasAudio": true, "sampleRateHz": 48000 } },
  "broll1": { "sha256": "…", "path": "@sources/city.mp4", "kind": "video", "probe": { … } },
  "logo": { "sha256": "…", "path": "@assets/logo.png", "kind": "image", "probe": { "w": 512, "h": 512 } },
  "bgm":  { "sha256": "…", "path": "@work/lofi.m4a", "kind": "audio", "probe": { "durationUs": 120000000, "sampleRateHz": 44100 } }
}
```
`kind`: `video | audio | image`. `probe` lo llena el motor (`project.ingest`), no el LLM.

### 2.3 Campos derivados (clave para la emitibilidad por LLM)

- El **track principal** (`role: "main"`) es **contiguo**: el LLM emite solo `source` (recorte del fuente) + `gaps` explícitos; el motor **deriva** `timeline.startUs/endUs` por contigüidad. Editar un corte hace *ripple* automático.
- `project.durationUs = timeline end del track principal` (los tracks de audio/overlay **no** alargan el vídeo; si la música excede, el motor la recorta con warning).
- El LLM que emita esos campos → el motor los ignora (no es error).

---

## 3. Anclaje: source-time vs program-time (el arreglo #1)

**El problema que resuelve:** los subtítulos y el b-roll de apoyo tienen que quedar pegados a *las palabras que se dicen*. Si están en tiempo de programa absoluto, cualquier corte los desincroniza.

**Solución:** dos formas de anclar un elemento —

- **`anchor: "source"`** → guarda `sourceAtUs`/`sourceEndUs` (timestamp del **material original**). El motor los mapea a tiempo de programa vía el **cut-map** del track principal (cada clip main = `[sourceStart,sourceEnd) → [progStart,progEnd)`). **Cualquier ripple re-sincroniza solo.** Úsanlo subtítulos y b-roll ligado al discurso.
- **`anchor: "program"`** → guarda `atUs`/`endUs` en tiempo de programa. Úsanlo elementos no ligados al discurso: **logo** (todo el vídeo), **placa de intro** (antes del contenido), **outro**.

Si un elemento source-anchored cae en un rango cortado, el motor lo descarta/recorta y el validador avisa (warning).

---

## 4. Tracks

Orden del array = z-order (fondo → frente). Roles y solape:

- **No-solape es POR TRACK.** El solape **entre** tracks es el mecanismo de compositing (overlays encima del main).

### 4.1 Track principal de vídeo (`kind:"video", role:"main"`)
Uno solo. Contiguo (ripple). El clip lleva su audio.

```jsonc
{ "id":"main", "kind":"video", "role":"main", "clips":[
  { "id":"c1", "asset":"hero",
    "source": { "startUs":1200000, "endUs":5400000 },   // el LLM emite ESTO
    "speed": 1.0,                                        // v1: 1.0
    "reframe": { "mode":"cover", "focusX":0.5, "focusY":0.40 },  // cover | contain | crop
    "transform": { "scale":1.0, "x":0, "y":0, "opacity":1.0 },   // DELTA sobre el reframe (ver §6)
    "audio": { "gainDb":0.0, "mute":false },
    "fadeInUs": 0, "fadeOutUs": 0,                       // fade desde/hacia negro
    "transitionIn": { "type":"cut", "durUs":0 },         // cut | dissolve (con el clip previo)
    "keyframes": [ /* §5 */ ] }
  // timeline.* de cada clip lo deriva el motor
] }
```

### 4.2 Tracks de overlay visual (`kind:"video"|"image", role:"broll"|"logo"|"overlay"`)
B-roll de apoyo y logo. Solapan con el main (compositing).

```jsonc
{ "id":"ov1", "kind":"video", "role":"broll", "clips":[
  { "id":"b1", "asset":"broll1", "anchor":"source",     // queda sobre las mismas palabras
    "sourceAtUs":3100000, "sourceEndUs":5200000,         // dónde (en el discurso) va encima
    "source": { "startUs":0, "endUs":2100000 },          // qué parte del b-roll se usa
    "reframe": { "mode":"cover", "focusX":0.5, "focusY":0.5 },
    "transform": { "scale":1.0, "x":0, "y":0, "opacity":1.0 },
    "audioUnderneath": true,                              // se sigue oyendo el talking-head
    "fadeInUs":150000, "fadeOutUs":150000, "keyframes":[] } ] }

{ "id":"logoTrack", "kind":"image", "role":"logo", "clips":[
  { "id":"lg", "asset":"logo", "anchor":"program",
    "atUs":0, "endUs":-1,                                 // -1 = todo el programa
    "transform": { "scale":0.18, "x":420, "y":-840, "opacity":0.85 } } ] }  // esquina
```

### 4.3 Track de placas / títulos (`kind:"title"`) — intro/outro/hook cards
Texto a pantalla (o sobre fondo). Program-anchored típicamente.

```jsonc
{ "id":"titles", "kind":"title", "clips":[
  { "id":"intro", "anchor":"program", "atUs":0, "endUs":1500000,
    "background": { "type":"blurredSource" },             // solid | color | blurredSource | transparent
    "text": { "content":"3 TRUCOS DE EDICIÓN", "fontFamily":"Montserrat", "fontWeight":900,
              "fontSizePx":110, "fillColor":"#FFFFFF", "strokeColor":"#000000", "strokePx":6,
              "align":"center", "position":{ "anchor":"center", "yPct":0.45 } },
    "fadeInUs":200000, "fadeOutUs":200000 } ] }
```

### 4.4 Track de subtítulos (`kind:"caption"`) — source-anchored
Word-highlight. **Anclado a source** (viene del transcript, que ya tiene timestamps de fuente).

```jsonc
{ "id":"caps", "kind":"caption",
  "style": { "preset":"word-highlight", "fontFamily":"Montserrat", "fontWeight":800,
    "fontSizePx":76, "fillColor":"#FFFFFF", "highlightColor":"#FFE000",
    "strokeColor":"#000000", "strokePx":8, "uppercase":true,
    "maxWordsPerLine":3, "autoFit":true,                  // si desborda, achica (remedio, no error muerto)
    "position": { "anchor":"blockCenter", "yPct":0.74 },  // punto de referencia definido
    "safeArea":"tiktok" },                                // tiktok | reels | shorts | generic
  "lines": [                                              // anchor:"source" implícito en captions
    { "words": [
        { "sourceAtUs":1350000, "sourceEndUs":1720000, "text":"ESTO" },
        { "sourceAtUs":1720000, "sourceEndUs":2010000, "text":"CAMBIA" },   // borde compartido con la previa
        { "sourceAtUs":2010000, "sourceEndUs":2480000, "text":"TODO" } ] }
  ] }
```
- `line.atUs/endUs` **derivados** de sus words (no se emiten).
- Bordes compartidos: `word[n].sourceAtUs := word[n-1].sourceEndUs` (evita solapes de 1µs por redondeo). Redondeo *round-half-even*.

### 4.5 Tracks de audio (`kind:"audio", role:"bgm"|"voiceover"|"sfx"`)
```jsonc
{ "id":"bgmTrack", "kind":"audio", "role":"bgm", "clips":[
  { "id":"m1", "asset":"bgm", "anchor":"program", "atUs":0, "endUs":-1,
    "source": { "startUs":0, "endUs":12300000 },
    "gainDb":-18.0,
    "duck": { "enabled":true, "targetDb":-26.0 },         // attack/release = default pineado; se MATERIALIZA (§7)
    "fadeInUs":500000, "fadeOutUs":1200000,
    "keyframes":[] } ] }                                   // keyframes animan gainDb
```

---

## 5. Keyframes

```jsonc
"keyframes": [
  { "prop":"scale", "atUs":0, "v":1.00, "ease":"linear" },
  { "prop":"scale", "atUs":4200000, "v":1.05, "ease":"easeInOut" }
]
```
- **Props:** `scale`, `x`, `y`, `opacity` (visual); `gainDb` (audio). *(Sin `rotDeg` en v1.)*
- **`atUs` relativo al inicio del clip en programa.** Límite: `0 ≤ atUs ≤ clip.durUs` (el borde `durUs` es límite de interpolación; el último frame mostrado es `endUs − frameDur`).
- **`ease`:** `linear | easeInOut | hold`. **Sin `spring` en v1** (era lossy a CapCut y no-determinista). `hold` = escalón (mantiene hasta el próximo kf).
- **Precisión de floats:** `scale`/`opacity` a 1e-4; `gainDb` a 0.1 dB. `atUs` monótono no-decreciente; empates → gana el último (o usar `hold`).

---

## 6. Stack de transform (orden definido — arreglo #3)

Composición, **en este orden exacto**:

1. **`reframe`** → produce el encuadre base (fit del fuente al canvas 9:16). Algoritmo definido para **todo** aspect de fuente:
   - fuente más ancha que 9:16 (16:9) → `cover` recorta a lo alto usando `focusX/Y`.
   - fuente = 9:16 → identidad; `focusX/Y` sin efecto (o `contain` con fondo desenfocado).
   - fuente más angosta (4:5, 1:1) → `contain` = pillarbox con fondo desenfocado (look talking-head estándar), o `cover` recorta.
2. **`transform`** → **delta** sobre el reframe (defaults identidad: scale 1, x/y 0, opacity 1).
3. **`keyframes`** → animan el **delta** (no reemplazan el reframe). Un keyframe `scale:1.0→1.05` es un zoom *encima* del encuadre base.

> En v1: si querés zoom (Ken Burns), animás `transform.scale` por keyframes; el reframe base nunca se pierde. Prohibido que un keyframe de `scale` baje de la escala de reframe (el validador lo chequea → evita barras negras).

---

## 7. Ducking materializado (arreglo #6)

`duck:{enabled:true, targetDb}` es **intención**. En render, el motor:
1. Analiza actividad de voz del track principal (side-chain).
2. **Materializa** el ducking como **keyframes de `gainDb`** en el clip de bgm (curva con attack/release pineados).

Ventaja: (a) determinista y portable (el resultado son keyframes, no un compresor dependiente del engine); (b) **exportable a CapCut** mecánicamente (keyframes de volumen). El IR de autoría guarda `duck:{enabled}`; el IR de render lleva los keyframes materializados.

---

## 8. Reglas del validador (fail-closed)

**Estructurales**
1. `irVersion` con major soportado; campos opcionales desconocidos → ignorados (no error).
2. Todo `clip.asset` existe; asset con `sha256` + `probe`.
3. Tiempos enteros, `start < end`, `≥ 0`.
4. `source` dentro de `probe.durationUs`; `timeline.durUs == retime(source, speed)` (v1: `speed==1`).
5. **IDs de clip globalmente únicos.**
6. Track principal: **exactamente uno**, no vacío, sin solape intra-track; overlays pueden solapar entre tracks.
7. `transitionIn.durUs ≤ min(dur del clip, dur del previo)`; `dissolve` requiere clip previo.

**Anclaje / cobertura**
8. Elemento `anchor:"source"` cuyo rango cae **entero** en un corte → warning (se descarta).
9. Cobertura de subtítulos = rango de programa del track principal; caption fuera → warning.

**Subtítulos (9:16-aware)**
10. `words[]` con `sourceAtUs` monótono; bordes compartidos.
11. **Sin solape entre `lines`** (word-highlight lo requiere).
12. **Safe-area** por perfil (`safeArea`): con `fontSizePx`, `maxWordsPerLine`, `position` → el bloque no desborda el canvas ni invade la zona de UI. Si `autoFit:true`, en vez de error se **achica** (remedio); si aún no entra → error.

**Keyframes / audio**
13. Keyframes monótonos, dentro del clip (§5); `scale` animado ≥ escala de reframe.
14. `bgm.gainDb` sano; `duck.targetDb` puede ser `≥` o `<` gainDb (si `≥`, no aplica extra-duck — **no es error**); fades `≥ 0`, `≤ dur`.

**Salidas:** `errors[]` (bloquean) + `warnings[]` (no). Sin `force`.

---

## 9. Superficie MCP (verbos del agente)

| Tool | Qué hace |
|---|---|
| `project.ingest(paths[])` | Registra assets + ffprobe (incl. fps racional). |
| `transcribe(assetId)` | Whisper.cpp word-level → transcript sidecar (con `sourceAtUs`). |
| `ir.get()` / `ir.propose(ir)` | Lee / setea el IR. |
| `ir.mutate(ops[])` | `cutFillers`, `trimClip`(ripple), `moveClip`, `removeClip`(limpia transiciones/anclas), `setTransform`, `addKeyframe`, `setCaptions`, `addBroll`, `addTitle`, `addLogo`, `setBgm`, `autoReframe`. |
| `ir.validate()` | §8 → errors/warnings. |
| `ir.preview(range?)` | Proxy FFmpeg baja-res + Video Receipt. |
| `timeline_view(range)` | PNG filmstrip+waveform (self-eval sin ingerir frames). |
| `ir.render()` | MP4 final (con perfil de salida §2.1) + Receipt. |
| `ir.export_capcut(version)` | *(Fase 2)* draft de CapCut. |

Mutaciones con **ripple**: `trimClip`/`removeClip` reflowean el main y **re-sincronizan** lo source-anchored automáticamente. Video Receipt (SHA-256 in/out, cursor, manifiesto) por render/export.

---

## 10. Mapeo IR → CapCut (feature secundaria) y puntos lossy

| IR | CapCut |
|---|---|
| `canvas`, `fps` | `canvas_config`, `fps` |
| µs | µs (nativo) |
| tracks (orden) | `tracks[]` (orden = z) |
| clip main / overlay / logo | `segment` (`target`/`source_timerange`) |
| `reframe` **resuelto** a transform (pre-pass compartido con el render) | transform del segmento |
| `transform` + keyframes | keyframes por-propiedad |
| caption line/words | text `material` + segment |
| `duck` **materializado** a keyframes de volumen | `audio_fades`/keyframes de volumen |
| `title` card | text material a pantalla + fondo |

**Lossy (documentado, se hornea o aproxima):**
- **word-highlight**: CapCut no expresa limpio el resaltado por palabra vía el formato reverse-engineered → **decisión: hornear a líneas estáticas** en el export (se pierde editar el resaltado en CapCut) **o** emitir N segmentos de texto. En v1 el export usa `block`/línea; word-highlight queda render/preview-only. *(A confirmar, §12.)*
- **Byte-offset de texto**: el exporter calcula offsets por **longitud UTF-8** (É=2 bytes, ☕=3) — golden tests con acentos/emoji.
- **cut fades de 30ms**: van al export también (política `cutAudioFadeUs`), no solo al preview → el entregable = el preview aprobado.
- reframe resuelto por el **mismo** pre-pass en render y export (si no, preview ≠ CapCut).

---

## 11. Mapeo IR → web-tweaker (handoff humano primario, ripple-safe)

| Edición humana | Op | Nota |
|---|---|---|
| Ajustar in/out de clip | `trimClip` | ripple + re-sync de captions/b-roll source-anchored |
| Reordenar / borrar | `moveClip`/`removeClip` | limpia transiciones y anclas colgantes |
| Editar texto de subtítulo | `setCaptions` | `autoFit` evita invalidar por overflow |
| Mover timing de subtítulo | `setCaptions` | en source-time |
| Mover/recortar b-roll | `addBroll`/edit | source-anchored |
| Cambiar placa intro/outro | `addTitle`/edit | program-anchored |
| Mover/opacar logo | `setTransform` | |
| Volumen de música | `setBgm` | bajar gainDb no rompe el duck (§8.14) |

No es un NLE: formulario/timeline-lite sobre un JSON, en el navegador del celular.

---

## 12. Decisiones abiertas (para cerrar juntos)

1. **word-highlight en el export a CapCut:** ¿hornear a líneas estáticas (simple, pierde editar el resaltado) o emitir N segmentos de texto (más fiel, más frágil)? *Recomendación: hornear en v1; el resaltado editable queda para el render propio.*
2. **B-roll: ¿lleva su propio audio alguna vez, o siempre `audioUnderneath:true`** (se oye el talking-head)? *Recomendación: siempre underneath en v1; audio del b-roll = después.*
3. **Placas intro/outro: ¿fondo por defecto `blurredSource` o `solid`?** *Recomendación: `blurredSource` (look talking-head).*
4. **`contain` con fondo desenfocado para fuentes verticales/cuadradas** — ¿lo incluimos en v1? *Recomendación: sí (es común).*

---

## 13. Fuera de alcance del v1 (para no sobre-modelar)

Speed ramps · rotación (`rotDeg`) · efectos/filtros/LUTs · máscaras/mattes · stickers/generadores complejos · transiciones más allá de cut/dissolve/fade · edición multi-speaker · 3D · export OTIO. Se agregan después **como extensión del schema versionado** (`ext:{}` + minors forward-compat), sin romper v1.

---

## 14. Plan de construcción (diseño completo, build en fases)

El IR de arriba cubre **todo** tu alcance real → **cero re-arquitectura** después. Pero se construye en rebanadas para ver algo funcionando rápido:

- **Rebanada 1 (core):** ingest → transcript → main track (cortar filler + reframe) → captions word-highlight → validate → render proxy + MP4 → web-tweaker mínima. *El primer vídeo publicable.*
- **Rebanada 2:** b-roll (overlays source-anchored) + logo (overlay program-anchored).
- **Rebanada 3:** placas intro/outro + ducking materializado + perfil de entrega/loudness.
- **Rebanada 4:** export a CapCut (feature secundaria).

---

*Especificación v1.1. El schema es el contrato: cambios versionados, nunca rompiendo documentos existentes. Decisiones abiertas en §12.*

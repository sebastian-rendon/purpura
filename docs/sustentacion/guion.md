# Guion de sustentación — Release 1 PÚRPURA

**Duración total**: 18 minutos (objetivo: 15-20 min para defender el R1).

**Setup recomendado**:
- Pantalla principal proyectada: navegador a `http://77.42.85.184` + tab `/dashboard` admin precargado.
- Pantalla secundaria (laptop): este guion + repo abierto en VS Code.
- Tener listas las 3 cuentas (admin / coord.ing / estudiante1, estudiante3) en pestañas separadas.
- `tests/visual_validation.py` ya corrido el día anterior con screenshots disponibles.

---

## Estructura (18 min)

| # | Bloque | Quién | Duración |
|---|---|---|---|
| 1 | Apertura + contexto del problema | Felipe | 2:00 |
| 2 | Demo en vivo (cara pública) | Sebastián | 2:30 |
| 3 | Modelo de datos + máquinas de estado | José Carlos | 3:00 |
| 4 | Demo en vivo (ciclo completo: postular → adjudicar) | Santiago + José Carlos | 4:00 |
| 5 | Capa IA híbrida (en vivo) | Santiago | 2:30 |
| 6 | Reportes + CSV + producción | Felipe | 1:30 |
| 7 | Decisiones defendibles + Q&A | Todos | 2:30 |

---

## Bloque 1 · Apertura (Felipe, 2:00)

> Buenas [tarde]. Somos el equipo PÚRPURA. Les vamos a presentar el Release 1 de un Sistema de Gestión de Monitorías Académicas para la Universidad de Medellín.
>
> El problema que resolvimos es concreto: el programa de monitorías hoy funciona en hojas de Excel y correos cruzados entre coordinaciones y estudiantes. No hay trazabilidad, no hay un único registro de quién postuló a qué, no hay sustento auditable de por qué se aprobó o rechazó una postulación. Y el coordinador no tiene tiempo para revisar manualmente cada caso.
>
> Construimos un sistema web con tres roles diferenciados — estudiante, coordinador, administrador — que centraliza el ciclo completo desde la publicación de una convocatoria hasta la designación final del monitor. Y agregamos una capa de inteligencia artificial que, **siempre como asesoría, nunca como decisión**, sugiere al coordinador si un postulante es apto.
>
> El sistema está **en producción ahora mismo** en `http://77.42.85.184`. Pueden conectarse desde su celular después.

**Cambio de slide / pantalla**: poner la landing pública del sistema.

---

## Bloque 2 · Demo cara pública (Sebastián, 2:30)

**En pantalla**: navegar la landing pública en `http://77.42.85.184` (sin login).

> Esta es la cara pública del sistema. Pueden ver el logosímbolo oficial de la UdeM en el navbar — lo exportamos del Figma del equipo y construimos un script Python que genera tres variantes desde el SVG: color para fondos blancos, blanco para el footer negro, y un composite sobre rojo institucional para banners hero.

**Acción**: scroll por la landing — hero rojo, las 4 stats, "Cómo funciona", los 3 perfiles, preview de convocatorias activas, FAQ, CTA final.

> La paleta y la tipografía siguen el manual de identidad gráfica adaptado al contexto digital que definió Felipe en el Figma: rojo `#C8202D`, footer negro `#1A1A1A`, tipografía Open Sans para títulos y Roboto para cuerpo.

**Acción**: click en "Documentación" del navbar → muestra las 6 cards.

> La documentación es estática, accesible sin login, pensada para que cualquiera entienda el sistema.

**Acción**: click en "Iniciar sesión" → muestra los tabs de rol.

> Los tabs son cosméticos — cuando el estudiante elige "Estudiante", el placeholder del email cambia. Pero la autorización real viene de la BD: cualquier cosa que el cliente envíe se descarta en el backend. Es defensa en profundidad.

---

## Bloque 3 · Modelo de datos + máquinas de estado (José Carlos, 3:00)

**En pantalla**: VS Code con `docs/entrega/entrega_release1.md` abierto en la sección 3 (diagrama ER).

> El núcleo del sistema son ocho entidades. Tres son de catálogo — `users`, `facultades`, `materias`. Tres son transaccionales — `convocatorias`, `postulaciones`, `monitores`. Dos transversales — `notificaciones`, `audit_log`.

**Mostrar**: diagrama ER del documento.

> La decisión técnica clave acá fue **no usar Alembic para migraciones**. Tenemos una función `run_migrations()` que corre al startup de la app y aplica `ALTER TABLE IF NOT EXISTS`. Idempotente. El trade-off: no se trackea historial formal de schema. Para R1 con un equipo de 4 personas no nos hacía falta. Para R2 lo introducimos.

**Cambio de pantalla**: mostrar el diagrama de la máquina de estados de Convocatoria.

> Las dos máquinas de estado son explícitas. Convocatoria tiene 5 estados, Postulación tiene 7. Cada transición permitida vive en un diccionario `TRANSICIONES`, con sus perfiles autorizados. **Una transición no listada lanza excepción**. No hay un if/elif diseminado en el código que se pueda olvidar de actualizar.

**Ejemplo concreto** (mostrar en código):

> En la postulación, EN_REVISION puede ir a APROBADA o RECHAZADA con perfiles `admin` o `coord_conv_owner`. Pero PUBLICADA → BORRADOR es **admin only** — el coordinador no puede despublicar, solo el administrador. Esa regla vive en el dict del service, no en una capa de UI que se pueda saltar.

---

## Bloque 4 · Demo ciclo completo (Santiago + José Carlos, 4:00)

**Setup**: 3 tabs ya abiertos:
- Tab A: admin@purpura.local logueado
- Tab B: coord.ing@udem.edu.co logueado
- Tab C: estudiante1@udem.edu.co logueado

### Paso 1 — Admin publica una convocatoria

**En Tab A** (admin):
> El admin crea una convocatoria nueva — vamos a usar una que ya existe en estado BORRADOR para no demorar.

**Acción**: ir a `/convocatorias`, buscar una en estado BORRADOR, abrir detalle, click "Publicar" → la convocatoria pasa a PUBLICADA. Mostrar el badge cambiando.

> Cada transición se persiste con autor y timestamp en `historial_estados` JSONB. Eso es nuestro audit trail.

### Paso 2 — Estudiante postula

**En Tab C** (estudiante1):
> El estudiante 1 tiene datos académicos completos en su perfil: promedio 4.5, créditos 80, semestre 6. Va a postular a una convocatoria que requiere promedio mínimo de 4.0.

**Acción**: ir a `/convocatorias`, abrir una PUBLICADA, click "Postularme". → redirect a `/mis-postulaciones` con flash de éxito.

### Paso 3 — Coord ve la bandeja

**En Tab B** (coord.ing):
> El coordinador ve la postulación en su bandeja inmediatamente. La columna "IA" ya muestra la sugerencia.

**Acción**: ir a `/bandeja`. Mostrar la fila nueva con badge `APTO` (verde teal).

**Acción**: click "Revisar" → abre `/postulaciones/{id}` con detalle completo.

> El coordinador ve el detalle del estudiante, la convocatoria, **y la evaluación automática**.

### Paso 4 — IA en acción (modo reglas)

**En Tab B**:
> La card de "Evaluación automática" dice **modo: reglas, modelo: reglas-v1**. Eso significa que el sistema verificó determinísticamente los requisitos con los datos del User. No invocó al LLM. La justificación es textual: "Cumple los requisitos automáticos: promedio 4.5 ≥ 4.0".

**Mostrar**: la card con el check verde.

### Paso 5 — Coord empieza revisión y aprueba

**En Tab B**:
> El coordinador toma la postulación. Click "Empezar revisión" → estado pasa a EN_REVISION.

**Acción**: scroll abajo, llenar motivo opcional, click "Aprobar".

> Estado pasa a APROBADA. La notificación al estudiante se dispara en el commit.

### Paso 6 — Estudiante ve la campana

**Volver a Tab C** (estudiante1) y hacer F5.

> En el navbar, la campana ahora tiene un badge rojo con número. El estudiante hace click...

**Acción**: click en campana → muestra dropdown con las últimas 5 no leídas.

> ...y ve: "¡Postulación aprobada en MON-...!". Click en la notif → marca leída + navega al detalle.

### Paso 7 — Admin cierra y adjudica

**Volver a Tab A** (admin):

**Acción**: ir al detalle de la convocatoria → click "Cerrar" → estado CERRADA → aparece banner "pendiente de adjudicar" → click "Adjudicar ahora".

> El admin ve la lista de aprobadas con checkboxes. Marca al estudiante 1. El contador "Seleccionadas" sube. El sistema NO permite marcar más allá del cupo.

**Acción**: marcar la única casilla → click "Confirmar adjudicación".

> Transacción atómica: una sola operación cambia la postulación a ADJUDICADA, crea un Monitor activo, y pasa la convocatoria a ADJUDICADA. Si algo falla a la mitad, rollback completo.

**Acción**: redirect al detalle de conv → mostrar card "Resumen de adjudicación" con el monitor designado.

### Paso 8 — Estudiante ve su monitoría

**Volver a Tab C** (estudiante1):

**Acción**: click "Mis monitorías" en navbar.

> Acá ve la monitoría que acaba de ganar, con código, materia, semestre y fecha de adjudicación.

---

## Bloque 5 · Capa IA — modo LLM real (Santiago, 2:30)

**Setup**: logout y login como estudiante3@udem.edu.co (sin datos académicos).

**En pantalla**: postular como estudiante3.

> Ahora hago el mismo flujo pero con un estudiante distinto. Estudiante 3 tiene los 3 campos académicos en NULL — todavía no llenó el perfil. Postula igual.

**Acción**: postular a una convocatoria nueva.

**Logout + login admin**, ir a `/bandeja`, abrir esa postulación.

> La card de evaluación automática dice ahora **modo: llm, modelo: claude-haiku-4-5-20251001**. Tokens consumidos: in 505, out 123.

**Mostrar**: la justificación textual generada por Haiku en tiempo real.

> Decisión sugerida: REVISAR_MANUAL. La justificación: "Datos insuficientes: promedio acumulado no registrado, créditos aprobados no disponibles, semestre actual desconocido. No se puede verificar si aprobó ISI-301 con nota ≥4.0 ni disponibilidad de 8 horas. Requiere validación manual en sistema académico."

> Esta justificación la generó Claude en vivo, ahora mismo, con los datos reales de la postulación.

**Punto crítico (resaltar)**:

> Importante: la IA dice **decision_sugerida**, no decision. En toda la UI usamos el lenguaje "sugerencia", "asesoría", "recomendación". El coordinador es siempre el decisor. Hay un disclaimer obligatorio bajo cada card que dice: *"Esta es una sugerencia algorítmica. La decisión final corresponde al coordinador o administrador."*

> Y si la API de Anthropic falla — timeout, rate limit, key inválida — el sistema cae a un modo fallback que dice REVISAR_MANUAL. **El sistema nunca rompe por culpa de la IA externa**. La postulación se persiste igual, el coord la revisa manualmente.

---

## Bloque 6 · Reportes + CSV + producción (Felipe, 1:30)

**En Tab A** (admin), ir a `/reportes`.

> El módulo de reportes muestra 4 KPIs en vivo: convocatorias activas, postulaciones totales, monitorías adjudicadas, y tasa de adjudicación. Filtros por facultad y rango de fechas. Tabla por facultad y por convocatoria.

**Acción**: click en "Postulaciones" del exportar bar → descarga el CSV.

> CSV con BOM UTF-8 y separador `;` para que Excel español lo abra con acentos. 12 columnas, incluyendo la decisión sugerida por IA y el modo. Esto es **trazabilidad institucional** — la coordinación puede auditar cualquier decisión semanas después.

**Mostrar el CSV abierto** brevemente en Excel/LibreOffice si está disponible.

> El sistema está deployado en un VPS de Hetzner. Cada commit en `main` activa un workflow manual: `git pull` en el VPS, rebuild del container Docker, restart. Migraciones idempotentes al startup. El smoke E2E con Playwright corre desde mi máquina contra producción via túnel SSH — 56 chequeos automáticos verde.

---

## Bloque 7 · Decisiones defendibles + Q&A (todos, 2:30)

**Felipe abre**:

> Tres decisiones que queremos dejar grabadas, porque son las que defendemos en cualquier preguntas técnica:

**Santiago**:
> Uno: **máquinas de estado explícitas en dict**, no if/elif. Cada transición es testeable directamente. Un cambio de regla es un cambio en una línea. Cualquier transición prohibida lanza excepción.

**José Carlos**:
> Dos: **IA como input, nunca decisor**. El campo se llama `decision_sugerida`. Disclaimer obligatorio. El coordinador siempre dice la última palabra. Esto no es solo ético — es defensivo: si la IA falla, el sistema sigue funcionando.

**Sebastián**:
> Tres: **soft delete vía estado ARCHIVADA**. No hay `DELETE FROM convocatorias`. Todo el historial queda en la BD para auditoría. El partial unique index `WHERE estado != 'CANCELADA'` nos permite que un estudiante cancele y vuelva a postular sin romper la unicidad.

**Felipe cierra**:

> Quedamos a disposición para preguntas.

---

## Preguntas anticipadas + respuestas pulidas

### Q1 — "¿La IA puede equivocarse y rechazar a un buen estudiante?"

> Por eso la IA **sugiere, no decide**. La sugerencia se llama `decision_sugerida` en BD y en la UI. El coordinador siempre revisa y aprueba o rechaza con su propio criterio. Lo único que la IA hace es ahorrar tiempo: si un caso es claramente APTO con promedio alto y todos los créditos, el coord lo ve marcado en verde y lo pasa rápido. Si es ambiguo, lo marca como REVISAR_MANUAL y el coord lo evalúa con detenimiento.

### Q2 — "¿Por qué Claude Haiku y no GPT u otro?"

> Tres razones. Primero, Haiku 4.5 es el modelo más reciente y rápido de Anthropic — la mediana de respuesta está bajo 2 segundos, lo cual mantiene la UX viva. Segundo, el SDK oficial de Anthropic tiene soporte de timeout nativo, que usamos en 10 segundos. Tercero, el SDK acepta `system` y `user` como roles separados, lo cual nos permite enviar las reglas como prompt de sistema y los datos del caso como prompt de usuario — más limpio que stuffing everything en un solo prompt.

### Q3 — "¿Qué pasa si la API key se vence o se queda sin créditos?"

> El sistema sigue funcionando. Cae al modo fallback con decisión `REVISAR_MANUAL` + justificación textual: "Evaluación automática no disponible (RateLimitError). Se requiere revisión manual.". La postulación se persiste igual, la notificación llega igual, el coordinador revisa manualmente — exactamente como funcionaba antes de la IA. No hay dependencia crítica.

### Q4 — "¿Cómo manejan datos sensibles del estudiante?"

> Tres capas. Uno, autenticación con bcrypt + rounds 12 + sesiones server-side firmadas. Dos, el `historial_estados` con notas privadas del coord NO se expone al estudiante — la nota vive en una ruta que requiere rol coord o admin. Tres, en el detalle de una convocatoria ADJUDICADA, el estudiante ve solo el total de monitores designados, no los nombres. Coord y admin sí ven nombres + emails. Los CSV exportados son para uso interno de coordinación.

### Q5 — "¿Por qué no usan email para notificaciones?"

> Decisión de alcance del R1. En R1 las notificaciones son in-app: campana en el navbar + pantalla `/notificaciones` con todas las novedades. R2 introduce email transaccional (SendGrid o similar) y opcionalmente WhatsApp Business API para las notificaciones críticas (aprobación, adjudicación). La infraestructura ya está: `crear_notificacion()` es el único punto de inserción, podemos agregar un hook que también envíe email sin tocar el resto del código.

### Q6 — "¿Cuántas líneas de código?"

> Sin contar tests ni templates HTML, el backend Python son alrededor de 2 500 líneas: `app/main.py` ~1 100, los 5 services (`transiciones`, `evaluacion_ia`, `adjudicacion`, `notificar`, `reportes`) ~800, `models.py` + `db.py` + `auth.py` + `config.py` ~500. Los templates Jinja son otros ~1 200 líneas, y el smoke E2E con Playwright cerca de 800 líneas. CSS Figma ~550 líneas. Bajo, compacto, sin frameworks innecesarios.

### Q7 — "¿Cómo testearon todo esto?"

> 56 chequeos automatizados con Playwright headed que recorren el sistema con tres roles distintos: estudiante, coordinador, administrador. Cada chequeo verifica un comportamiento concreto: que el botón "Postular" aparezca solo si la conv está PUBLICADA y el estudiante no tiene postulación activa; que el coord NO vea las convocatorias de otros coords; que el filtro `?ia=revisar` en la bandeja devuelva solo postulaciones con decisión REVISAR_MANUAL; que la transición BORRADOR → ADJUDICADA esté prohibida y devuelva 303 con flash; etcétera. La suite corre en local antes de cada push, y en producción via túnel SSH después de cada deploy. Cada commit lleva un veredicto verde adjunto.

### Q8 — "¿Por qué un VPS y no un PaaS como Heroku/Render?"

> Costo y control. El VPS de Hetzner cuesta menos de 5 EUR/mes, soporta el ciclo completo del demo sin throttling, y nos da SSH directo para diagnóstico. El deploy es manual pero idempotente: 3 comandos. Para un proyecto académico esto es óptimo. Para producción institucional real, R2 introduciría CI/CD con GitHub Actions y posiblemente un PaaS según el presupuesto institucional.

### Q9 — "¿Cuál fue la decisión más difícil?"

> Probablemente cómo manejar la postulación cuando el estudiante no tiene datos académicos en su perfil. Dos opciones: bloquear la postulación hasta que llene su perfil, o permitir postular y delegar la validación al coordinador / a la IA. Elegimos lo segundo. Razón: el sistema académico real de la UdeM no expone esos datos via API para nosotros, y forzar al estudiante a copiarlos manualmente introduce errores y fricción. Mejor permitir la postulación con datos parciales y que la IA lo marque como `REVISAR_MANUAL`, dejando al coord la verificación. En R2, con integración al sistema académico, la validación es automática.

### Q10 — "¿Y si dos coordinadores quieren tomar la misma postulación al mismo tiempo?"

> Las postulaciones pertenecen a la convocatoria, y la convocatoria pertenece a un único `created_by` (el coordinador owner). Solo ese coord — o el admin — puede transicionarla. No hay race condition: la postulación vive bajo una sola "jurisdicción". Si en R2 introducimos coordinación compartida, agregamos una tabla `convocatoria_coordinador` many-to-many y un lock optimista con `updated_at` en la transición.

### Q11 — "¿Qué medirían para considerar exitoso el R1 en uso real?"

> Cuatro métricas: (1) tasa de adopción — cuántos estudiantes postulan via el sistema vs vía email tradicional, objetivo > 80% en el primer semestre. (2) Tiempo medio de revisión por postulación — esperamos < 24 h. (3) Concordancia coord-IA — qué porcentaje de las decisiones del coord coincide con la sugerencia de la IA. Si es muy alta (>95%), la IA agrega valor; si es muy baja (<60%), revisamos el prompt. (4) Postulaciones que terminan en ADJUDICADA / postulaciones totales — la "tasa de adjudicación", que ya mostramos en `/reportes`.

### Q12 — "¿Está listo para producción institucional?"

> Para una piloto académico con esta facultad, sí. El sistema está estable, deployado, validado con 56 tests E2E. Para producción institucional masiva (toda la UdeM), R2 introduce los items de roadmap: HTTPS con Caddy, backups automatizados, monitoreo, integración con el sistema académico, CI/CD, escalabilidad horizontal si hace falta. R1 cubre el flujo de extremo a extremo; R2 lo endurece operacionalmente.

---

## Ensayo: cronómetro

Sugerencia: hacer dos ensayos completos antes de la sustentación cronometrando cada bloque. Si un bloque pasa de su tiempo asignado, recortar la parte demostrativa (menos clicks, más narración). El objetivo es **dejar 4 minutos limpios para Q&A**.

## Notas finales

- Si algo falla en vivo (red lenta, timeout de IA), **NO dramatizar**. Decir simplemente: "El sistema tiene un modo fallback exactamente para esto" y seguir con la justificación textual del fallback. Eso *prueba* que la decisión arquitectónica fue correcta.
- Tener un screenshot de respaldo de la card IA modo=llm con la justificación de Haiku, por si la API responde lento.
- Hablar despacio, mostrar la pantalla con calma, dejar 2-3 segundos para que el jurado lea.

Suerte equipo.

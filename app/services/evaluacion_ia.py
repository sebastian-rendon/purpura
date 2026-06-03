"""
Evaluación automática híbrida de postulaciones.

Estrategia:
1. Si los datos del estudiante son suficientes para aplicar reglas
   determinísticas → decide AUTO_APTO / AUTO_NO_APTO.
2. Si los datos son insuficientes → invoca Gemini Flash con prompt
   estructurado y persiste la sugerencia.
3. Si la API falla (timeout, rate limit, key faltante, etc.) → fallback
   REVISAR_MANUAL sin levantar excepción al caller.

La función es PURA: recibe objetos, retorna dict, no toca DB. El router
es responsable de persistir el resultado en Postulacion.evaluacion_ia_ultima
y en historial_estados.

Constraint ético: el output se llama `decision_sugerida`, nunca `decision`.
El coordinador / administrador es quien decide. Esta capa es asesoría.
"""
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.0-flash"
_TIMEOUT_SEC = 10
_MAX_TOKENS = 300
_TEMPERATURE = 0.2

_PROMPT_SISTEMA = (
    "Eres un asistente de validación académica del programa de monitorías "
    "de la Universidad de Medellín. Recibes datos de una postulación a una "
    "convocatoria y debes sugerir si el estudiante es APTO, NO_APTO, o si "
    "requiere REVISAR_MANUAL.\n\n"
    "Reglas:\n"
    "- Si el estudiante claramente cumple los requisitos publicados → APTO.\n"
    "- Si claramente no los cumple → NO_APTO.\n"
    "- Si los datos son insuficientes o ambiguos → REVISAR_MANUAL.\n"
    "- Nunca decides 'aprobar' o 'rechazar' — solo sugieres. El coordinador "
    "toma la decisión final.\n\n"
    "Responde ÚNICAMENTE con JSON válido, sin markdown ni texto extra:\n"
    '{"decision": "APTO" | "NO_APTO" | "REVISAR_MANUAL", '
    '"confianza": 0.0-1.0, '
    '"justificacion": "explicación breve en español, máx 200 caracteres"}'
)


def _utcnow_iso() -> str:
    return datetime.utcnow().isoformat()


def _datos_academicos_completos(estudiante) -> bool:
    """STRICT: los 3 campos del User deben estar no-NULL para activar reglas.

    Si falta uno, la evaluación cae al LLM. Evita validaciones parciales
    engañosas (ej: aprobar solo por promedio sin haber validado créditos).
    """
    return (
        getattr(estudiante, "promedio_acumulado", None) is not None
        and getattr(estudiante, "creditos_aprobados", None) is not None
        and getattr(estudiante, "semestre_actual", None) is not None
    )


def _aplicar_reglas(estudiante, convocatoria) -> list[dict]:
    """Construye la lista de checks aplicables según `convocatoria.requisitos`.

    Asume que `_datos_academicos_completos(estudiante)` es True. Solo
    añade un check si la convocatoria publica el requisito correspondiente.
    """
    checks: list[dict] = []
    requisitos = convocatoria.requisitos or {}

    if requisitos.get("promedio_minimo") is not None:
        try:
            minimo = float(requisitos["promedio_minimo"])
            actual = float(estudiante.promedio_acumulado)
            checks.append(
                {
                    "regla": "promedio_minimo",
                    "esperado": f">= {minimo:.1f}",
                    "actual": round(actual, 2),
                    "ok": actual >= minimo,
                }
            )
        except (TypeError, ValueError):
            pass

    if requisitos.get("creditos_minimos") is not None:
        try:
            minimo = int(requisitos["creditos_minimos"])
            actual = int(estudiante.creditos_aprobados)
            checks.append(
                {
                    "regla": "creditos_minimos",
                    "esperado": f">= {minimo}",
                    "actual": actual,
                    "ok": actual >= minimo,
                }
            )
        except (TypeError, ValueError):
            pass

    if requisitos.get("semestre_minimo") is not None:
        try:
            minimo = int(requisitos["semestre_minimo"])
            actual = int(estudiante.semestre_actual)
            checks.append(
                {
                    "regla": "semestre_minimo",
                    "esperado": f">= {minimo}",
                    "actual": actual,
                    "ok": actual >= minimo,
                }
            )
        except (TypeError, ValueError):
            pass

    return checks


def _resumen_reglas(checks: list[dict]) -> str:
    fallidos = [c for c in checks if not c["ok"]]
    if not fallidos:
        return "Cumple los requisitos automáticos: " + ", ".join(
            f"{c['regla']} {c['actual']} {c['esperado']}" for c in checks
        ) + "."
    return "No cumple: " + "; ".join(
        f"{c['regla']} actual {c['actual']} vs esperado {c['esperado']}"
        for c in fallidos
    ) + "."


def _construir_user_message(postulacion, convocatoria, estudiante) -> str:
    facultad_nombre = "no especificada"
    materia_nombre = "no especificada"
    if getattr(convocatoria, "asignatura", None):
        materia_nombre = convocatoria.asignatura
    if getattr(convocatoria, "facultad", None):
        facultad_nombre = convocatoria.facultad

    promedio = getattr(estudiante, "promedio_acumulado", None)
    creditos = getattr(estudiante, "creditos_aprobados", None)
    semestre = getattr(estudiante, "semestre_actual", None)

    return (
        f"Convocatoria: {convocatoria.titulo}\n"
        f"Código: {convocatoria.codigo}\n"
        f"Facultad: {facultad_nombre}\n"
        f"Asignatura/materia: {materia_nombre}\n"
        f"Requisitos publicados:\n"
        f"{json.dumps(convocatoria.requisitos or {}, indent=2, ensure_ascii=False)}\n\n"
        f"Estudiante:\n"
        f"- Email: {estudiante.email}\n"
        f"- Nombre: {estudiante.full_name or 'no especificado'}\n"
        f"- Promedio acumulado: "
        f"{promedio if promedio is not None else 'no registrado'}\n"
        f"- Créditos aprobados: "
        f"{creditos if creditos is not None else 'no registrado'}\n"
        f"- Semestre actual: "
        f"{semestre if semestre is not None else 'no registrado'}\n\n"
        f"Motivación del estudiante:\n{postulacion.motivacion or 'no proporcionada'}\n\n"
        "Evalúa según las reglas y responde JSON."
    )


def _parsear_json_defensivo(texto: str) -> dict[str, Any]:
    """Intenta parsear JSON. Si viene con markdown fences o texto extra,
    extrae el primer bloque que parezca JSON."""
    if not texto:
        return {}
    texto = texto.strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[^{}]*\}", texto, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return {}


_DECISION_LLM_A_INTERNA = {
    "APTO": "AUTO_APTO",
    "NO_APTO": "AUTO_NO_APTO",
    "REVISAR_MANUAL": "REVISAR_MANUAL",
}


def _invocar_gemini(postulacion, convocatoria, estudiante) -> dict[str, Any]:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY no configurada")

    import google.generativeai as genai

    genai.configure(api_key=api_key)

    user_message = _construir_user_message(postulacion, convocatoria, estudiante)
    prompt_completo = _PROMPT_SISTEMA + "\n\n" + user_message

    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        generation_config=genai.types.GenerationConfig(
            max_output_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
        ),
    )

    resp = model.generate_content(prompt_completo)

    texto_resp = ""
    if resp.text:
        texto_resp = resp.text.strip()

    parsed = _parsear_json_defensivo(texto_resp)

    decision_llm = parsed.get("decision", "REVISAR_MANUAL")
    if not isinstance(decision_llm, str):
        decision_llm = "REVISAR_MANUAL"
    decision_sugerida = _DECISION_LLM_A_INTERNA.get(
        decision_llm.upper(), "REVISAR_MANUAL"
    )

    confianza_raw = parsed.get("confianza", 0.5)
    try:
        confianza = max(0.0, min(1.0, float(confianza_raw)))
    except (TypeError, ValueError):
        confianza = 0.5

    justificacion = parsed.get("justificacion") or "(sin justificación)"
    if not isinstance(justificacion, str):
        justificacion = str(justificacion)
    justificacion = justificacion[:300]

    # Tokens de uso si están disponibles
    tokens_in = 0
    tokens_out = 0
    if getattr(resp, "usage_metadata", None):
        tokens_in = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
        tokens_out = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0

    return {
        "decision_sugerida": decision_sugerida,
        "confianza": confianza,
        "modo": "llm",
        "justificacion": justificacion,
        "checks": [],
        "modelo": GEMINI_MODEL,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def evaluar_postulacion(
    postulacion, convocatoria, estudiante
) -> dict[str, Any]:
    """Evalúa una postulación y retorna un dict serializable.

    Bifurcación strict:
    - Si los 3 campos académicos del User están no-NULL → modo reglas.
    - Sino → modo LLM (con fallback REVISAR_MANUAL si la API falla).

    Nunca lanza excepción al caller.
    """
    base = {"evaluado_at": _utcnow_iso()}

    if _datos_academicos_completos(estudiante):
        try:
            checks = _aplicar_reglas(estudiante, convocatoria)
        except Exception as exc:
            LOGGER.warning("Falla aplicando reglas: %s", exc)
            checks = []
        if checks:
            todos_ok = all(c["ok"] for c in checks)
            return {
                **base,
                "decision_sugerida": "AUTO_APTO" if todos_ok else "AUTO_NO_APTO",
                "confianza": 1.0,
                "modo": "reglas",
                "justificacion": _resumen_reglas(checks),
                "checks": checks,
                "modelo": "reglas-v1",
            }

    try:
        llm_result = _invocar_gemini(postulacion, convocatoria, estudiante)
        return {**base, **llm_result}
    except Exception as exc:
        LOGGER.warning(
            "Evaluación IA cae en fallback por %s: %s",
            type(exc).__name__,
            exc,
        )
        return {
            **base,
            "decision_sugerida": "REVISAR_MANUAL",
            "confianza": 0.0,
            "modo": "fallback",
            "justificacion": (
                f"Evaluación automática no disponible "
                f"({type(exc).__name__}). Se requiere revisión manual."
            ),
            "checks": [],
            "modelo": "fallback",
        }


def get_ultima_evaluacion(postulacion) -> Optional[dict[str, Any]]:
    """Recupera la evaluación más reciente, priorizando el campo dedicado."""
    directo = getattr(postulacion, "evaluacion_ia_ultima", None)
    if directo:
        return directo
    historial = postulacion.historial_estados or []
    for evento in reversed(historial):
        if isinstance(evento, dict) and evento.get("tipo") == "evaluacion_ia":
            return {k: v for k, v in evento.items() if k != "tipo"}
    return None

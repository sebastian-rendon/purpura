"""
Evaluación automática de postulaciones via Gemini.

Flujo único:
1. Construir mensaje con datos de la convocatoria y el estudiante.
2. Consultar Gemini (siempre, sin importar si los datos están completos).
3. Parsear JSON de respuesta: decision, confianza, justificacion.
4. Si Gemini falla → fallback REVISAR_MANUAL sin levantar excepción.

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

GEMINI_MODEL = "gemini-2.5-flash"
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


def _construir_user_message(postulacion, convocatoria, estudiante) -> str:
    facultad_nombre = getattr(convocatoria, "facultad", None) or "no especificada"
    asignatura = getattr(convocatoria, "asignatura", None) or "no especificada"

    promedio = getattr(estudiante, "promedio_acumulado", None)
    creditos = getattr(estudiante, "creditos_aprobados", None)
    semestre = getattr(estudiante, "semestre_actual", None)

    requisitos = convocatoria.requisitos or {}

    return (
        f"Convocatoria: {convocatoria.titulo}\n"
        f"Código: {convocatoria.codigo}\n"
        f"Facultad: {facultad_nombre}\n"
        f"Asignatura/materia: {asignatura}\n"
        f"Descripción: {getattr(convocatoria, 'descripcion', None) or 'no especificada'}\n"
        f"Requisitos publicados:\n"
        f"- Promedio mínimo: {requisitos.get('promedio_minimo', 'no especificado')}\n"
        f"- Créditos mínimos: {requisitos.get('creditos_minimos', 'no especificado')}\n"
        f"- Semestre mínimo: {requisitos.get('semestre_minimo', 'no especificado')}\n\n"
        f"Estudiante:\n"
        f"- Email: {estudiante.email}\n"
        f"- Nombre: {getattr(estudiante, 'full_name', None) or 'no especificado'}\n"
        f"- Promedio acumulado: {promedio if promedio is not None else 'no disponible'}\n"
        f"- Créditos aprobados: {creditos if creditos is not None else 'no disponible'}\n"
        f"- Semestre actual: {semestre if semestre is not None else 'no disponible'}\n\n"
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


def _invocar_gemini(
    postulacion, convocatoria, estudiante, modelo: str = GEMINI_MODEL
) -> dict[str, Any]:
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY no configurada")

    import google.generativeai as genai

    genai.configure(api_key=api_key)

    user_message = _construir_user_message(postulacion, convocatoria, estudiante)
    prompt_completo = _PROMPT_SISTEMA + "\n\n" + user_message

    model = genai.GenerativeModel(
        model_name=modelo,
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
        "modelo": modelo,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def _cache_valido(postulacion) -> Optional[dict[str, Any]]:
    """Devuelve el resultado cacheado si es válido para reusar, o None.

    Se considera válido si tiene decision_sugerida distinta de REVISAR_MANUAL
    (los resultados REVISAR_MANUAL son inciertos y vale la pena reevaluar).
    """
    cached = getattr(postulacion, "evaluacion_ia_ultima", None)
    if not cached or not isinstance(cached, dict):
        return None
    decision = cached.get("decision_sugerida", "")
    if decision in ("AUTO_APTO", "AUTO_NO_APTO") and cached.get("justificacion"):
        return cached
    return None


def evaluar_postulacion(
    postulacion,
    convocatoria,
    estudiante,
    config: Optional[dict] = None,
    forzar_reevaluacion: bool = False,
) -> dict[str, Any]:
    """Evalúa una postulación consultando a Gemini.

    Si la postulación ya tiene un resultado cacheado con decision != REVISAR_MANUAL
    y ``forzar_reevaluacion`` es False, retorna el caché sin llamar a Gemini.

    ``config`` es un dict opcional con claves:
      - modelo_activo: str  (default GEMINI_MODEL)
      - umbral_confianza: float  (default 0.5; resultado con confianza < umbral → REVISAR_MANUAL)
      - modo_fallback: bool  (True → REVISAR_MANUAL en error; False → AUTO_NO_APTO)

    Nunca lanza excepción al caller.
    """
    if not forzar_reevaluacion:
        cached = _cache_valido(postulacion)
        if cached is not None:
            LOGGER.debug(
                "Cache hit para postulacion %s — decision: %s",
                getattr(postulacion, "id", "?"),
                cached.get("decision_sugerida"),
            )
            return cached

    cfg = config or {}
    modelo = cfg.get("modelo_activo") or GEMINI_MODEL
    umbral = float(cfg.get("umbral_confianza", 0.5))
    modo_fallback = bool(cfg.get("modo_fallback", True))

    base = {"evaluado_at": _utcnow_iso()}

    try:
        llm_result = _invocar_gemini(postulacion, convocatoria, estudiante, modelo=modelo)
        if llm_result.get("confianza", 1.0) < umbral:
            llm_result["decision_sugerida"] = "REVISAR_MANUAL"
            llm_result["justificacion"] = (
                f"[Confianza {llm_result['confianza']:.2f} < umbral {umbral:.2f}] "
                + llm_result.get("justificacion", "")
            )
        return {**base, **llm_result}
    except Exception as exc:
        LOGGER.warning(
            "Evaluación IA cae en fallback por %s: %s",
            type(exc).__name__,
            exc,
        )
        decision_fallback = "REVISAR_MANUAL" if modo_fallback else "AUTO_NO_APTO"
        return {
            **base,
            "decision_sugerida": decision_fallback,
            "confianza": 0.0,
            "modo": "fallback",
            "justificacion": "Servicio IA no disponible, requiere revisión manual.",
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

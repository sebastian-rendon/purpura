"""
Adjudicación batch atómica de una convocatoria.

`adjudicar_convocatoria()` es una función pura: valida invariantes,
muta los objetos en memoria (postulaciones + convocatoria) y retorna
la lista de Monitor a persistir. NO toca DB.

El caller (router) hace session.add()/session.commit(). Si lanza
excepción, el caller debe hacer rollback.
"""
from datetime import date
from typing import Iterable

from app.models import Convocatoria, ConvocatoriaStatus, Monitor, Postulacion, User
from app.services.transiciones import (
    POST_ADJUDICADA,
    POST_APROBADA,
    POST_NO_ADJUDICADA,
    transicionar_estado,
    transicionar_postulacion,
)


class AdjudicacionInvalidaError(Exception):
    """Falla de validación al adjudicar una convocatoria batch."""


def adjudicar_convocatoria(
    convocatoria: Convocatoria,
    postulaciones_aprobadas: list[Postulacion],
    ids_seleccionados: Iterable[int],
    usuario: User,
) -> tuple[list[Monitor], dict]:
    """Ejecuta la adjudicación batch.

    Args:
        convocatoria: la convocatoria a adjudicar. Debe estar en CERRADA.
        postulaciones_aprobadas: TODAS las postulaciones APROBADA de la
            convocatoria. La función las divide en adjudicadas vs
            no_adjudicadas según el set de IDs seleccionados.
        ids_seleccionados: IDs de postulaciones que ganan el cupo.
        usuario: User (administrador o coordinador owner) que dispara la adjudicación.

    Returns:
        (monitores_a_persistir, resumen). El caller hace session.add() de los
        monitores y session.commit() en una sola transacción.

    Raises:
        AdjudicacionInvalidaError: si la convocatoria no está CERRADA,
            si alguna seleccionada no está APROBADA o si excede cupos.
    """
    if convocatoria.status != ConvocatoriaStatus.CERRADA:
        raise AdjudicacionInvalidaError(
            f"La convocatoria debe estar CERRADA para adjudicar "
            f"(actual: {convocatoria.status.value})."
        )

    set_seleccionados = set(ids_seleccionados)

    ids_aprobadas = {p.id for p in postulaciones_aprobadas}
    fuera_de_aprobadas = set_seleccionados - ids_aprobadas
    if fuera_de_aprobadas:
        raise AdjudicacionInvalidaError(
            f"Postulaciones seleccionadas no están APROBADAS: {sorted(fuera_de_aprobadas)}."
        )

    no_aprobadas = [
        p for p in postulaciones_aprobadas if p.estado != POST_APROBADA
    ]
    if no_aprobadas:
        raise AdjudicacionInvalidaError(
            "La lista de aprobadas contiene postulaciones en otros estados: "
            f"{[(p.id, p.estado) for p in no_aprobadas]}."
        )

    if len(set_seleccionados) > convocatoria.cupos:
        raise AdjudicacionInvalidaError(
            f"Seleccionadas ({len(set_seleccionados)}) excede cupos "
            f"({convocatoria.cupos})."
        )

    motivo_adj = (
        f"Adjudicación final por {usuario.email}: "
        f"{len(set_seleccionados)} monitores asignados de "
        f"{len(postulaciones_aprobadas)} aprobadas."
    )
    motivo_no_adj = (
        f"No adjudicada tras decisión final de {usuario.email}."
    )

    monitores_a_crear: list[Monitor] = []
    for post in postulaciones_aprobadas:
        if post.id in set_seleccionados:
            transicionar_postulacion(
                post, POST_ADJUDICADA, usuario, convocatoria, motivo=motivo_adj
            )
            monitores_a_crear.append(
                Monitor(
                    postulacion_id=post.id,
                    convocatoria_id=convocatoria.id,
                    estudiante_id=post.estudiante_id,
                    fecha_adjudicacion=date.today(),
                    semestre=convocatoria.semestre,
                )
            )
        else:
            transicionar_postulacion(
                post,
                POST_NO_ADJUDICADA,
                usuario,
                convocatoria,
                motivo=motivo_no_adj,
            )

    transicionar_estado(
        convocatoria,
        ConvocatoriaStatus.ADJUDICADA,
        usuario,
        motivo=motivo_adj,
    )

    resumen = {
        "adjudicadas": len(set_seleccionados),
        "no_adjudicadas": len(postulaciones_aprobadas) - len(set_seleccionados),
        "cupos_totales": convocatoria.cupos,
        "cupos_libres": convocatoria.cupos - len(set_seleccionados),
    }
    return monitores_a_crear, resumen

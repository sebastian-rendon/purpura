"""
Máquinas de estado de Convocatoria y Postulacion.

Las funciones `transicionar_estado` y `transicionar_postulacion` son puras:
validan el mapa + permisos, mutan el objeto y appendean al historial.
NO hacen commit. El caller (router) es responsable de persistir.
"""
from datetime import datetime
from typing import Tuple

from app.models import Convocatoria, ConvocatoriaStatus, Postulacion, User, UserRole


# ============================================================
# Estados de Postulacion (strings, no enum Postgres)
# ============================================================

POST_ENVIADA = "ENVIADA"
POST_EN_REVISION = "EN_REVISION"
POST_APROBADA = "APROBADA"
POST_RECHAZADA = "RECHAZADA"
POST_ADJUDICADA = "ADJUDICADA"
POST_NO_ADJUDICADA = "NO_ADJUDICADA"
POST_CANCELADA = "CANCELADA"

POST_ESTADOS_TERMINALES = {
    POST_RECHAZADA,
    POST_ADJUDICADA,
    POST_NO_ADJUDICADA,
    POST_CANCELADA,
}


class TransicionInvalidaError(Exception):
    """La transición entre estados no está permitida por el mapa."""


class PermisoInsuficienteError(Exception):
    """El usuario no tiene rol u ownership para realizar la transición."""


_ADMIN_AND_COORD_OWNER: Tuple[str, ...] = ("admin", "coord_owner")
_ADMIN_ONLY: Tuple[str, ...] = ("admin",)

TRANSICIONES: dict[
    Tuple[ConvocatoriaStatus, ConvocatoriaStatus], Tuple[str, ...]
] = {
    (ConvocatoriaStatus.BORRADOR,   ConvocatoriaStatus.PUBLICADA):   _ADMIN_AND_COORD_OWNER,
    (ConvocatoriaStatus.BORRADOR,   ConvocatoriaStatus.ARCHIVADA):   _ADMIN_AND_COORD_OWNER,
    (ConvocatoriaStatus.PUBLICADA,  ConvocatoriaStatus.CERRADA):     _ADMIN_AND_COORD_OWNER,
    (ConvocatoriaStatus.PUBLICADA,  ConvocatoriaStatus.BORRADOR):    _ADMIN_ONLY,
    (ConvocatoriaStatus.CERRADA,    ConvocatoriaStatus.ADJUDICADA):  _ADMIN_AND_COORD_OWNER,
    (ConvocatoriaStatus.CERRADA,    ConvocatoriaStatus.PUBLICADA):   _ADMIN_ONLY,
    (ConvocatoriaStatus.ADJUDICADA, ConvocatoriaStatus.ARCHIVADA):   _ADMIN_AND_COORD_OWNER,
}


def transiciones_disponibles(
    conv: Convocatoria, usuario: User
) -> list[ConvocatoriaStatus]:
    """Lista los estados a los que ``usuario`` puede llevar ``conv``.

    Útil para renderizar botones de transición contextuales en la UI.
    """
    resultado: list[ConvocatoriaStatus] = []
    for (origen, destino), perfiles in TRANSICIONES.items():
        if origen != conv.status:
            continue
        if _puede(usuario, perfiles, conv):
            resultado.append(destino)
    return resultado


def transicionar_estado(
    conv: Convocatoria,
    nuevo_estado: ConvocatoriaStatus,
    usuario: User,
    motivo: str = "",
) -> Convocatoria:
    """Valida + muta el estado de ``conv``. No persiste.

    Levanta:
        TransicionInvalidaError: si (estado_actual, nuevo_estado) no está
            en TRANSICIONES.
        PermisoInsuficienteError: si el rol u ownership no autorizan la
            transición.
    """
    key = (conv.status, nuevo_estado)
    if key not in TRANSICIONES:
        raise TransicionInvalidaError(
            f"Transición {conv.status.value} → {nuevo_estado.value} no permitida"
        )
    if not _puede(usuario, TRANSICIONES[key], conv):
        raise PermisoInsuficienteError(
            f"Usuario {usuario.email} no puede transicionar de "
            f"{conv.status.value} a {nuevo_estado.value}"
        )

    historial = list(conv.historial_estados or [])
    historial.append(
        {
            "from": conv.status.value,
            "to": nuevo_estado.value,
            "by_user_id": str(usuario.id),
            "by_email": usuario.email,
            "motivo": motivo,
            "at": datetime.utcnow().isoformat(),
        }
    )
    conv.historial_estados = historial
    conv.status = nuevo_estado
    conv.updated_at = datetime.utcnow()
    return conv


def _puede(usuario: User, perfiles: Tuple[str, ...], conv: Convocatoria) -> bool:
    if usuario.role == UserRole.ADMINISTRADOR:
        return True
    if usuario.role == UserRole.COORDINADOR:
        return "coord_owner" in perfiles and conv.created_by == usuario.id
    return False


# ============================================================
# Postulaciones
# ============================================================

# Mapa: (estado_actual, estado_nuevo) -> perfiles autorizados.
# Perfiles posibles:
#   "estudiante_owner": el dueño de la postulación (rol estudiante)
#   "coord_conv_owner": coord owner de la convocatoria
#   "admin": cualquier administrador
TRANSICIONES_POSTULACION: dict[Tuple[str, str], Tuple[str, ...]] = {
    (POST_ENVIADA,     POST_CANCELADA):     ("estudiante_owner",),
    (POST_ENVIADA,     POST_EN_REVISION):   ("admin", "coord_conv_owner"),
    (POST_EN_REVISION, POST_CANCELADA):     ("estudiante_owner",),
    (POST_EN_REVISION, POST_APROBADA):      ("admin", "coord_conv_owner"),
    (POST_EN_REVISION, POST_RECHAZADA):     ("admin", "coord_conv_owner"),
    (POST_APROBADA,    POST_ADJUDICADA):    ("admin", "coord_conv_owner"),
    (POST_APROBADA,    POST_NO_ADJUDICADA): ("admin", "coord_conv_owner"),
}


def transicionar_postulacion(
    postulacion: Postulacion,
    nuevo_estado: str,
    usuario: User,
    convocatoria: Convocatoria,
    motivo: str = "",
) -> Postulacion:
    """Valida + muta el estado de ``postulacion``. No persiste.

    Necesita la ``convocatoria`` para verificar el perfil "coord_conv_owner".
    """
    nuevo_estado = nuevo_estado.upper()
    key = (postulacion.estado, nuevo_estado)
    if key not in TRANSICIONES_POSTULACION:
        raise TransicionInvalidaError(
            f"Transición de postulación {postulacion.estado} → {nuevo_estado} "
            "no permitida"
        )
    if not _puede_postulacion(
        usuario, TRANSICIONES_POSTULACION[key], postulacion, convocatoria
    ):
        raise PermisoInsuficienteError(
            f"Usuario {usuario.email} no puede transicionar la postulación "
            f"{postulacion.id} a {nuevo_estado}"
        )

    historial = list(postulacion.historial_estados or [])
    historial.append(
        {
            "from": postulacion.estado,
            "to": nuevo_estado,
            "by_user_id": str(usuario.id),
            "by_email": usuario.email,
            "motivo": motivo,
            "at": datetime.utcnow().isoformat(),
        }
    )
    postulacion.historial_estados = historial
    postulacion.estado = nuevo_estado
    postulacion.updated_at = datetime.utcnow()
    if nuevo_estado in {POST_APROBADA, POST_RECHAZADA, POST_ADJUDICADA, POST_NO_ADJUDICADA}:
        postulacion.decidida_por = usuario.id
        postulacion.decidida_en = datetime.utcnow()
    return postulacion


def _puede_postulacion(
    usuario: User,
    perfiles: Tuple[str, ...],
    postulacion: Postulacion,
    convocatoria: Convocatoria,
) -> bool:
    if "admin" in perfiles and usuario.role == UserRole.ADMINISTRADOR:
        return True
    if (
        "coord_conv_owner" in perfiles
        and usuario.role == UserRole.COORDINADOR
        and convocatoria.created_by == usuario.id
    ):
        return True
    if (
        "estudiante_owner" in perfiles
        and usuario.role == UserRole.ESTUDIANTE
        and postulacion.estudiante_id == usuario.id
    ):
        return True
    return False

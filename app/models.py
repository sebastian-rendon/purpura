import enum
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Column, Field, SQLModel


class UserRole(str, enum.Enum):
    ADMINISTRADOR = "administrador"
    COORDINADOR = "coordinador"
    DOCENTE = "docente"
    ESTUDIANTE = "estudiante"


class ConvocatoriaStatus(str, enum.Enum):
    BORRADOR = "borrador"
    PUBLICADA = "publicada"
    CERRADA = "cerrada"
    ARCHIVADA = "archivada"
    ADJUDICADA = "adjudicada"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    email: str = Field(unique=True, index=True, max_length=255)
    password_hash: str = Field(max_length=255)
    full_name: str = Field(max_length=255)
    role: UserRole = Field(index=True)
    is_active: bool = Field(default=True)
    contrasena_temporal: bool = Field(default=False)
    last_login_at: Optional[datetime] = None
    promedio_acumulado: Optional[float] = Field(default=None)
    creditos_aprobados: Optional[int] = Field(default=None)
    semestre_actual: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Facultad(SQLModel, table=True):
    __tablename__ = "facultades"

    id: Optional[int] = Field(default=None, primary_key=True)
    nombre: str = Field(unique=True, max_length=120)
    codigo: str = Field(unique=True, max_length=40)
    color_hex: str = Field(max_length=7)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Materia(SQLModel, table=True):
    __tablename__ = "materias"

    id: Optional[int] = Field(default=None, primary_key=True)
    facultad_id: int = Field(foreign_key="facultades.id", index=True)
    codigo: str = Field(unique=True, max_length=20)
    nombre: str = Field(max_length=255)
    creditos: int = Field(default=3)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Convocatoria(SQLModel, table=True):
    __tablename__ = "convocatorias"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    codigo: str = Field(unique=True, index=True, max_length=50)
    titulo: str = Field(max_length=255)
    descripcion: Optional[str] = None
    facultad: str = Field(max_length=255)
    asignatura: str = Field(max_length=255)
    cupos: int
    fecha_apertura: datetime
    fecha_cierre: datetime
    requisitos: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default="{}"),
    )
    status: ConvocatoriaStatus = Field(
        default=ConvocatoriaStatus.BORRADOR, index=True
    )
    created_by: uuid.UUID = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    facultad_id: Optional[int] = Field(
        default=None, foreign_key="facultades.id", index=True
    )
    materia_id: Optional[int] = Field(
        default=None, foreign_key="materias.id", index=True
    )
    semestre: str = Field(default="2026-1", max_length=20)
    historial_estados: list = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default="[]"),
    )


class Postulacion(SQLModel, table=True):
    __tablename__ = "postulaciones"

    id: Optional[int] = Field(default=None, primary_key=True)
    convocatoria_id: uuid.UUID = Field(
        foreign_key="convocatorias.id", index=True
    )
    estudiante_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    promedio_acumulado: float
    motivacion: str = Field(sa_column=Column(Text, nullable=False))
    archivo_pdf_path: Optional[str] = Field(default=None, max_length=512)
    score_automatico: Optional[float] = None
    decision_automatica: str = Field(default="PENDIENTE", max_length=20)
    """Valores válidos: PENDIENTE, APTA, NO_APTA, REVISAR."""
    justificacion_automatica: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    estado: str = Field(default="ENVIADA", max_length=20, index=True)
    """Valores válidos: ENVIADA, EN_REVISION, APROBADA, RECHAZADA, ADJUDICADA,
    NO_ADJUDICADA, CANCELADA. La unicidad activa (convocatoria, estudiante)
    excluye CANCELADA via partial index aplicado en run_migrations()."""
    decidida_por: Optional[uuid.UUID] = Field(
        default=None, foreign_key="users.id"
    )
    decidida_en: Optional[datetime] = None
    historial_estados: list = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default="[]"),
    )
    evaluacion_ia_ultima: Optional[dict] = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None


class Monitor(SQLModel, table=True):
    __tablename__ = "monitores"

    id: Optional[int] = Field(default=None, primary_key=True)
    postulacion_id: int = Field(
        foreign_key="postulaciones.id", unique=True
    )
    convocatoria_id: uuid.UUID = Field(
        foreign_key="convocatorias.id", index=True
    )
    estudiante_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    fecha_adjudicacion: date
    semestre: str = Field(max_length=20)
    estado: str = Field(default="ACTIVO", max_length=20)
    """Valores válidos: ACTIVO, FINALIZADO."""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Notificacion(SQLModel, table=True):
    __tablename__ = "notificaciones"

    id: Optional[int] = Field(default=None, primary_key=True)
    usuario_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    titulo: str = Field(max_length=255)
    cuerpo: str = Field(sa_column=Column(Text, nullable=False))
    url_destino: Optional[str] = Field(default=None, max_length=512)
    leida: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ConfiguracionIA(SQLModel, table=True):
    __tablename__ = "configuracion_ia"

    id: Optional[int] = Field(default=None, primary_key=True)
    umbral_confianza: float = Field(default=0.5)
    peso_promedio: int = Field(default=33)
    peso_creditos: int = Field(default=33)
    peso_semestre: int = Field(default=34)
    modelo_activo: str = Field(default="gemini-2.0-flash", max_length=100)
    modo_fallback: bool = Field(default=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[uuid.UUID] = Field(default=None, index=True)
    action: str = Field(max_length=100, index=True)
    """Valores válidos: LOGIN, LOGOUT, CREATE_CONVOCATORIA, PUBLICAR_CONVOCATORIA,
    CERRAR_CONVOCATORIA, POSTULAR, APROBAR_POSTULACION, RECHAZAR_POSTULACION,
    ADJUDICAR_MONITOR, EXPORTAR_REPORTE."""
    entity_type: Optional[str] = Field(default=None, max_length=100)
    entity_id: Optional[uuid.UUID] = None
    payload: Optional[dict] = Field(default=None, sa_column=Column(JSONB))
    ip_address: Optional[str] = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)

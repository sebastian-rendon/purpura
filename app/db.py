from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

settings = get_settings()

engine = create_engine(settings.DATABASE_URL, echo=False, pool_pre_ping=True)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def run_migrations() -> None:
    """Migra columnas y valores de enum nuevos sobre tablas ya existentes.

    SQLModel.metadata.create_all NO añade columnas a tablas creadas en releases
    previos (ej: convocatorias del Sprint 1 ya está en producción con datos).
    Aquí aplicamos ALTER TABLE IF NOT EXISTS y ALTER TYPE ADD VALUE IF NOT EXISTS
    de forma idempotente para no romper datos.
    """
    column_statements = [
        "ALTER TABLE convocatorias ADD COLUMN IF NOT EXISTS facultad_id INTEGER REFERENCES facultades(id)",
        "ALTER TABLE convocatorias ADD COLUMN IF NOT EXISTS materia_id INTEGER REFERENCES materias(id)",
        "ALTER TABLE convocatorias ADD COLUMN IF NOT EXISTS semestre VARCHAR(20) NOT NULL DEFAULT '2026-1'",
        "ALTER TABLE convocatorias ADD COLUMN IF NOT EXISTS historial_estados JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE postulaciones ADD COLUMN IF NOT EXISTS historial_estados JSONB NOT NULL DEFAULT '[]'::jsonb",
        "ALTER TABLE postulaciones ADD COLUMN IF NOT EXISTS evaluacion_ia_ultima JSONB",
        "ALTER TABLE postulaciones DROP CONSTRAINT IF EXISTS uq_postulacion_conv_est",
        (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_postulacion_activa "
            "ON postulaciones (convocatoria_id, estudiante_id) "
            "WHERE estado <> 'CANCELADA'"
        ),
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS promedio_acumulado NUMERIC(3,2)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS creditos_aprobados INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS semestre_actual INTEGER",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS contrasena_temporal BOOLEAN NOT NULL DEFAULT FALSE",
        (
            "CREATE INDEX IF NOT EXISTS ix_notificaciones_usuario_leida "
            "ON notificaciones (usuario_id, leida)"
        ),
    ]
    with engine.begin() as conn:
        for stmt in column_statements:
            conn.execute(text(stmt))

    enum_lookup = text(
        """
        SELECT t.typname
        FROM pg_type t
        JOIN pg_attribute a ON a.atttypid = t.oid
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = 'convocatorias' AND a.attname = 'status'
        """
    )
    with engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as conn:
        row = conn.execute(enum_lookup).first()
        if row is not None:
            enum_name = row[0]
            conn.execute(
                text(
                    f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS 'ADJUDICADA'"
                )
            )


def get_session():
    with Session(engine) as session:
        yield session

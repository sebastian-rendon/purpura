import re
import secrets
import string
import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import text as sa_text

from app.services.adjudicacion import (
    AdjudicacionInvalidaError,
    adjudicar_convocatoria,
)
from app.services.evaluacion_ia import evaluar_postulacion
from app.services.notificar import crear_notificacion
from app.services.reportes import (
    exportar_convocatorias_csv,
    exportar_monitores_csv,
    exportar_postulaciones_csv,
    kpis_globales,
    metricas_por_convocatoria,
    metricas_por_facultad,
)
from app.services.transiciones import (
    POST_APROBADA,
    POST_CANCELADA,
    POST_EN_REVISION,
    POST_ENVIADA,
    POST_RECHAZADA,
    PermisoInsuficienteError,
    TransicionInvalidaError,
    transicionar_estado,
    transicionar_postulacion,
    transiciones_disponibles,
)

from app.auth import (
    get_current_user,
    hash_password,  # noqa: F401
    log_audit,
    require_login,
    require_role,
    verify_password,
)
from app.config import get_settings
from app.db import get_session, init_db, run_migrations
from app.models import (
    ConfiguracionIA,
    Convocatoria,
    ConvocatoriaStatus,
    Facultad,
    Materia,
    Notificacion,
    Postulacion,
    User,
    UserRole,
)

settings = get_settings()

app = FastAPI(title="Proyecto PÚRPURA", version="0.1.0")

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET_KEY,
    max_age=8 * 3600,
    same_site="lax",
    https_only=settings.SESSION_COOKIE_SECURE,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _time_ago(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    delta = datetime.utcnow() - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "ahora"
    if secs < 3600:
        m = secs // 60
        return f"hace {m} min"
    if secs < 86400:
        h = secs // 3600
        return f"hace {h} h"
    if secs < 86400 * 7:
        d = secs // 86400
        return f"hace {d} d"
    return dt.strftime("%d %b %Y")


templates.env.filters["time_ago"] = _time_ago


def _notif_ctx_user(session: Session, user: User) -> dict:
    """Carga datos de notificación para el dropdown de la campana.

    Devuelve {notif_count, notif_dropdown} para mergear al template
    context. El count es exacto (no leídas); el dropdown trae las
    últimas 5 no leídas, ordenadas por created_at desc.
    """
    from app.models import Notificacion as _N

    count = len(
        session.exec(
            select(_N).where(_N.usuario_id == user.id).where(_N.leida.is_(False))
        ).all()
    )
    items = session.exec(
        select(_N)
        .where(_N.usuario_id == user.id)
        .where(_N.leida.is_(False))
        .order_by(_N.created_at.desc())
        .limit(5)
    ).all()
    return {
        "notif_count": count,
        "notif_dropdown": [
            {
                "id": n.id,
                "titulo": n.titulo,
                "cuerpo": n.cuerpo,
                "url_destino": n.url_destino,
                "created_at": n.created_at,
            }
            for n in items
        ],
    }


CODIGO_REGEX = re.compile(r"^MON-\d{4}-\d{2}-[A-Z0-9]+$")


def _get_config_ia(session: Session) -> dict:
    cfg = session.exec(select(ConfiguracionIA)).first()
    if cfg is None:
        return {}
    return {
        "modelo_activo": cfg.modelo_activo,
        "umbral_confianza": cfg.umbral_confianza,
        "modo_fallback": cfg.modo_fallback,
    }


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    run_migrations()


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(url=exc.headers["Location"], status_code=303)
    if exc.status_code == 403:
        return templates.TemplateResponse(
            request,
            "base.html",
            {"user": None, "error_403": exc.detail},
            status_code=403,
        )
    raise exc


@app.get("/")
def root(
    request: Request,
    user: Optional[User] = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if user is not None:
        return RedirectResponse("/dashboard", status_code=303)

    rows = session.exec(
        select(Convocatoria, Facultad, Materia)
        .outerjoin(Facultad, Convocatoria.facultad_id == Facultad.id)
        .outerjoin(Materia, Convocatoria.materia_id == Materia.id)
        .where(Convocatoria.status == ConvocatoriaStatus.PUBLICADA)
        .order_by(Convocatoria.fecha_apertura.desc())
        .limit(3)
    ).all()
    convocatorias = [
        {
            "id": c.id,
            "titulo": c.titulo,
            "descripcion": c.descripcion,
            "cupos": c.cupos,
            "facultad": f,
            "materia": m,
            "estado_publico": "ABIERTA",
        }
        for c, f, m in rows
    ]
    return templates.TemplateResponse(
        request,
        "landing.html",
        {"user": None, "convocatorias": convocatorias},
    )


@app.get("/login")
def login_get(
    request: Request,
    rol: str = "estudiante",
    user: Optional[User] = Depends(get_current_user),
):
    if user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    if rol not in ("estudiante", "coordinador", "administrador", "registro"):
        rol = "estudiante"
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "user": None,
            "error": None,
            "rol_preferido": rol,
            "registro_error": None,
            "registro_form": {},
            "panel_recuperar_activo": False,
            "recuperar_error": None,
            "recuperar_mensaje": None,
        },
    )


@app.get("/documentacion")
def documentacion(request: Request):
    """Documentación pública del sistema. Accesible con o sin sesión."""
    return templates.TemplateResponse(
        request,
        "documentacion.html",
        {"user": None},
    )


def _parse_date_opt(s: Optional[str]) -> Optional[date]:
    if not s or not s.strip():
        return None
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        return None


def _filtros_reportes(
    user: User,
    facultad_id: Optional[int],
    desde: Optional[str],
    hasta: Optional[str],
) -> dict:
    return {
        "coord_owner_id": (
            user.id if user.role == UserRole.COORDINADOR else None
        ),
        "facultad_id": facultad_id,
        "desde": _parse_date_opt(desde),
        "hasta": _parse_date_opt(hasta),
    }


def _querystring_reportes(
    facultad_id: Optional[int],
    desde: Optional[str],
    hasta: Optional[str],
) -> str:
    parts = []
    if facultad_id is not None:
        parts.append(f"facultad_id={facultad_id}")
    if desde:
        parts.append(f"desde={desde}")
    if hasta:
        parts.append(f"hasta={hasta}")
    return ("?" + "&".join(parts)) if parts else ""


@app.get("/reportes")
def reportes(
    request: Request,
    facultad_id: Optional[str] = Query(default=None),
    desde: Optional[str] = Query(default=None),
    hasta: Optional[str] = Query(default=None),
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    # Parseo seguro de facultad_id — cadena vacía se trata como "sin filtro"
    fac_id: Optional[int] = None
    if facultad_id and facultad_id.strip():
        try:
            fac_id = int(facultad_id.strip())
        except ValueError:
            pass

    # Validación de rango de fechas
    desde_dt = _parse_date_opt(desde)
    hasta_dt = _parse_date_opt(hasta)
    if desde_dt and hasta_dt and hasta_dt < desde_dt:
        _flash(request, "danger", "La fecha de fin no puede ser anterior a la fecha de inicio.")
        return RedirectResponse("/reportes", status_code=303)

    filtros = _filtros_reportes(user, fac_id, desde, hasta)
    kpis = kpis_globales(session, filtros)
    por_facultad = metricas_por_facultad(session, filtros)
    por_convocatoria = metricas_por_convocatoria(session, filtros)
    facultades = session.exec(select(Facultad).order_by(Facultad.nombre)).all()
    qs = _querystring_reportes(fac_id, desde, hasta)
    ctx = {
        "user": user,
        "kpis": kpis,
        "por_facultad": por_facultad,
        "por_convocatoria": por_convocatoria,
        "facultades": facultades,
        "filtros_form": {
            "facultad_id": fac_id,
            "desde": desde or "",
            "hasta": hasta or "",
        },
        "querystring": qs,
    }
    ctx.update(_notif_ctx_user(session, user))
    return templates.TemplateResponse(request, "reportes.html", ctx)


def _csv_response(contenido: str, nombre_base: str) -> Response:
    """Envuelve el CSV con BOM UTF-8 + Content-Disposition con fecha."""
    fecha = date.today().isoformat()
    filename = f"{nombre_base}_{fecha}.csv"
    return Response(
        content="﻿" + contenido,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/reportes/csv/postulaciones")
def reportes_csv_postulaciones(
    facultad_id: Optional[int] = Query(default=None),
    desde: Optional[str] = Query(default=None),
    hasta: Optional[str] = Query(default=None),
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    filtros = _filtros_reportes(user, facultad_id, desde, hasta)
    contenido = exportar_postulaciones_csv(session, filtros)
    return _csv_response(contenido, "postulaciones")


@app.get("/reportes/csv/convocatorias")
def reportes_csv_convocatorias(
    facultad_id: Optional[int] = Query(default=None),
    desde: Optional[str] = Query(default=None),
    hasta: Optional[str] = Query(default=None),
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    filtros = _filtros_reportes(user, facultad_id, desde, hasta)
    contenido = exportar_convocatorias_csv(session, filtros)
    return _csv_response(contenido, "convocatorias")


@app.get("/reportes/csv/monitores")
def reportes_csv_monitores(
    facultad_id: Optional[int] = Query(default=None),
    desde: Optional[str] = Query(default=None),
    hasta: Optional[str] = Query(default=None),
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    filtros = _filtros_reportes(user, facultad_id, desde, hasta)
    contenido = exportar_monitores_csv(session, filtros)
    return _csv_response(contenido, "monitores")


@app.post("/login")
def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    rol_preferido: str = Form(default="estudiante"),
    session: Session = Depends(get_session),
):
    _ROLES_LOGIN = {"estudiante", "coordinador", "administrador"}
    rol_limpio = rol_preferido if rol_preferido in _ROLES_LOGIN else "estudiante"

    def render_login_error(msg: str):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "user": None,
                "error": msg,
                "rol_preferido": rol_limpio,
                "registro_error": None,
                "registro_form": {},
                "panel_recuperar_activo": False,
                "recuperar_error": None,
                "recuperar_mensaje": None,
            },
            status_code=200,
        )

    user = session.exec(
        select(User).where(User.email == email.strip().lower())
    ).first()
    if (
        user is None
        or not user.is_active
        or not verify_password(password, user.password_hash)
    ):
        return render_login_error("Credenciales inválidas.")

    if user.role.value != rol_limpio:
        return render_login_error(
            "El correo ingresado no corresponde al perfil seleccionado."
        )

    request.session["user_id"] = str(user.id)
    request.session["email"] = user.email
    request.session["nombre_completo"] = user.full_name
    request.session["rol"] = user.role.value
    user.last_login_at = datetime.utcnow()
    session.add(user)
    session.commit()
    log_audit(session, user_id=user.id, action="LOGIN", request=request)
    if user.contrasena_temporal:
        return RedirectResponse("/cambiar-contrasena", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/registro")
def registro_post(
    request: Request,
    nombre_completo: str = Form(...),
    email: str = Form(...),
    rol: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    session: Session = Depends(get_session),
):
    form_data = {
        "nombre_completo": nombre_completo,
        "email": email,
        "rol": rol,
    }

    def render_registro_error(msg: str):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "user": None,
                "error": None,
                "rol_preferido": "registro",
                "registro_error": msg,
                "registro_form": form_data,
                "panel_recuperar_activo": False,
                "recuperar_error": None,
                "recuperar_mensaje": None,
            },
            status_code=200,
        )

    if rol not in ("estudiante", "coordinador"):
        return render_registro_error("Perfil no válido.")

    if password != password2:
        return render_registro_error("Las contraseñas no coinciden.")

    if len(password) < 8:
        return render_registro_error("La contraseña debe tener al menos 8 caracteres.")

    email_limpio = email.strip().lower()
    if session.exec(select(User).where(User.email == email_limpio)).first():
        return render_registro_error("Ya existe una cuenta con ese correo.")

    nuevo_usuario = User(
        email=email_limpio,
        password_hash=hash_password(password),
        full_name=nombre_completo.strip(),
        role=UserRole(rol),
        is_active=True,
    )
    session.add(nuevo_usuario)
    session.commit()

    _flash(request, "success", "Cuenta creada. Ya puedes iniciar sesión.")
    return RedirectResponse("/login", status_code=303)


_LOGIN_CONTEXT_BASE = {
    "user": None,
    "error": None,
    "rol_preferido": "estudiante",
    "registro_error": None,
    "registro_form": {},
    "panel_recuperar_activo": True,
}


@app.post("/recuperar-contrasena")
def recuperar_contrasena_post(
    request: Request,
    email: str = Form(...),
    session: Session = Depends(get_session),
):
    email_limpio = email.strip().lower()
    user = session.exec(select(User).where(User.email == email_limpio)).first()

    ctx = dict(_LOGIN_CONTEXT_BASE)

    if user is None or not user.is_active:
        ctx["recuperar_error"] = "No encontramos una cuenta con ese correo."
        ctx["recuperar_mensaje"] = None
        return templates.TemplateResponse(request, "login.html", ctx, status_code=200)

    alphabet = string.ascii_letters + string.digits
    nueva_password = "".join(secrets.choice(alphabet) for _ in range(10))
    user.password_hash = hash_password(nueva_password)
    user.contrasena_temporal = True
    user.updated_at = datetime.utcnow()
    session.add(user)
    session.commit()

    ctx["recuperar_error"] = None
    ctx["recuperar_mensaje"] = (
        f"Tu nueva contraseña temporal es: {nueva_password}"
        " — Cámbiala después de iniciar sesión."
    )
    return templates.TemplateResponse(request, "login.html", ctx, status_code=200)


@app.get("/cambiar-contrasena")
def cambiar_contrasena_get(
    request: Request,
    user: User = Depends(require_login),
):
    return templates.TemplateResponse(
        request,
        "cambiar_contrasena.html",
        {"user": user, "error": None},
    )


@app.post("/cambiar-contrasena")
def cambiar_contrasena_post(
    request: Request,
    nueva_contrasena: str = Form(...),
    confirmar_contrasena: str = Form(...),
    user: User = Depends(require_login),
    session: Session = Depends(get_session),
):
    def render_error(msg: str):
        return templates.TemplateResponse(
            request,
            "cambiar_contrasena.html",
            {"user": user, "error": msg},
            status_code=200,
        )

    if nueva_contrasena != confirmar_contrasena:
        return render_error("Las contraseñas no coinciden.")
    if len(nueva_contrasena) < 8:
        return render_error("La contraseña debe tener al menos 8 caracteres.")

    user.password_hash = hash_password(nueva_contrasena)
    user.contrasena_temporal = False
    user.updated_at = datetime.utcnow()
    session.add(user)
    session.commit()

    log_audit(
        session,
        user_id=user.id,
        action="CAMBIAR_CONTRASENA",
        request=request,
        entity_type="user",
        entity_id=user.id,
    )
    _flash(request, "success", "Contraseña actualizada correctamente.")
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/logout")
def logout(
    request: Request,
    user: Optional[User] = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if user is not None:
        log_audit(session, user_id=user.id, action="LOGOUT", request=request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/dashboard")
def dashboard(
    request: Request,
    user: User = Depends(require_login),
    session: Session = Depends(get_session),
):
    ctx = {"user": user}
    ctx.update(_notif_ctx_user(session, user))
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/perfil")
def perfil_get(
    request: Request,
    user: User = Depends(require_role(UserRole.ESTUDIANTE)),
):
    form = {
        "promedio_acumulado": (
            "" if user.promedio_acumulado is None
            else f"{user.promedio_acumulado:g}"
        ),
        "creditos_aprobados": (
            "" if user.creditos_aprobados is None else str(user.creditos_aprobados)
        ),
        "semestre_actual": (
            "" if user.semestre_actual is None else str(user.semestre_actual)
        ),
    }
    return templates.TemplateResponse(
        request,
        "perfil.html",
        {"user": user, "form": form, "error": None},
    )


@app.post("/perfil")
def perfil_post(
    request: Request,
    promedio_acumulado: Optional[str] = Form(None),
    creditos_aprobados: Optional[str] = Form(None),
    semestre_actual: Optional[str] = Form(None),
    user: User = Depends(require_role(UserRole.ESTUDIANTE)),
    session: Session = Depends(get_session),
):
    form_data = {
        "promedio_acumulado": promedio_acumulado or "",
        "creditos_aprobados": creditos_aprobados or "",
        "semestre_actual": semestre_actual or "",
    }

    def render_error(msg: str):
        return templates.TemplateResponse(
            request,
            "perfil.html",
            {"user": user, "form": form_data, "error": msg},
            status_code=200,
        )

    def _parse_int(value: Optional[str]) -> Optional[int]:
        if value is None or value.strip() == "":
            return None
        return int(value.strip())

    def _parse_float(value: Optional[str]) -> Optional[float]:
        if value is None or value.strip() == "":
            return None
        return float(value.strip())

    try:
        promedio_val = _parse_float(promedio_acumulado)
        creditos_val = _parse_int(creditos_aprobados)
        semestre_val = _parse_int(semestre_actual)
    except (ValueError, TypeError):
        return render_error("Algún valor no es un número válido.")

    if promedio_val is not None and not (0.0 <= promedio_val <= 5.0):
        return render_error("El promedio debe estar entre 0.0 y 5.0.")
    if creditos_val is not None and creditos_val < 0:
        return render_error("Los créditos aprobados no pueden ser negativos.")
    if semestre_val is not None and not (1 <= semestre_val <= 12):
        return render_error("El semestre debe estar entre 1 y 12.")

    user.promedio_acumulado = promedio_val
    user.creditos_aprobados = creditos_val
    user.semestre_actual = semestre_val
    user.updated_at = datetime.utcnow()
    session.add(user)
    session.commit()

    log_audit(
        session,
        user_id=user.id,
        action="ACTUALIZAR_PERFIL",
        request=request,
        entity_type="user",
        entity_id=user.id,
        payload={
            "promedio": promedio_val,
            "creditos": creditos_val,
            "semestre": semestre_val,
        },
    )
    _flash(request, "success", "Datos académicos actualizados.")
    return RedirectResponse("/perfil", status_code=303)


@app.get("/convocatorias")
def convocatorias_list(
    request: Request,
    user: User = Depends(require_login),
    session: Session = Depends(get_session),
):
    stmt = (
        select(Convocatoria, User, Facultad, Materia)
        .join(User, Convocatoria.created_by == User.id)
        .outerjoin(Facultad, Convocatoria.facultad_id == Facultad.id)
        .outerjoin(Materia, Convocatoria.materia_id == Materia.id)
    )
    if user.role == UserRole.ESTUDIANTE:
        stmt = stmt.where(Convocatoria.status == ConvocatoriaStatus.PUBLICADA)
    else:
        stmt = stmt.where(Convocatoria.status != ConvocatoriaStatus.ARCHIVADA)

    rows = session.exec(stmt).all()
    convocatorias = [
        {
            "id": conv.id,
            "codigo": conv.codigo,
            "titulo": conv.titulo,
            "facultad": facultad_obj,
            "materia": materia_obj,
            "asignatura": conv.asignatura,
            "cupos": conv.cupos,
            "semestre": conv.semestre,
            "fecha_apertura": conv.fecha_apertura,
            "fecha_cierre": conv.fecha_cierre,
            "status": conv.status.value if conv.status else None,
            "created_by_full_name": creator.full_name,
            "created_by_id": conv.created_by,
            "puede_editar": (
                conv.status == ConvocatoriaStatus.BORRADOR
                and (
                    user.role == UserRole.ADMINISTRADOR
                    or (
                        user.role == UserRole.COORDINADOR
                        and conv.created_by == user.id
                    )
                )
            ),
        }
        for conv, creator, facultad_obj, materia_obj in rows
    ]
    return templates.TemplateResponse(
        request,
        "convocatorias_list.html",
        {"user": user, "convocatorias": convocatorias},
    )


@app.get("/convocatorias/crear")
def convocatorias_crear_get(
    request: Request,
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    facultades = session.exec(select(Facultad).order_by(Facultad.nombre)).all()
    return templates.TemplateResponse(
        request,
        "convocatorias_form.html",
        {"user": user, "error": None, "form": {}, "facultades": facultades},
    )


def _parse_optional_number(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    return float(v)


@app.post("/convocatorias/crear")
def convocatorias_crear_post(
    request: Request,
    codigo: str = Form(default=""),
    titulo: str = Form(default=""),
    descripcion: Optional[str] = Form(None),
    facultad_id: str = Form(default=""),
    asignatura: str = Form(default=""),
    cupos: str = Form(default=""),
    fecha_apertura: str = Form(default=""),
    fecha_cierre: str = Form(default=""),
    promedio_minimo: str = Form(default=""),
    creditos_minimos: str = Form(default=""),
    semestre_minimo: str = Form(default=""),
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    facultades = session.exec(select(Facultad).order_by(Facultad.nombre)).all()
    form_data = {
        "codigo": codigo,
        "titulo": titulo,
        "descripcion": descripcion or "",
        "facultad_id": facultad_id,
        "asignatura": asignatura,
        "cupos": cupos,
        "fecha_apertura": fecha_apertura,
        "fecha_cierre": fecha_cierre,
        "promedio_minimo": promedio_minimo,
        "creditos_minimos": creditos_minimos,
        "semestre_minimo": semestre_minimo,
    }

    def render_error(msg: str):
        return templates.TemplateResponse(
            request,
            "convocatorias_form.html",
            {
                "user": user,
                "error": msg,
                "form": form_data,
                "facultades": facultades,
            },
            status_code=200,
        )

    # Validación de campos de texto obligatorios
    if not titulo.strip():
        return render_error("El campo 'título' es obligatorio.")
    if not asignatura.strip():
        return render_error("El campo 'asignatura' es obligatorio.")
    if not fecha_apertura.strip():
        return render_error("La fecha de apertura es obligatoria.")
    if not fecha_cierre.strip():
        return render_error("La fecha de cierre es obligatoria.")

    codigo_norm = codigo.strip().upper()
    if not CODIGO_REGEX.match(codigo_norm):
        return render_error(
            "El código debe seguir el formato MON-AAAA-NN-XXXX (ej. MON-2026-01-CALC1)."
        )

    # Cupos — parsing manual para evitar 422 JSON
    try:
        cupos_int = int(cupos.strip())
        if cupos_int <= 0:
            return render_error("Los cupos deben ser mayores a 0.")
    except (ValueError, AttributeError):
        return render_error("El campo 'cupos' debe ser un número entero mayor que 0.")

    # Facultad — lookup por ID
    if not facultad_id.strip():
        return render_error("Debes seleccionar una facultad.")
    try:
        fac_id_int = int(facultad_id.strip())
    except ValueError:
        return render_error("Facultad no válida.")
    fac_obj = session.exec(select(Facultad).where(Facultad.id == fac_id_int)).first()
    if fac_obj is None:
        return render_error("La facultad seleccionada no existe.")

    try:
        apertura_dt = datetime.fromisoformat(fecha_apertura)
        cierre_dt = datetime.fromisoformat(fecha_cierre)
    except ValueError:
        return render_error("Las fechas tienen un formato inválido.")

    if cierre_dt <= apertura_dt:
        return render_error(
            "La fecha de cierre debe ser posterior a la fecha de apertura."
        )

    existing = session.exec(
        select(Convocatoria).where(Convocatoria.codigo == codigo_norm)
    ).first()
    if existing is not None:
        return render_error(f"Ya existe una convocatoria con el código {codigo_norm}.")

    # Requisitos obligatorios
    try:
        prom = _parse_optional_number(promedio_minimo)
        cred = _parse_optional_number(creditos_minimos)
        sem = _parse_optional_number(semestre_minimo)
    except ValueError:
        return render_error("Los requisitos numéricos no son válidos.")

    if prom is None:
        return render_error("El promedio mínimo es obligatorio.")
    if cred is None:
        return render_error("Los créditos mínimos son obligatorios.")
    if sem is None:
        return render_error("El semestre mínimo es obligatorio.")

    requisitos = {
        "promedio_minimo": prom,
        "creditos_minimos": int(cred),
        "semestre_minimo": int(sem),
    }

    conv = Convocatoria(
        codigo=codigo_norm,
        titulo=titulo.strip(),
        descripcion=(descripcion.strip() if descripcion else None),
        facultad=fac_obj.nombre,
        facultad_id=fac_id_int,
        asignatura=asignatura.strip(),
        cupos=cupos_int,
        fecha_apertura=apertura_dt,
        fecha_cierre=cierre_dt,
        requisitos=requisitos,
        status=ConvocatoriaStatus.BORRADOR,
        created_by=user.id,
    )
    session.add(conv)
    session.commit()
    session.refresh(conv)

    log_audit(
        session,
        user_id=user.id,
        action="CREATE_CONVOCATORIA",
        request=request,
        entity_type="convocatoria",
        entity_id=conv.id,
        payload={"codigo": conv.codigo, "titulo": conv.titulo},
    )

    return RedirectResponse("/convocatorias", status_code=303)


def _flash(request: Request, level: str, msg: str) -> None:
    """Apila un mensaje flash en la sesión para que base.html lo renderice."""
    bag = request.session.get("flash", [])
    bag.append([level, msg])
    request.session["flash"] = bag


def _load_convocatoria_or_404(
    session: Session, conv_id: uuid.UUID
) -> tuple[Convocatoria, User, Optional[Facultad], Optional[Materia]]:
    row = session.exec(
        select(Convocatoria, User, Facultad, Materia)
        .join(User, Convocatoria.created_by == User.id)
        .outerjoin(Facultad, Convocatoria.facultad_id == Facultad.id)
        .outerjoin(Materia, Convocatoria.materia_id == Materia.id)
        .where(Convocatoria.id == conv_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Convocatoria no encontrada")
    return row


@app.get("/convocatorias/archivadas")
def convocatorias_archivadas(
    request: Request,
    user: User = Depends(require_role(UserRole.ADMINISTRADOR)),
    session: Session = Depends(get_session),
):
    rows = session.exec(
        select(Convocatoria, User, Facultad, Materia)
        .join(User, Convocatoria.created_by == User.id)
        .outerjoin(Facultad, Convocatoria.facultad_id == Facultad.id)
        .outerjoin(Materia, Convocatoria.materia_id == Materia.id)
        .where(Convocatoria.status == ConvocatoriaStatus.ARCHIVADA)
        .order_by(Convocatoria.updated_at.desc())
    ).all()
    convocatorias = [
        {
            "id": conv.id,
            "codigo": conv.codigo,
            "titulo": conv.titulo,
            "facultad": facultad_obj,
            "semestre": conv.semestre,
            "status": conv.status.value,
            "updated_at": conv.updated_at,
            "created_by_full_name": creator.full_name,
        }
        for conv, creator, facultad_obj, materia_obj in rows
    ]
    return templates.TemplateResponse(
        request,
        "convocatorias_archivadas.html",
        {"user": user, "convocatorias": convocatorias},
    )


@app.get("/convocatorias/{conv_id}")
def convocatorias_detalle(
    request: Request,
    conv_id: uuid.UUID,
    user: User = Depends(require_login),
    session: Session = Depends(get_session),
):
    conv, creator, facultad_obj, materia_obj = _load_convocatoria_or_404(
        session, conv_id
    )
    transiciones = transiciones_disponibles(conv, user)
    puede_editar = (
        conv.status == ConvocatoriaStatus.BORRADOR
        and (
            user.role == UserRole.ADMINISTRADOR
            or (
                user.role == UserRole.COORDINADOR
                and conv.created_by == user.id
            )
        )
    )

    monitores_info = None
    if conv.status == ConvocatoriaStatus.ADJUDICADA:
        from app.models import Monitor

        monitor_rows = session.exec(
            select(Monitor, User)
            .join(User, Monitor.estudiante_id == User.id)
            .where(Monitor.convocatoria_id == conv.id)
            .order_by(Monitor.fecha_adjudicacion.desc())
        ).all()
        total = len(monitor_rows)
        if user.role in (UserRole.ADMINISTRADOR, UserRole.COORDINADOR):
            monitores_info = {
                "total": total,
                "lista": [
                    {
                        "nombre": est.full_name or est.email,
                        "email": est.email,
                        "fecha": m.fecha_adjudicacion,
                        "postulacion_id": m.postulacion_id,
                    }
                    for m, est in monitor_rows
                ],
            }
        else:
            monitores_info = {"total": total, "lista": None}

    postulacion_activa: Optional[Postulacion] = None
    postulacion_historica: Optional[Postulacion] = None
    puede_postular = False
    puede_cancelar_postulacion = False
    if user.role == UserRole.ESTUDIANTE:
        postulacion_activa = session.exec(
            select(Postulacion)
            .where(Postulacion.convocatoria_id == conv.id)
            .where(Postulacion.estudiante_id == user.id)
            .where(Postulacion.estado != POST_CANCELADA)
        ).first()
        if postulacion_activa is None:
            postulacion_historica = session.exec(
                select(Postulacion)
                .where(Postulacion.convocatoria_id == conv.id)
                .where(Postulacion.estudiante_id == user.id)
                .order_by(Postulacion.created_at.desc())
            ).first()
        if conv.status == ConvocatoriaStatus.PUBLICADA:
            if postulacion_activa is None:
                puede_postular = True
            elif postulacion_activa.estado in (POST_ENVIADA, POST_EN_REVISION):
                puede_cancelar_postulacion = True

    puede_adjudicar = conv.status == ConvocatoriaStatus.CERRADA and (
        user.role == UserRole.ADMINISTRADOR
        or (user.role == UserRole.COORDINADOR and conv.created_by == user.id)
    )

    return templates.TemplateResponse(
        request,
        "convocatorias_detalle.html",
        {
            "user": user,
            "convocatoria": conv,
            "facultad": facultad_obj,
            "materia": materia_obj,
            "creator": creator,
            "transiciones": transiciones,
            "puede_editar": puede_editar,
            "puede_adjudicar": puede_adjudicar,
            "postulacion_activa": postulacion_activa,
            "postulacion_historica": postulacion_historica,
            "puede_postular": puede_postular,
            "puede_cancelar_postulacion": puede_cancelar_postulacion,
            "monitores_info": monitores_info,
        },
    )


@app.get("/convocatorias/{conv_id}/editar")
def convocatorias_editar_get(
    request: Request,
    conv_id: uuid.UUID,
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    conv, _creator, facultad_obj, materia_obj = _load_convocatoria_or_404(
        session, conv_id
    )
    if conv.status != ConvocatoriaStatus.BORRADOR:
        _flash(
            request,
            "warning",
            "Solo se pueden editar convocatorias en estado BORRADOR.",
        )
        return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)
    if user.role == UserRole.COORDINADOR and conv.created_by != user.id:
        raise HTTPException(
            status_code=403,
            detail="Solo el coordinador que creó la convocatoria puede editarla.",
        )
    facultades = session.exec(select(Facultad).order_by(Facultad.nombre)).all()
    form = {
        "titulo": conv.titulo,
        "descripcion": conv.descripcion or "",
        "facultad_id": conv.facultad_id,
        "asignatura": conv.asignatura,
        "cupos": conv.cupos,
        "fecha_apertura": conv.fecha_apertura.strftime("%Y-%m-%dT%H:%M"),
        "fecha_cierre": conv.fecha_cierre.strftime("%Y-%m-%dT%H:%M"),
        "promedio_minimo": str(conv.requisitos.get("promedio_minimo", "")),
        "creditos_minimos": str(conv.requisitos.get("creditos_minimos", "")),
        "semestre_minimo": str(conv.requisitos.get("semestre_minimo", "")),
    }
    return templates.TemplateResponse(
        request,
        "convocatorias_editar.html",
        {
            "user": user,
            "convocatoria": conv,
            "form": form,
            "error": None,
            "facultades": facultades,
        },
    )


@app.post("/convocatorias/{conv_id}/editar")
def convocatorias_editar_post(
    request: Request,
    conv_id: uuid.UUID,
    titulo: str = Form(default=""),
    descripcion: Optional[str] = Form(None),
    facultad_id: str = Form(default=""),
    asignatura: str = Form(default=""),
    cupos: str = Form(default=""),
    fecha_apertura: str = Form(default=""),
    fecha_cierre: str = Form(default=""),
    promedio_minimo: str = Form(default=""),
    creditos_minimos: str = Form(default=""),
    semestre_minimo: str = Form(default=""),
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    conv, _creator, _fac, _mat = _load_convocatoria_or_404(session, conv_id)
    if conv.status != ConvocatoriaStatus.BORRADOR:
        _flash(
            request,
            "warning",
            "Solo se pueden editar convocatorias en estado BORRADOR.",
        )
        return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)
    if user.role == UserRole.COORDINADOR and conv.created_by != user.id:
        raise HTTPException(
            status_code=403,
            detail="Solo el coordinador que creó la convocatoria puede editarla.",
        )

    facultades = session.exec(select(Facultad).order_by(Facultad.nombre)).all()
    form_data = {
        "titulo": titulo,
        "descripcion": descripcion or "",
        "facultad_id": facultad_id,
        "asignatura": asignatura,
        "cupos": cupos,
        "fecha_apertura": fecha_apertura,
        "fecha_cierre": fecha_cierre,
        "promedio_minimo": promedio_minimo,
        "creditos_minimos": creditos_minimos,
        "semestre_minimo": semestre_minimo,
    }

    def render_error(msg: str):
        return templates.TemplateResponse(
            request,
            "convocatorias_editar.html",
            {
                "user": user,
                "convocatoria": conv,
                "form": form_data,
                "error": msg,
                "facultades": facultades,
            },
            status_code=200,
        )

    # Validación de campos de texto obligatorios
    if not titulo.strip():
        return render_error("El campo 'título' es obligatorio.")
    if not asignatura.strip():
        return render_error("El campo 'asignatura' es obligatorio.")
    if not fecha_apertura.strip():
        return render_error("La fecha de apertura es obligatoria.")
    if not fecha_cierre.strip():
        return render_error("La fecha de cierre es obligatoria.")

    # Cupos — parsing manual para evitar 422 JSON
    try:
        cupos_int = int(cupos.strip())
        if cupos_int <= 0:
            return render_error("Los cupos deben ser mayores a 0.")
    except (ValueError, AttributeError):
        return render_error("El campo 'cupos' debe ser un número entero mayor que 0.")

    # Facultad — lookup por ID
    if not facultad_id.strip():
        return render_error("Debes seleccionar una facultad.")
    try:
        fac_id_int = int(facultad_id.strip())
    except ValueError:
        return render_error("Facultad no válida.")
    fac_obj = session.exec(select(Facultad).where(Facultad.id == fac_id_int)).first()
    if fac_obj is None:
        return render_error("La facultad seleccionada no existe.")

    try:
        apertura_dt = datetime.fromisoformat(fecha_apertura)
        cierre_dt = datetime.fromisoformat(fecha_cierre)
    except ValueError:
        return render_error("Las fechas tienen un formato inválido.")
    if cierre_dt <= apertura_dt:
        return render_error(
            "La fecha de cierre debe ser posterior a la fecha de apertura."
        )

    # Requisitos obligatorios
    try:
        prom = _parse_optional_number(promedio_minimo)
        cred = _parse_optional_number(creditos_minimos)
        sem = _parse_optional_number(semestre_minimo)
    except ValueError:
        return render_error("Los requisitos numéricos no son válidos.")

    if prom is None:
        return render_error("El promedio mínimo es obligatorio.")
    if cred is None:
        return render_error("Los créditos mínimos son obligatorios.")
    if sem is None:
        return render_error("El semestre mínimo es obligatorio.")

    conv.titulo = titulo.strip()
    conv.descripcion = descripcion.strip() if descripcion else None
    conv.facultad = fac_obj.nombre
    conv.facultad_id = fac_id_int
    conv.asignatura = asignatura.strip()
    conv.cupos = cupos_int
    conv.fecha_apertura = apertura_dt
    conv.fecha_cierre = cierre_dt
    conv.requisitos = {
        "promedio_minimo": prom,
        "creditos_minimos": int(cred),
        "semestre_minimo": int(sem),
    }
    conv.updated_at = datetime.utcnow()
    session.add(conv)
    session.commit()
    log_audit(
        session,
        user_id=user.id,
        action="EDIT_CONVOCATORIA",
        request=request,
        entity_type="convocatoria",
        entity_id=conv.id,
        payload={"codigo": conv.codigo},
    )
    _flash(request, "success", f"Convocatoria {conv.codigo} actualizada.")
    return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)


@app.post("/convocatorias/{conv_id}/transicionar")
def convocatorias_transicionar(
    request: Request,
    conv_id: uuid.UUID,
    nuevo_estado: str = Form(...),
    motivo: str = Form(""),
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    conv, _creator, _f, _m = _load_convocatoria_or_404(session, conv_id)
    try:
        nuevo = ConvocatoriaStatus(nuevo_estado.lower())
    except ValueError:
        _flash(request, "danger", f"Estado '{nuevo_estado}' no es válido.")
        return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)

    accion_audit = f"TRANSICION_{conv.status.value.upper()}_TO_{nuevo.value.upper()}"
    estado_origen = conv.status.value
    try:
        transicionar_estado(conv, nuevo, user, motivo=motivo)
    except TransicionInvalidaError as e:
        _flash(request, "danger", f"Transición no permitida: {e}")
        return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)
    except PermisoInsuficienteError as e:
        _flash(request, "danger", f"Permiso insuficiente: {e}")
        return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)

    session.add(conv)
    session.commit()
    log_audit(
        session,
        user_id=user.id,
        action=accion_audit,
        request=request,
        entity_type="convocatoria",
        entity_id=conv.id,
        payload={
            "from": estado_origen,
            "to": nuevo.value,
            "motivo": motivo,
        },
    )
    _flash(
        request,
        "success",
        f"Convocatoria {conv.codigo}: {estado_origen} → {nuevo.value}.",
    )
    return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)


# ============================================================
# Postulaciones (P03)
# ============================================================

_MOTIVACION_DEFAULT = (
    "Postulación enviada desde el detalle de la convocatoria. "
    "El estudiante aceptó los requisitos y solicita revisión."
)


def _registrar_evaluacion_ia(
    postulacion: Postulacion,
    convocatoria: Convocatoria,
    estudiante: User,
    user: User,
    trigger: str,
    session: Optional[Session] = None,
    forzar_reevaluacion: bool = False,
) -> dict:
    """Ejecuta evaluación y persiste en el objeto. Caller hace commit.

    - trigger='postular': primera evaluación, siempre llama a Gemini.
    - trigger='iniciar_revision': usa caché si existe resultado válido.
    - trigger='reevaluar': forzar_reevaluacion=True, siempre llama a Gemini.
    """
    config = _get_config_ia(session) if session is not None else {}
    resultado = evaluar_postulacion(
        postulacion, convocatoria, estudiante,
        config=config,
        forzar_reevaluacion=forzar_reevaluacion,
    )
    postulacion.evaluacion_ia_ultima = resultado
    historial = list(postulacion.historial_estados or [])
    historial.append(
        {
            "tipo": "evaluacion_ia",
            "trigger": trigger,
            "by_user_id": str(user.id),
            "by_email": user.email,
            **resultado,
        }
    )
    postulacion.historial_estados = historial
    return resultado


@app.post("/convocatorias/{conv_id}/postular")
def postular_post(
    request: Request,
    conv_id: uuid.UUID,
    motivacion: str = Form(default=_MOTIVACION_DEFAULT),
    user: User = Depends(require_role(UserRole.ESTUDIANTE)),
    session: Session = Depends(get_session),
):
    conv = session.exec(
        select(Convocatoria).where(Convocatoria.id == conv_id)
    ).first()
    if conv is None:
        raise HTTPException(status_code=404, detail="Convocatoria no encontrada")
    if conv.status != ConvocatoriaStatus.PUBLICADA:
        _flash(
            request,
            "danger",
            "Solo se puede postular a convocatorias PUBLICADAS.",
        )
        return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)

    existing = session.exec(
        select(Postulacion)
        .where(Postulacion.convocatoria_id == conv.id)
        .where(Postulacion.estudiante_id == user.id)
        .where(Postulacion.estado != POST_CANCELADA)
    ).first()
    if existing is not None:
        _flash(
            request,
            "warning",
            f"Ya tienes una postulación activa para {conv.codigo} (estado: {existing.estado}).",
        )
        return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)

    motivacion_final = (motivacion or "").strip() or _MOTIVACION_DEFAULT

    historial_inicial = [
        {
            "from": "(creación)",
            "to": POST_ENVIADA,
            "by_user_id": str(user.id),
            "by_email": user.email,
            "motivo": "Postulación creada",
            "at": datetime.utcnow().isoformat(),
        },
        {
            "tipo": "validacion_inicial",
            "promedio_validado": False,
            "creditos_validados": False,
            "razon": "datos_academicos_incompletos_en_modelo_user",
            "manual_review": True,
            "at": datetime.utcnow().isoformat(),
        },
    ]

    promedio_para_postulacion = float(user.promedio_acumulado or 0.0)
    if user.promedio_acumulado is not None:
        justificacion_auto = (
            f"Datos académicos disponibles en perfil del estudiante "
            f"(promedio {user.promedio_acumulado})."
        )
    else:
        justificacion_auto = (
            "Datos académicos del estudiante no disponibles en perfil; "
            "requiere revisión manual."
        )
    postulacion = Postulacion(
        convocatoria_id=conv.id,
        estudiante_id=user.id,
        promedio_acumulado=promedio_para_postulacion,
        motivacion=motivacion_final,
        decision_automatica="REVISAR",
        justificacion_automatica=justificacion_auto,
        estado=POST_ENVIADA,
        historial_estados=historial_inicial,
    )
    session.add(postulacion)
    session.flush()

    _registrar_evaluacion_ia(postulacion, conv, user, user, trigger="postular", session=session, forzar_reevaluacion=True)

    notif = Notificacion(
        usuario_id=conv.created_by,
        titulo=f"Nueva postulación en {conv.codigo}",
        cuerpo=(
            f"{user.full_name} ({user.email}) postuló a tu convocatoria "
            f"{conv.titulo}. Requiere revisión manual de requisitos."
        ),
        url_destino=f"/convocatorias/{conv.id}",
        leida=False,
    )
    session.add(notif)
    session.commit()
    session.refresh(postulacion)

    log_audit(
        session,
        user_id=user.id,
        action="POSTULAR",
        request=request,
        entity_type="postulacion",
        entity_id=None,
        payload={"postulacion_id": postulacion.id, "convocatoria": conv.codigo},
    )
    _flash(
        request,
        "success",
        f"Postulación enviada a {conv.codigo}. Te avisaremos cuando haya revisión.",
    )
    return RedirectResponse("/mis-postulaciones", status_code=303)


@app.get("/mis-postulaciones")
def mis_postulaciones(
    request: Request,
    user: User = Depends(require_role(UserRole.ESTUDIANTE)),
    session: Session = Depends(get_session),
):
    rows = session.exec(
        select(Postulacion, Convocatoria, Facultad)
        .join(Convocatoria, Postulacion.convocatoria_id == Convocatoria.id)
        .outerjoin(Facultad, Convocatoria.facultad_id == Facultad.id)
        .where(Postulacion.estudiante_id == user.id)
        .order_by(Postulacion.created_at.desc())
    ).all()
    postulaciones = [
        {
            "id": post.id,
            "estado": post.estado,
            "created_at": post.created_at,
            "updated_at": post.updated_at,
            "convocatoria_id": conv.id,
            "convocatoria_codigo": conv.codigo,
            "convocatoria_titulo": conv.titulo,
            "convocatoria_status": conv.status.value,
            "facultad": facultad_obj,
            "puede_cancelar": (
                post.estado in (POST_ENVIADA, POST_EN_REVISION)
                and conv.status == ConvocatoriaStatus.PUBLICADA
            ),
        }
        for post, conv, facultad_obj in rows
    ]
    ctx = {"user": user, "postulaciones": postulaciones}
    ctx.update(_notif_ctx_user(session, user))
    return templates.TemplateResponse(request, "mis_postulaciones.html", ctx)


@app.post("/postulaciones/{post_id}/cancelar")
def postulacion_cancelar(
    request: Request,
    post_id: int,
    user: User = Depends(require_role(UserRole.ESTUDIANTE)),
    session: Session = Depends(get_session),
):
    postulacion = session.exec(
        select(Postulacion).where(Postulacion.id == post_id)
    ).first()
    if postulacion is None:
        raise HTTPException(status_code=404, detail="Postulación no encontrada")
    if postulacion.estudiante_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Solo puedes cancelar tus propias postulaciones.",
        )
    conv = session.exec(
        select(Convocatoria).where(Convocatoria.id == postulacion.convocatoria_id)
    ).first()
    if conv is None:
        raise HTTPException(status_code=404, detail="Convocatoria no encontrada")
    if conv.status != ConvocatoriaStatus.PUBLICADA:
        _flash(
            request,
            "warning",
            "Solo se puede cancelar mientras la convocatoria sigue PUBLICADA.",
        )
        return RedirectResponse("/mis-postulaciones", status_code=303)

    try:
        transicionar_postulacion(
            postulacion,
            POST_CANCELADA,
            user,
            conv,
            motivo="cancelación voluntaria del estudiante",
        )
    except (TransicionInvalidaError, PermisoInsuficienteError) as e:
        _flash(request, "danger", str(e))
        return RedirectResponse("/mis-postulaciones", status_code=303)

    session.add(postulacion)
    session.commit()
    log_audit(
        session,
        user_id=user.id,
        action="POSTULACION_CANCELAR",
        request=request,
        entity_type="postulacion",
        entity_id=None,
        payload={"postulacion_id": postulacion.id, "convocatoria": conv.codigo},
    )
    _flash(
        request,
        "success",
        f"Postulación a {conv.codigo} cancelada.",
    )
    return RedirectResponse("/mis-postulaciones", status_code=303)


# ============================================================
# Bandeja del coordinador (P04)
# ============================================================


def _puede_ver_postulacion(user: User, conv: Convocatoria) -> bool:
    """Admin ve todas. Coord ve solo las de convocatorias que creó."""
    if user.role == UserRole.ADMINISTRADOR:
        return True
    if user.role == UserRole.COORDINADOR:
        return conv.created_by == user.id
    return False


@app.get("/bandeja")
def bandeja(
    request: Request,
    convocatoria: Optional[str] = None,
    estado: str = "activas",
    ia: Optional[str] = None,
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    convocatorias_stmt = select(Convocatoria).where(
        Convocatoria.status.in_(
            [ConvocatoriaStatus.PUBLICADA, ConvocatoriaStatus.CERRADA]
        )
    )
    if user.role == UserRole.COORDINADOR:
        convocatorias_stmt = convocatorias_stmt.where(
            Convocatoria.created_by == user.id
        )
    convs_visibles = session.exec(
        convocatorias_stmt.order_by(Convocatoria.codigo)
    ).all()
    ids_visibles = [c.id for c in convs_visibles]

    if not ids_visibles:
        postulaciones: list[dict] = []
    else:
        stmt = (
            select(Postulacion, Convocatoria, User)
            .join(Convocatoria, Postulacion.convocatoria_id == Convocatoria.id)
            .join(User, Postulacion.estudiante_id == User.id)
            .where(Postulacion.convocatoria_id.in_(ids_visibles))
        )

        estado_norm = (estado or "activas").lower()
        if estado_norm == "enviada":
            stmt = stmt.where(Postulacion.estado == POST_ENVIADA)
        elif estado_norm == "en_revision":
            stmt = stmt.where(Postulacion.estado == POST_EN_REVISION)
        elif estado_norm == "todas":
            pass
        else:  # activas (default)
            stmt = stmt.where(
                Postulacion.estado.in_([POST_ENVIADA, POST_EN_REVISION])
            )

        if convocatoria:
            try:
                conv_uuid = uuid.UUID(convocatoria)
                stmt = stmt.where(Postulacion.convocatoria_id == conv_uuid)
            except ValueError:
                pass

        ia_filtro = (ia or "").lower()
        _IA_MAP = {
            "apto": "AUTO_APTO",
            "no_apto": "AUTO_NO_APTO",
            "revisar": "REVISAR_MANUAL",
        }
        if ia_filtro in _IA_MAP:
            stmt = stmt.where(
                sa_text(
                    "postulaciones.evaluacion_ia_ultima->>'decision_sugerida' = :decision"
                ).bindparams(decision=_IA_MAP[ia_filtro])
            )

        rows = session.exec(
            stmt.order_by(Postulacion.created_at.desc())
        ).all()
        postulaciones = [
            {
                "id": post.id,
                "estado": post.estado,
                "created_at": post.created_at,
                "decision_automatica": post.decision_automatica,
                "convocatoria_id": conv.id,
                "convocatoria_codigo": conv.codigo,
                "convocatoria_titulo": conv.titulo,
                "estudiante_nombre": estudiante.full_name or estudiante.email,
                "estudiante_email": estudiante.email,
                "tiene_warning": any(
                    isinstance(ev, dict) and ev.get("manual_review") is True
                    for ev in (post.historial_estados or [])
                ),
                "evaluacion": post.evaluacion_ia_ultima,
            }
            for post, conv, estudiante in rows
        ]

    ctx = {
        "user": user,
        "postulaciones": postulaciones,
        "convocatorias_filtro": convs_visibles,
        "filtro_estado": (estado or "activas").lower(),
        "filtro_convocatoria": convocatoria or "",
        "filtro_ia": (ia or "").lower(),
    }
    ctx.update(_notif_ctx_user(session, user))
    return templates.TemplateResponse(request, "bandeja.html", ctx)


def _load_postulacion_or_404(
    session: Session, post_id: int, user: User
) -> tuple[Postulacion, Convocatoria, User]:
    row = session.exec(
        select(Postulacion, Convocatoria, User)
        .join(Convocatoria, Postulacion.convocatoria_id == Convocatoria.id)
        .join(User, Postulacion.estudiante_id == User.id)
        .where(Postulacion.id == post_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Postulación no encontrada")
    post, conv, estudiante = row
    if not _puede_ver_postulacion(user, conv):
        raise HTTPException(
            status_code=403,
            detail="Solo el coordinador owner o un administrador pueden ver esta postulación.",
        )
    return post, conv, estudiante


@app.get("/postulaciones/{post_id}")
def postulacion_detalle(
    request: Request,
    post_id: int,
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    post, conv, estudiante = _load_postulacion_or_404(session, post_id, user)
    es_owner_o_admin = (
        user.role == UserRole.ADMINISTRADOR or conv.created_by == user.id
    )
    puede_iniciar_revision = post.estado == POST_ENVIADA and es_owner_o_admin
    puede_anotar = post.estado == POST_EN_REVISION and es_owner_o_admin
    puede_decidir = post.estado == POST_EN_REVISION and es_owner_o_admin
    return templates.TemplateResponse(
        request,
        "postulacion_detalle.html",
        {
            "user": user,
            "postulacion": post,
            "convocatoria": conv,
            "estudiante": estudiante,
            "puede_iniciar_revision": puede_iniciar_revision,
            "puede_anotar": puede_anotar,
            "puede_decidir": puede_decidir,
        },
    )


@app.post("/postulaciones/{post_id}/transicionar")
def postulacion_transicionar(
    request: Request,
    post_id: int,
    nuevo_estado: str = Form(...),
    motivo: str = Form(""),
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    post, conv, estudiante = _load_postulacion_or_404(session, post_id, user)
    estado_origen = post.estado
    nuevo_norm = nuevo_estado.upper()
    if nuevo_norm == POST_RECHAZADA and not (motivo or "").strip():
        _flash(
            request,
            "danger",
            "El motivo es obligatorio al rechazar una postulación.",
        )
        return RedirectResponse(f"/postulaciones/{post.id}", status_code=303)
    try:
        transicionar_postulacion(
            post, nuevo_norm, user, conv, motivo=motivo
        )
    except TransicionInvalidaError as e:
        _flash(request, "danger", f"Transición no permitida: {e}")
        return RedirectResponse(f"/postulaciones/{post.id}", status_code=303)
    except PermisoInsuficienteError as e:
        _flash(request, "danger", f"Permiso insuficiente: {e}")
        return RedirectResponse(f"/postulaciones/{post.id}", status_code=303)

    if post.estado == POST_EN_REVISION:
        _registrar_evaluacion_ia(
            post, conv, estudiante, user, trigger="iniciar_revision", session=session
        )

    session.add(post)

    _notif_titulo_cuerpo = {
        POST_EN_REVISION: (
            f"Tu postulación a {conv.codigo} está en revisión",
            f"El coordinador comenzó la revisión de tu postulación a "
            f"«{conv.titulo}».",
        ),
        POST_APROBADA: (
            f"¡Postulación aprobada en {conv.codigo}!",
            f"Tu postulación a «{conv.titulo}» fue aprobada. "
            f"Falta la adjudicación final.",
        ),
        POST_RECHAZADA: (
            f"Postulación rechazada en {conv.codigo}",
            (
                f"Tu postulación a «{conv.titulo}» fue rechazada. "
                f"Motivo: {motivo}"
                if (motivo or "").strip()
                else f"Tu postulación a «{conv.titulo}» fue rechazada."
            ),
        ),
    }
    if post.estado in _notif_titulo_cuerpo:
        titulo, cuerpo = _notif_titulo_cuerpo[post.estado]
        crear_notificacion(
            session,
            usuario_id=estudiante.id,
            titulo=titulo,
            cuerpo=cuerpo,
            url_destino=f"/convocatorias/{conv.id}",
        )

    session.commit()
    log_audit(
        session,
        user_id=user.id,
        action=f"POSTULACION_{estado_origen}_TO_{post.estado}",
        request=request,
        entity_type="postulacion",
        entity_id=None,
        payload={
            "postulacion_id": post.id,
            "convocatoria": conv.codigo,
            "from": estado_origen,
            "to": post.estado,
            "motivo": motivo,
        },
    )
    _flash(
        request,
        "success",
        f"Postulación {post.id}: {estado_origen} → {post.estado}.",
    )
    return RedirectResponse(f"/postulaciones/{post.id}", status_code=303)


@app.post("/postulaciones/{post_id}/reevaluar")
def postulacion_reevaluar(
    request: Request,
    post_id: int,
    user: User = Depends(require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)),
    session: Session = Depends(get_session),
):
    post, conv, estudiante = _load_postulacion_or_404(session, post_id, user)
    _registrar_evaluacion_ia(
        post, conv, estudiante, user,
        trigger="reevaluar",
        session=session,
        forzar_reevaluacion=True,
    )
    session.add(post)
    session.commit()
    log_audit(session, user_id=user.id, action="REEVALUAR_IA", request=request,
              entity_type="postulacion", entity_id=None,
              payload={"postulacion_id": post_id})
    _flash(request, "success", "Evaluación IA actualizada.")
    return RedirectResponse(f"/postulaciones/{post_id}", status_code=303)


@app.post("/postulaciones/{post_id}/nota")
def postulacion_nota(
    request: Request,
    post_id: int,
    nota: str = Form(...),
    user: User = Depends(
        require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)
    ),
    session: Session = Depends(get_session),
):
    post, conv, _est = _load_postulacion_or_404(session, post_id, user)
    nota_limpia = (nota or "").strip()
    if not nota_limpia:
        _flash(request, "warning", "La nota no puede estar vacía.")
        return RedirectResponse(f"/postulaciones/{post.id}", status_code=303)

    historial = list(post.historial_estados or [])
    historial.append(
        {
            "tipo": "nota_coord",
            "by_user_id": str(user.id),
            "by_email": user.email,
            "nota": nota_limpia,
            "at": datetime.utcnow().isoformat(),
        }
    )
    post.historial_estados = historial
    post.updated_at = datetime.utcnow()
    session.add(post)
    session.commit()
    log_audit(
        session,
        user_id=user.id,
        action="POSTULACION_NOTA",
        request=request,
        entity_type="postulacion",
        entity_id=None,
        payload={"postulacion_id": post.id, "convocatoria": conv.codigo},
    )
    _flash(request, "success", "Nota agregada al historial.")
    return RedirectResponse(f"/postulaciones/{post.id}", status_code=303)


# ============================================================
# Adjudicación batch (P06)
# ============================================================


def _load_aprobadas_de_convocatoria(
    session: Session, conv_id: uuid.UUID
) -> list[tuple[Postulacion, User]]:
    rows = session.exec(
        select(Postulacion, User)
        .join(User, Postulacion.estudiante_id == User.id)
        .where(Postulacion.convocatoria_id == conv_id)
        .where(Postulacion.estado == POST_APROBADA)
        .order_by(Postulacion.created_at.asc())
    ).all()
    return [(post, est) for post, est in rows]


@app.get("/convocatorias/{conv_id}/adjudicar")
def convocatorias_adjudicar_get(
    request: Request,
    conv_id: uuid.UUID,
    user: User = Depends(require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)),
    session: Session = Depends(get_session),
):
    conv, _creator, _f, _m = _load_convocatoria_or_404(session, conv_id)
    if user.role == UserRole.COORDINADOR and conv.created_by != user.id:
        raise HTTPException(
            status_code=403,
            detail="Solo el coordinador que creó la convocatoria puede adjudicarla.",
        )
    if conv.status != ConvocatoriaStatus.CERRADA:
        _flash(
            request,
            "warning",
            "Cierre la convocatoria primero antes de adjudicar.",
        )
        return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)

    rows = _load_aprobadas_de_convocatoria(session, conv.id)
    aprobadas = [
        {
            "id": post.id,
            "estudiante_nombre": est.full_name or est.email,
            "estudiante_email": est.email,
            "promedio": post.promedio_acumulado,
            "evaluacion": post.evaluacion_ia_ultima,
        }
        for post, est in rows
    ]
    return templates.TemplateResponse(
        request,
        "convocatorias_adjudicar.html",
        {
            "user": user,
            "convocatoria": conv,
            "aprobadas": aprobadas,
        },
    )


@app.post("/convocatorias/{conv_id}/adjudicar")
def convocatorias_adjudicar_post(
    request: Request,
    conv_id: uuid.UUID,
    seleccionadas: list[int] = Form(default=[]),
    user: User = Depends(require_role(UserRole.COORDINADOR, UserRole.ADMINISTRADOR)),
    session: Session = Depends(get_session),
):
    conv, _creator, _f, _m = _load_convocatoria_or_404(session, conv_id)
    if user.role == UserRole.COORDINADOR and conv.created_by != user.id:
        raise HTTPException(
            status_code=403,
            detail="Solo el coordinador que creó la convocatoria puede adjudicarla.",
        )
    rows = _load_aprobadas_de_convocatoria(session, conv.id)
    postulaciones_aprobadas = [post for post, _ in rows]

    try:
        monitores, resumen = adjudicar_convocatoria(
            conv, postulaciones_aprobadas, seleccionadas, user
        )
    except AdjudicacionInvalidaError as e:
        session.rollback()
        _flash(request, "danger", f"No se pudo adjudicar: {e}")
        return RedirectResponse(
            f"/convocatorias/{conv.id}/adjudicar", status_code=303
        )
    except (TransicionInvalidaError, PermisoInsuficienteError) as e:
        session.rollback()
        _flash(request, "danger", f"Error en transición batch: {e}")
        return RedirectResponse(
            f"/convocatorias/{conv.id}/adjudicar", status_code=303
        )

    for post in postulaciones_aprobadas:
        session.add(post)
    for monitor in monitores:
        session.add(monitor)
    session.add(conv)

    set_seleccionados = set(seleccionadas)
    for post in postulaciones_aprobadas:
        if post.id in set_seleccionados:
            titulo = f"¡Has sido designado monitor de {conv.codigo}!"
            cuerpo = (
                f"Felicidades. Quedaste adjudicado como monitor de "
                f"«{conv.titulo}» para el semestre {conv.semestre}."
            )
        else:
            titulo = f"Adjudicación: no resultaste seleccionado en {conv.codigo}"
            cuerpo = (
                f"Tu postulación a «{conv.titulo}» fue aprobada pero no "
                f"resultó adjudicada esta vez. Te animamos a postular a "
                f"futuras convocatorias."
            )
        crear_notificacion(
            session,
            usuario_id=post.estudiante_id,
            titulo=titulo,
            cuerpo=cuerpo,
            url_destino=f"/convocatorias/{conv.id}",
        )

    try:
        session.commit()
    except Exception as e:
        session.rollback()
        _flash(request, "danger", f"Error al persistir la adjudicación: {e}")
        return RedirectResponse(
            f"/convocatorias/{conv.id}/adjudicar", status_code=303
        )

    log_audit(
        session,
        user_id=user.id,
        action="ADJUDICAR_CONVOCATORIA",
        request=request,
        entity_type="convocatoria",
        entity_id=conv.id,
        payload={
            "codigo": conv.codigo,
            "adjudicadas": resumen["adjudicadas"],
            "no_adjudicadas": resumen["no_adjudicadas"],
            "cupos_libres": resumen["cupos_libres"],
        },
    )
    _flash(
        request,
        "success",
        f"Adjudicación completada: {resumen['adjudicadas']} monitores asignados, "
        f"{resumen['no_adjudicadas']} no adjudicadas.",
    )
    return RedirectResponse(f"/convocatorias/{conv.id}", status_code=303)


# ============================================================
# Mis monitorías + notificaciones (P07)
# ============================================================


@app.get("/mis-monitorias")
def mis_monitorias(
    request: Request,
    user: User = Depends(require_role(UserRole.ESTUDIANTE)),
    session: Session = Depends(get_session),
):
    from app.models import Monitor

    rows = session.exec(
        select(Monitor, Convocatoria, Facultad, Materia)
        .join(Convocatoria, Monitor.convocatoria_id == Convocatoria.id)
        .outerjoin(Facultad, Convocatoria.facultad_id == Facultad.id)
        .outerjoin(Materia, Convocatoria.materia_id == Materia.id)
        .where(Monitor.estudiante_id == user.id)
        .order_by(Monitor.fecha_adjudicacion.desc())
    ).all()
    monitorias = [
        {
            "id": m.id,
            "convocatoria_id": conv.id,
            "convocatoria_codigo": conv.codigo,
            "convocatoria_titulo": conv.titulo,
            "facultad": facultad_obj,
            "materia": materia_obj,
            "fecha_adjudicacion": m.fecha_adjudicacion,
            "semestre": m.semestre,
            "estado": m.estado,
        }
        for m, conv, facultad_obj, materia_obj in rows
    ]
    ctx = {"user": user, "monitorias": monitorias}
    ctx.update(_notif_ctx_user(session, user))
    return templates.TemplateResponse(request, "mis_monitorias.html", ctx)


@app.get("/admin/panel")
def admin_panel_get(
    request: Request,
    user: User = Depends(require_role(UserRole.ADMINISTRADOR)),
    session: Session = Depends(get_session),
):
    cfg = session.exec(select(ConfiguracionIA)).first()
    if cfg is None:
        cfg = ConfiguracionIA()
    ctx = {"user": user, "cfg": cfg}
    ctx.update(_notif_ctx_user(session, user))
    return templates.TemplateResponse(request, "panel.html", ctx)


@app.post("/admin/panel")
def admin_panel_post(
    request: Request,
    umbral_confianza: str = Form(default="0.5"),
    peso_promedio: str = Form(default="33"),
    peso_creditos: str = Form(default="33"),
    peso_semestre: str = Form(default="34"),
    modelo_activo: str = Form(default="gemini-2.0-flash"),
    modo_fallback: str = Form(default=""),
    user: User = Depends(require_role(UserRole.ADMINISTRADOR)),
    session: Session = Depends(get_session),
):
    cfg = session.exec(select(ConfiguracionIA)).first()
    if cfg is None:
        cfg = ConfiguracionIA()
        session.add(cfg)

    try:
        cfg.umbral_confianza = max(0.0, min(1.0, float(umbral_confianza.strip() or "0.5")))
        cfg.peso_promedio = max(0, min(100, int(peso_promedio.strip() or "33")))
        cfg.peso_creditos = max(0, min(100, int(peso_creditos.strip() or "33")))
        cfg.peso_semestre = max(0, min(100, int(peso_semestre.strip() or "34")))
    except (ValueError, TypeError):
        _flash(request, "danger", "Los valores numéricos ingresados no son válidos.")
        return RedirectResponse("/admin/panel", status_code=303)

    cfg.modelo_activo = (modelo_activo.strip() or "gemini-2.0-flash")[:100]
    cfg.modo_fallback = modo_fallback == "on"
    cfg.updated_at = datetime.utcnow()
    session.commit()

    _flash(request, "success", "Configuración actualizada correctamente.")
    return RedirectResponse("/admin/panel", status_code=303)


@app.post("/convocatorias/{conv_id}/eliminar")
def convocatorias_eliminar(
    request: Request,
    conv_id: uuid.UUID,
    user: User = Depends(require_role(UserRole.ADMINISTRADOR)),
    session: Session = Depends(get_session),
):
    conv = session.exec(select(Convocatoria).where(Convocatoria.id == conv_id)).first()
    if conv is None:
        raise HTTPException(status_code=404, detail="Convocatoria no encontrada")
    if conv.status != ConvocatoriaStatus.ARCHIVADA:
        _flash(request, "danger", "Solo se pueden eliminar convocatorias archivadas.")
        return RedirectResponse(f"/convocatorias/{conv_id}", status_code=303)

    from app.models import Monitor
    monitores = session.exec(select(Monitor).where(Monitor.convocatoria_id == conv_id)).all()
    for m in monitores:
        session.delete(m)

    postulaciones = session.exec(select(Postulacion).where(Postulacion.convocatoria_id == conv_id)).all()
    for p in postulaciones:
        session.delete(p)

    session.delete(conv)
    session.commit()

    log_audit(session, user_id=user.id, action="DELETE_CONVOCATORIA", request=request,
              entity_type="convocatoria", entity_id=conv_id,
              payload={"codigo": conv.codigo})

    _flash(request, "success", "Convocatoria eliminada correctamente.")
    return RedirectResponse("/convocatorias/archivadas", status_code=303)


@app.get("/notificaciones")
def notificaciones_get(
    request: Request,
    user: User = Depends(require_login),
    session: Session = Depends(get_session),
):
    from app.models import Notificacion as _N

    rows = session.exec(
        select(_N)
        .where(_N.usuario_id == user.id)
        .order_by(_N.leida.asc(), _N.created_at.desc())
    ).all()
    ctx = {
        "user": user,
        "notificaciones": [
            {
                "id": n.id,
                "titulo": n.titulo,
                "cuerpo": n.cuerpo,
                "url_destino": n.url_destino,
                "leida": n.leida,
                "created_at": n.created_at,
            }
            for n in rows
        ],
    }
    ctx.update(_notif_ctx_user(session, user))
    return templates.TemplateResponse(request, "notificaciones.html", ctx)


@app.post("/notificaciones/marcar-todas-leidas")
def notificaciones_marcar_todas(
    request: Request,
    user: User = Depends(require_login),
    session: Session = Depends(get_session),
):
    from app.models import Notificacion as _N

    no_leidas = session.exec(
        select(_N).where(_N.usuario_id == user.id).where(_N.leida.is_(False))
    ).all()
    for n in no_leidas:
        n.leida = True
        session.add(n)
    session.commit()
    _flash(request, "success", f"Marcadas {len(no_leidas)} notificaciones como leídas.")
    return RedirectResponse("/notificaciones", status_code=303)


@app.post("/notificaciones/{notif_id}/marcar-leida")
def notificacion_marcar_leida(
    request: Request,
    notif_id: int,
    user: User = Depends(require_login),
    session: Session = Depends(get_session),
):
    from app.models import Notificacion as _N

    notif = session.exec(
        select(_N).where(_N.id == notif_id).where(_N.usuario_id == user.id)
    ).first()
    if notif is None:
        raise HTTPException(status_code=404, detail="Notificación no encontrada")
    if not notif.leida:
        notif.leida = True
        session.add(notif)
        session.commit()
    if notif.url_destino:
        return RedirectResponse(notif.url_destino, status_code=303)
    return RedirectResponse("/notificaciones", status_code=303)


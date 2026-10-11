"""HTTP-шар: розбір запиту, виклик сервісу, формування відповіді. Бізнес-логіки тут немає."""

import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.errors import DomainError, TooManyRequests
from app.files import SNIFF_BYTES, UnsupportedFile, safe_filename, sniff
from app.idempotency import Stored, run_once
from app.jobs import Queue
from app.repositories import Document
from app.security import Claims, InvalidToken, TokenService
from app.services import AuthService, Conflict, DocumentService, Unauthorized
from app.storage import Storage, StorageError, TooLarge, new_key

log = logging.getLogger(__name__)
router = APIRouter()
bearer = HTTPBearer(auto_error=False, description="Access-токен з /api/v1/auth/login")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{1,128}")


# --- моделі ---------------------------------------------------------------------------------
class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # лишні поля це 422 (mass assignment)


class Credentials(Strict):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshIn(Strict):
    refresh_token: str = Field(min_length=1, max_length=512)


class TokensOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


class UserOut(BaseModel):
    id: int
    email: str
    role: str


class DocumentIn(Strict):
    title: str = Field(min_length=1, max_length=200)


class DocumentOut(BaseModel):
    id: int
    title: str
    status: str
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    error: str | None = None


class UploadUrlIn(Strict):
    filename: str = Field(min_length=1, max_length=255)


class PayloadTooLarge(DomainError):
    status = 413
    title = "Payload too large"


class UnsupportedMedia(DomainError):
    status = 415
    title = "Unsupported media type"


class ServiceUnavailable(DomainError):
    status = 503
    title = "Service unavailable"


def doc_out(d: Document) -> DocumentOut:
    return DocumentOut(
        id=d.id,
        title=d.title,
        status=d.status,
        filename=d.filename,
        content_type=d.content_type,
        size_bytes=d.size_bytes,
        error=d.error,
    )


# --- залежності -----------------------------------------------------------------------------
def documents(request: Request) -> DocumentService:
    service: DocumentService = request.app.state.documents
    return service


def auth_service(request: Request) -> AuthService:
    service: AuthService = request.app.state.auth
    return service


def tokens(request: Request) -> TokenService:
    service: TokenService = request.app.state.tokens
    return service


Docs = Annotated[DocumentService, Depends(documents)]
Auth = Annotated[AuthService, Depends(auth_service)]


def current_claims(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Claims:
    unauthorized = Unauthorized("Missing or invalid access token")
    unauthorized.headers = {"WWW-Authenticate": "Bearer"}
    if creds is None:
        raise unauthorized
    try:
        claims = tokens(request).verify_access(creds.credentials)
    except InvalidToken as exc:
        raise unauthorized from exc
    limit = request.app.state.limits
    decision = request.app.state.limiter.hit(
        "api", str(claims.user_id), limit=limit.api_limit, window=limit.api_window
    )
    if not decision.allowed:
        raise TooManyRequests(decision.retry_after)
    return claims


class Forbidden(DomainError):
    status = 403
    title = "Forbidden"


def admin_only(claims: Annotated[Claims, Depends(current_claims)]) -> Claims:
    if claims.role != "admin":
        raise Forbidden("Admin role required")
    return claims


Current = Annotated[Claims, Depends(current_claims)]
Admin = Annotated[Claims, Depends(admin_only)]


# --- health (публічні) ----------------------------------------------------------------------
@router.get("/health/live", tags=["health"])
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", tags=["health"])
def ready(request: Request) -> JSONResponse:
    """Готовність: усі залежності відповідають. Балансувальник знімає інстанс при 503."""
    failed: list[str] = []
    for name, check in request.app.state.ready_checks.items():
        try:
            check()
        except Exception:
            log.exception("readiness check failed: %s", name)
            failed.append(name)
    if failed:
        return JSONResponse({"status": "unavailable", "failed": failed}, status_code=503)
    return JSONResponse({"status": "ok"})


# --- автентифікація (публічні) ---------------------------------------------------------------
@router.post("/api/v1/auth/register", status_code=201, response_model=UserOut, tags=["auth"])
def register(body: Credentials, auth: Auth) -> UserOut:
    user = auth.register(body.email, body.password)
    return UserOut(id=user.id, email=user.email, role=user.role)


@router.post("/api/v1/auth/login", response_model=TokensOut, tags=["auth"])
def login(body: Credentials, request: Request, auth: Auth) -> dict[str, Any]:
    limits = request.app.state.limits
    limiter = request.app.state.limiter
    email = body.email.lower()
    ip = request.client.host if request.client else "unknown"
    # перебір одного акаунта з одного IP і з багатьох IP обмежуються окремо (CWE-307)
    for scope, subject, limit in (
        ("login", f"{ip}:{email}", limits.login_limit),
        ("login-email", email, limits.login_email_limit),
    ):
        decision = limiter.hit(scope, subject, limit=limit, window=limits.login_window)
        if not decision.allowed:
            raise TooManyRequests(decision.retry_after)
    return auth.login(body.email, body.password)


@router.post("/api/v1/auth/refresh", response_model=TokensOut, tags=["auth"])
def refresh(body: RefreshIn, auth: Auth) -> dict[str, Any]:
    return auth.refresh(body.refresh_token)


@router.post("/api/v1/auth/logout", status_code=204, tags=["auth"])
def logout(body: RefreshIn, auth: Auth) -> Response:
    auth.logout(body.refresh_token)
    return Response(status_code=204)


# --- захищені ---------------------------------------------------------------------------------
@router.get("/api/v1/me", response_model=UserOut, tags=["auth"])
def me(claims: Current, auth: Auth) -> UserOut:
    user = auth.user_for(claims.user_id)
    if user is None:
        raise Unauthorized("User no longer exists")
    return UserOut(id=user.id, email=user.email, role=user.role)


@router.get("/api/v1/admin/users", response_model=list[UserOut], tags=["admin"])
def admin_users(_: Admin, auth: Auth) -> list[UserOut]:
    return [UserOut(id=u.id, email=u.email, role=u.role) for u in auth.all_users()]


@router.post("/api/v1/documents", status_code=201, response_model=DocumentOut, tags=["documents"])
def create_document(
    body: DocumentIn,
    request: Request,
    docs: Docs,
    claims: Current,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Response:
    def create() -> Stored:
        doc = docs.create(claims.user_id, body.title)
        return Stored(201, doc_out(doc).model_dump())

    if idempotency_key is None:
        result, replayed = create(), False
    else:
        if not IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise DomainError("Idempotency-Key must match [A-Za-z0-9_-]{1,128}")
        result, replayed = run_once(
            request.app.state.kv,
            user_id=claims.user_id,
            key=idempotency_key,
            payload=body.model_dump(),
            execute=create,
        )
    headers = {"Idempotency-Replayed": "true"} if replayed else None
    return JSONResponse(result.body, status_code=result.status, headers=headers)


@router.get("/api/v1/documents", response_model=list[DocumentOut], tags=["documents"])
def list_documents(docs: Docs, claims: Current) -> list[DocumentOut]:
    return [doc_out(d) for d in docs.list_mine(claims.user_id)]


@router.get("/api/v1/documents/{doc_id}", response_model=DocumentOut, tags=["documents"])
def get_document(doc_id: int, docs: Docs, claims: Current) -> DocumentOut:
    return doc_out(docs.get_mine(claims.user_id, doc_id))


# --- файли документа ----------------------------------------------------------------------------
async def _enqueue_or_rollback(
    request: Request, docs: DocumentService, doc_id: int, storage: Storage, key: str
) -> None:
    queue: Queue = request.app.state.queue
    try:
        await queue.enqueue_ingest(doc_id)
    except Exception as exc:
        log.exception("cannot enqueue ingest for document %s", doc_id)
        await run_in_threadpool(storage.delete, key)
        await run_in_threadpool(
            docs.update,
            doc_id,
            status="created",
            storage_key=None,
            content_type=None,
            size_bytes=None,
        )
        raise ServiceUnavailable("Cannot queue the document now, retry later") from exc


@router.put(
    "/api/v1/documents/{doc_id}/file",
    status_code=202,
    response_model=DocumentOut,
    tags=["documents"],
)
async def upload_file(
    doc_id: int,
    request: Request,
    docs: Docs,
    claims: Current,
    filename: Annotated[str, Query(min_length=1, max_length=255)],
) -> DocumentOut:
    """Потокове завантаження: тіло запиту (сирі байти) пишеться шматками, ліміт діє на льоту."""
    doc = await run_in_threadpool(docs.get_mine, claims.user_id, doc_id)  # чужий документ = 404
    if doc.status != "created":
        raise Conflict("File was already uploaded")
    storage: Storage = request.app.state.storage
    limit: int = request.app.state.limits.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLarge(f"File is larger than {limit} bytes")

    key = new_key()  # у сховищі файл лежить під випадковим ключем, не під іменем від клієнта
    upload = storage.begin_upload(key, limit)
    head = bytearray()
    ctype: str | None = None
    try:
        async for chunk in request.stream():
            if ctype is None:
                head += chunk[: SNIFF_BYTES - len(head)]
                if len(head) >= SNIFF_BYTES:
                    ctype = sniff(bytes(head))  # відмовляємо одразу, не чекаючи кінця файлу
            await run_in_threadpool(upload.write, chunk)
        ctype = ctype or sniff(bytes(head))
        size = await run_in_threadpool(upload.finish)
    except TooLarge as exc:
        await run_in_threadpool(upload.abort)
        raise PayloadTooLarge(f"File is larger than {limit} bytes") from exc
    except UnsupportedFile as exc:
        await run_in_threadpool(upload.abort)
        raise UnsupportedMedia(f"Unsupported file: {exc}") from exc
    except BaseException:
        await run_in_threadpool(upload.abort)
        raise
    await run_in_threadpool(
        docs.update,
        doc_id,
        status="queued",
        storage_key=key,
        content_type=ctype,
        size_bytes=size,
        filename=safe_filename(filename),
    )
    await _enqueue_or_rollback(request, docs, doc_id, storage, key)
    return doc_out(await run_in_threadpool(docs.get_mine, claims.user_id, doc_id))


@router.post("/api/v1/documents/{doc_id}/upload-url", tags=["documents"])
async def upload_url(
    doc_id: int, body: UploadUrlIn, request: Request, docs: Docs, claims: Current
) -> dict[str, Any]:
    """Presigned POST: клієнт вантажить файл прямо в S3 (повз API), S3 сам тримає ліміт."""
    doc = await run_in_threadpool(docs.get_mine, claims.user_id, doc_id)
    if doc.status != "created":
        raise Conflict("File was already uploaded")
    storage: Storage = request.app.state.storage
    ttl: int = request.app.state.limits.presign_ttl
    limit: int = request.app.state.limits.max_upload_bytes
    key = new_key()
    try:
        post = await run_in_threadpool(storage.presigned_upload, key, limit, ttl)
    except StorageError as exc:
        raise DomainError("Direct upload is not available with this storage") from exc
    await run_in_threadpool(
        docs.update, doc_id, storage_key=key, filename=safe_filename(body.filename)
    )
    return {"url": post["url"], "fields": post["fields"], "expires_in": ttl}


@router.post(
    "/api/v1/documents/{doc_id}/complete",
    status_code=202,
    response_model=DocumentOut,
    tags=["documents"],
)
async def complete_upload(
    doc_id: int, request: Request, docs: Docs, claims: Current
) -> DocumentOut:
    """Клієнт каже, що завантажив у S3: перевіряємо розмір і тип за вмістом, потім у чергу."""
    doc = await run_in_threadpool(docs.get_mine, claims.user_id, doc_id)
    if doc.status != "created" or doc.storage_key is None:
        raise Conflict("No pending direct upload for this document")
    storage: Storage = request.app.state.storage
    limit: int = request.app.state.limits.max_upload_bytes
    key = doc.storage_key
    size = await run_in_threadpool(storage.size, key)
    if size is None:
        raise Conflict("The file has not been uploaded yet")
    try:
        if size > limit:
            raise TooLarge
        ctype = sniff(await run_in_threadpool(storage.read_head, key, SNIFF_BYTES))
    except (TooLarge, UnsupportedFile) as exc:
        await run_in_threadpool(storage.delete, key)
        await run_in_threadpool(docs.update, doc_id, storage_key=None)
        if isinstance(exc, TooLarge):
            raise PayloadTooLarge(f"File is larger than {limit} bytes") from exc
        raise UnsupportedMedia(f"Unsupported file: {exc}") from exc
    await run_in_threadpool(
        docs.update, doc_id, status="queued", content_type=ctype, size_bytes=size
    )
    await _enqueue_or_rollback(request, docs, doc_id, storage, key)
    return doc_out(await run_in_threadpool(docs.get_mine, claims.user_id, doc_id))

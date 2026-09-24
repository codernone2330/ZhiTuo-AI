import base64
import binascii
import io
import socket
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import AppError, CommonErrorCode
from app.core.responses import PageData
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentIdentity, IdentityContext
from app.modules.documents.models import DocumentEvent, DocumentVersion, SharedDocument
from app.modules.organizations.models import Organization

router = APIRouter()
Db = Annotated[Session, Depends(get_db)]
MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".pdf", ".txt"}
MIME_BY_EXTENSION = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
}
OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")


class DocumentCreate(BaseModel):
    organizationId: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    category: str = Field(default="综合资料", max_length=50)
    mimeType: str = Field(default="application/octet-stream", max_length=120)
    contentBase64: str = Field(min_length=1, max_length=8_000_000)


class DocumentUpdate(BaseModel):
    version: int = Field(ge=1)
    contentBase64: str = Field(min_length=1, max_length=8_000_000)
    name: str = Field(min_length=1, max_length=255)
    category: str = Field(default="综合资料", max_length=50)
    mimeType: str = Field(default="application/octet-stream", max_length=120)


class DocumentRestore(BaseModel):
    version: int = Field(ge=1)
    currentVersion: int = Field(ge=1)


def _scan(content: bytes) -> None:
    settings = get_settings()
    if not settings.clamav_host:
        return  # Local prototype; shared deployments require a configured scanner.
    try:
        with socket.create_connection(
            (settings.clamav_host, settings.clamav_port), timeout=5
        ) as sock:
            sock.settimeout(15)
            sock.sendall(b"zINSTREAM\0")
            for start in range(0, len(content), 65536):
                chunk = content[start : start + 65536]
                sock.sendall(len(chunk).to_bytes(4, "big") + chunk)
            sock.sendall((0).to_bytes(4, "big"))
            result = sock.recv(1024)
    except (OSError, TimeoutError) as exc:
        raise AppError("DOCUMENT.SCAN_UNAVAILABLE", "文件安全扫描暂不可用", 503) from exc
    if b"FOUND" in result:
        raise AppError("DOCUMENT.MALWARE", "文件未通过安全扫描", 400)
    if b"OK" not in result:
        raise AppError("DOCUMENT.SCAN_UNAVAILABLE", "文件安全扫描结果无效", 503)


def _content(value: str, name: str) -> tuple[bytes, str]:
    extension = Path(name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS or Path(name).name != name or "\\" in name:
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "仅支持 Word、Excel、PDF 和 TXT 文件", 400)
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "文件内容不是有效 Base64", 400) from exc
    if not content or len(content) > MAX_FILE_BYTES:
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "文件大小须为 1 字节至 5 MB", 400)
    if extension in {".docx", ".xlsx"}:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
                required = "word/document.xml" if extension == ".docx" else "xl/workbook.xml"
                if (
                    "[Content_Types].xml" not in names
                    or required not in names
                    or len(names) > 1000
                    or sum(item.file_size for item in archive.infolist()) > 50 * 1024 * 1024
                    or any("vbaProject.bin" in item for item in names)
                ):
                    raise ValueError("invalid Office archive")
        except (ValueError, zipfile.BadZipFile, OSError) as exc:
            raise AppError(
                CommonErrorCode.INVALID_ARGUMENT, "Office 文件内容或格式无效", 400
            ) from exc
    elif extension in {".doc", ".xls"} and not content.startswith(OLE_SIGNATURE):
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "旧版 Office 文件内容无效", 400)
    elif extension == ".pdf" and not (content.startswith(b"%PDF-") and b"%%EOF" in content[-2048:]):
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "PDF 文件内容无效", 400)
    elif extension == ".txt":
        try:
            decoded = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise AppError(
                CommonErrorCode.INVALID_ARGUMENT, "TXT 文件必须采用 UTF-8 编码", 400
            ) from exc
        if any(ord(character) < 32 and character not in "\r\n\t" for character in decoded):
            raise AppError(CommonErrorCode.INVALID_ARGUMENT, "TXT 文件包含非法控制字符", 400)
    _scan(content)
    return content, MIME_BY_EXTENSION[extension]


def _allowed(identity: IdentityContext, organization: Organization) -> bool:
    return identity.organization_in_scope(organization)


def _writable(identity: IdentityContext, organization: Organization) -> bool:
    return _allowed(identity, organization) and bool(
        identity.role_codes
        & {"super_admin", "group_admin", "org_admin", "department_manager", "customer_manager"}
    )


def _require_write(identity: IdentityContext, organization: Organization) -> None:
    if not _writable(identity, organization):
        raise AppError(CommonErrorCode.FORBIDDEN, "当前身份无权修改该组织资料", 403)


def _document(session: Session, identity: IdentityContext, doc_id: uuid.UUID, lock=False):
    statement = select(SharedDocument).where(
        SharedDocument.id == doc_id, SharedDocument.deleted_at.is_(None)
    )
    doc = session.scalar(statement.with_for_update() if lock else statement)
    if doc is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "资料不存在", 404)
    org = session.get(Organization, doc.organization_id)
    if not _allowed(identity, org):
        raise AppError(CommonErrorCode.FORBIDDEN, "无权访问该组织资料", 403)
    return doc


def _data(doc: SharedDocument):
    return {
        "id": str(doc.id),
        "organizationId": str(doc.organization_id),
        "name": doc.name,
        "category": doc.category,
        "mimeType": doc.mime_type,
        "sizeBytes": doc.size_bytes,
        "version": doc.version,
        "createdAt": doc.created_at.isoformat(),
        "updatedAt": doc.updated_at.isoformat(),
    }


def _event(session, identity, doc, action):
    session.add(
        DocumentEvent(
            id=uuid.uuid4(),
            document_id=doc.id,
            actor_id=identity.user.id,
            action=action,
            version=doc.version,
            created_at=datetime.now(timezone.utc),
        )
    )


def _version(session: Session, identity: IdentityContext, doc: SharedDocument) -> None:
    session.add(
        DocumentVersion(
            id=uuid.uuid4(),
            document_id=doc.id,
            version=doc.version,
            name=doc.name,
            category=doc.category,
            mime_type=doc.mime_type,
            size_bytes=doc.size_bytes,
            content=doc.content,
            created_by=identity.user.id,
            created_at=datetime.now(timezone.utc),
        )
    )


def _download(content: bytes, mime_type: str, name: str, version: int) -> Response:
    return Response(
        content,
        media_type=mime_type,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name),
            "X-Document-Version": str(version),
            "Cache-Control": "no-store",
        },
    )


@router.get("")
def list_documents(
    request: Request,
    identity: CurrentIdentity,
    session: Db,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
):
    statement = (
        select(SharedDocument)
        .join(Organization, SharedDocument.organization_id == Organization.id)
        .where(SharedDocument.deleted_at.is_(None))
    )
    if identity.is_super_admin or identity.is_group_admin:
        pass
    elif identity.is_org_admin:
        statement = statement.where(
            or_(
                Organization.path == identity.organization.path,
                Organization.path.startswith(identity.organization.path + "/"),
            )
        )
    else:
        statement = statement.where(SharedDocument.organization_id == identity.organization.id)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    docs = session.scalars(
        statement.order_by(SharedDocument.updated_at.desc())
        .offset((page - 1) * pageSize)
        .limit(pageSize)
    ).all()
    return {
        "success": True,
        "data": PageData.build([_data(doc) for doc in docs], page, pageSize, total).model_dump(),
        "traceId": request.state.trace_id,
    }


@router.post("", status_code=201)
def create_document(
    payload: DocumentCreate, request: Request, identity: CurrentIdentity, session: Db
):
    org = session.get(Organization, payload.organizationId)
    if org is None or not org.is_active or not _writable(identity, org):
        raise AppError(CommonErrorCode.FORBIDDEN, "无权向该组织上传资料", 403)
    content, mime_type = _content(payload.contentBase64, payload.name)
    doc = SharedDocument(
        id=uuid.uuid4(),
        organization_id=org.id,
        name=payload.name,
        category=payload.category,
        mime_type=mime_type,
        content=content,
        size_bytes=len(content),
        version=1,
        created_by=identity.user.id,
        updated_by=identity.user.id,
    )
    session.add(doc)
    session.flush()
    _version(session, identity, doc)
    _event(session, identity, doc, "created")
    session.commit()
    session.refresh(doc)
    return {"success": True, "data": _data(doc), "traceId": request.state.trace_id}


@router.put("/{document_id}")
def update_document(
    document_id: uuid.UUID,
    payload: DocumentUpdate,
    request: Request,
    identity: CurrentIdentity,
    session: Db,
):
    doc = _document(session, identity, document_id, lock=True)
    _require_write(identity, session.get(Organization, doc.organization_id))
    if doc.version != payload.version:
        raise AppError(CommonErrorCode.CONFLICT, "文件已被更新，请刷新版本后重试", 409)
    content, mime_type = _content(payload.contentBase64, payload.name)
    doc.name, doc.category, doc.mime_type = payload.name, payload.category, mime_type
    doc.content, doc.size_bytes = content, len(content)
    doc.version += 1
    doc.updated_by = identity.user.id
    _version(session, identity, doc)
    _event(session, identity, doc, "updated")
    session.commit()
    session.refresh(doc)
    return {"success": True, "data": _data(doc), "traceId": request.state.trace_id}


@router.get("/{document_id}/download")
def download_document(document_id: uuid.UUID, identity: CurrentIdentity, session: Db):
    doc = _document(session, identity, document_id)
    _event(session, identity, doc, "downloaded")
    session.commit()
    return _download(doc.content, doc.mime_type, doc.name, doc.version)


@router.get("/{document_id}/versions")
def document_versions(
    document_id: uuid.UUID,
    request: Request,
    identity: CurrentIdentity,
    session: Db,
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
):
    _document(session, identity, document_id)
    statement = select(DocumentVersion).where(DocumentVersion.document_id == document_id)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = session.scalars(
        statement.order_by(DocumentVersion.version.desc())
        .offset((page - 1) * pageSize)
        .limit(pageSize)
    ).all()
    return {
        "success": True,
        "data": PageData.build(
            [
                {
                    "version": row.version,
                    "name": row.name,
                    "category": row.category,
                    "sizeBytes": row.size_bytes,
                    "createdAt": row.created_at.isoformat(),
                    "createdBy": str(row.created_by),
                }
                for row in rows
            ],
            page,
            pageSize,
            total,
        ).model_dump(),
        "traceId": request.state.trace_id,
    }


@router.get("/{document_id}/versions/{version}/download")
def download_document_version(
    document_id: uuid.UUID, version: int, identity: CurrentIdentity, session: Db
):
    doc = _document(session, identity, document_id)
    snapshot = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == doc.id, DocumentVersion.version == version
        )
    )
    if snapshot is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "资料版本不存在", 404)
    _event(session, identity, doc, f"download_v{version}")
    session.commit()
    return _download(snapshot.content, snapshot.mime_type, snapshot.name, version)


@router.post("/{document_id}/restore")
def restore_document(
    document_id: uuid.UUID,
    payload: DocumentRestore,
    request: Request,
    identity: CurrentIdentity,
    session: Db,
):
    doc = _document(session, identity, document_id, lock=True)
    _require_write(identity, session.get(Organization, doc.organization_id))
    if doc.version != payload.currentVersion:
        raise AppError(CommonErrorCode.CONFLICT, "文件已被更新，请刷新后重试", 409)
    snapshot = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.document_id == doc.id, DocumentVersion.version == payload.version
        )
    )
    if snapshot is None:
        raise AppError(CommonErrorCode.NOT_FOUND, "资料版本不存在", 404)
    doc.name, doc.category, doc.mime_type = snapshot.name, snapshot.category, snapshot.mime_type
    doc.content, doc.size_bytes = snapshot.content, snapshot.size_bytes
    doc.version += 1
    doc.updated_by = identity.user.id
    _version(session, identity, doc)
    _event(session, identity, doc, "restored")
    session.commit()
    session.refresh(doc)
    return {"success": True, "data": _data(doc), "traceId": request.state.trace_id}


@router.delete("/{document_id}")
def delete_document(
    document_id: uuid.UUID, request: Request, identity: CurrentIdentity, session: Db
):
    doc = _document(session, identity, document_id, lock=True)
    _require_write(identity, session.get(Organization, doc.organization_id))
    doc.deleted_at = datetime.now(timezone.utc)
    doc.version += 1
    _event(session, identity, doc, "deleted")
    session.commit()
    return {"success": True, "data": {"deleted": True}, "traceId": request.state.trace_id}


@router.get("/{document_id}/events")
def document_events(
    document_id: uuid.UUID, request: Request, identity: CurrentIdentity, session: Db
):
    _document(session, identity, document_id)
    rows = session.scalars(
        select(DocumentEvent)
        .where(DocumentEvent.document_id == document_id)
        .order_by(DocumentEvent.created_at.desc())
    ).all()
    return {
        "success": True,
        "data": [
            {
                "action": row.action,
                "actorId": str(row.actor_id),
                "version": row.version,
                "createdAt": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "traceId": request.state.trace_id,
    }

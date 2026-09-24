import base64
import binascii
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import AppError, CommonErrorCode
from app.core.responses import PageData
from app.db.session import get_db
from app.modules.auth.dependencies import CurrentIdentity, IdentityContext
from app.modules.documents.models import DocumentEvent, SharedDocument
from app.modules.organizations.models import Organization

router = APIRouter()
Db = Annotated[Session, Depends(get_db)]
MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".pdf", ".txt"}


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


def _content(value: str, name: str) -> bytes:
    if not any(name.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS):
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "仅支持 Word、Excel、PDF 和 TXT 文件", 400)
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "文件内容不是有效 Base64", 400) from exc
    if not content or len(content) > MAX_FILE_BYTES:
        raise AppError(CommonErrorCode.INVALID_ARGUMENT, "文件大小须为 1 字节至 5 MB", 400)
    return content


def _allowed(identity: IdentityContext, organization: Organization) -> bool:
    return identity.organization_in_scope(organization)


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
    total = len(session.scalars(statement).all())
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
    if org is None or not org.is_active or not _allowed(identity, org):
        raise AppError(CommonErrorCode.FORBIDDEN, "无权向该组织上传资料", 403)
    content = _content(payload.contentBase64, payload.name)
    doc = SharedDocument(
        id=uuid.uuid4(),
        organization_id=org.id,
        name=payload.name,
        category=payload.category,
        mime_type=payload.mimeType,
        content=content,
        size_bytes=len(content),
        version=1,
        created_by=identity.user.id,
        updated_by=identity.user.id,
    )
    session.add(doc)
    session.flush()
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
    if doc.version != payload.version:
        raise AppError(CommonErrorCode.CONFLICT, "文件已被更新，请刷新版本后重试", 409)
    content = _content(payload.contentBase64, payload.name)
    doc.name, doc.category, doc.mime_type = payload.name, payload.category, payload.mimeType
    doc.content, doc.size_bytes = content, len(content)
    doc.version += 1
    doc.updated_by = identity.user.id
    _event(session, identity, doc, "updated")
    session.commit()
    session.refresh(doc)
    return {"success": True, "data": _data(doc), "traceId": request.state.trace_id}


@router.get("/{document_id}/download")
def download_document(document_id: uuid.UUID, identity: CurrentIdentity, session: Db):
    doc = _document(session, identity, document_id)
    _event(session, identity, doc, "downloaded")
    session.commit()
    return Response(
        doc.content,
        media_type=doc.mime_type,
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + __import__("urllib.parse", fromlist=["quote"]).quote(doc.name),
            "X-Document-Version": str(doc.version),
        },
    )


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

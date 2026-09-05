from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ...extensions.support.documents import extract_text
from ...infra.config import get_settings
from ...platform.database import User
from ...services import get_rag_store
from ..deps import current_user

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("/search")
async def search_knowledge(query: str, top_k: int = 5, _user: User = Depends(current_user)) -> dict:
    results = await get_rag_store().search(query, top_k=top_k)
    return {"results": [doc.__dict__ for doc in results]}


@router.get("/documents")
def knowledge_documents(_user: User = Depends(current_user)) -> dict:
    return {"documents": get_rag_store().list_documents()}


@router.post("/upload")
async def upload_knowledge(file: UploadFile = File(...), _user: User = Depends(current_user)) -> dict:
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    settings = get_settings()
    path = Path(settings.upload_dir) / Path(file.filename or "upload.txt").name
    path.write_bytes(raw)
    text = extract_text(path.name, raw)
    if not text.strip():
        raise HTTPException(status_code=400, detail="No text extracted")
    return get_rag_store().add_document(text, source=path.name, title=path.stem)


@router.delete("")
def clear_knowledge(_user: User = Depends(current_user)) -> dict:
    get_rag_store().clear()
    return {"status": "cleared"}


@router.delete("/{doc_id}")
def delete_knowledge_document(doc_id: str, _user: User = Depends(current_user)) -> dict:
    if not get_rag_store().delete_document(doc_id):
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return {"status": "deleted", "doc_id": doc_id}

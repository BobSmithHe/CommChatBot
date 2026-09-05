from __future__ import annotations

from fastapi import APIRouter, Depends

from ...platform.database import User
from ...services import get_code_executor
from ..deps import current_user
from ..schemas import CodeExecuteRequest

router = APIRouter(prefix="/api/code", tags=["code"])


@router.post("/execute")
async def execute_code(req: CodeExecuteRequest, user: User = Depends(current_user)) -> dict:
    result = await get_code_executor().execute(req.code, req.language)
    return {"success": result.get("exit_code") == 0, **result}

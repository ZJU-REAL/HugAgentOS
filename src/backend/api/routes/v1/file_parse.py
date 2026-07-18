"""POST /v1/file/parse — server-side file parsing endpoint."""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from core.auth.backend import get_current_user, UserContext
from core.content.file_parser import SUPPORTED_EXTENSIONS, parse_file

router = APIRouter(prefix="/v1/file", tags=["file"])


@router.post("/parse", summary="解析上传文件，返回文本内容")
async def parse_uploaded_file(
    file: UploadFile = File(...),
    user: UserContext = Depends(get_current_user),
):
    """
    接收上传文件，提取文本内容返回。

    **支持格式**：PDF（调用外部解析服务）、DOCX、DOC、WPS（pandoc + LibreOffice）、TXT、XLSX、XLS、CSV

    **返回**：
    - `filename`: 原始文件名
    - `content`: 提取的文本 / markdown 内容
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="文件内容为空")

    try:
        content = parse_file(file_bytes, file.filename)
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if content is None:
        exts = "、".join(SUPPORTED_EXTENSIONS)
        raise HTTPException(
            status_code=415,
            detail=f"不支持的文件格式，支持：{exts}",
        )

    return {"filename": file.filename, "content": content}

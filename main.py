from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
import logging
import sys

from app.database import engine, Base, SessionLocal
from app.models import *
from app.seed_data import initialize_seed_data
from app.archive_service import ensure_default_configs

from app.routers import users, batches, manifests, validations, approvals, reports
from app.routers import archive as archive_router
from app.routers import sandbox as sandbox_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="交付批次验收 JSON API",
    description="""
    本地交付批次验收管理系统 API

    **核心流程**：
    1. 创建交付批次 (草稿)
    2. 导入 CSV / JSON 清单 (v1)
    3. 执行规则校验，生成逐项校验结果
    4. submitter 提交待验收 (pending_review)
    5. reviewer 驳回问题项 (partially_rejected)
    6. submitter 开始返修 (repairing) → 导入新清单 (v2)
    7. 重复 3-6 直到校验通过
    8. lead 通过验收 (approved)
    9. lead 归档 (archived) → 导出验收报告

    **认证方式**：所有 API 通过 HTTP Header `X-User-Id` 指定用户 ID（本地系统简化版）
    """,
    version="1.0.0",
)


@app.on_event("startup")
def on_startup():
    db = SessionLocal()
    try:
        initialize_seed_data(db)
        ensure_default_configs(db)
    finally:
        db.close()


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "type": "HTTPException",
                "code": exc.status_code,
                "message": exc.detail
            }
        }
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        loc = " -> ".join([str(x) for x in err.get("loc", [])])
        errors.append({
            "location": loc,
            "message": err.get("msg", ""),
            "type": err.get("type", "")
        })
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False,
            "error": {
                "type": "RequestValidationError",
                "code": 422,
                "message": "请求参数验证失败",
                "details": errors
            }
        }
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "success": False,
            "error": {
                "type": "IntegrityError",
                "code": 409,
                "message": "数据完整性冲突，可能是重复键或约束违反",
                "detail": str(exc.orig)
            }
        }
    )


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error": {
                "type": "DatabaseError",
                "code": 500,
                "message": "数据库操作异常",
                "detail": str(exc)
            }
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error": {
                "type": type(exc).__name__,
                "code": 500,
                "message": "服务器内部错误",
                "detail": str(exc)
            }
        }
    )


app.include_router(users.router)
app.include_router(batches.router)
app.include_router(manifests.router)
app.include_router(validations.router)
app.include_router(approvals.router)
app.include_router(reports.router)
app.include_router(archive_router.router)
app.include_router(sandbox_router.router)


@app.get("/", tags=["根路径"])
def root():
    return {
        "name": "交付批次验收 JSON API",
        "version": "1.0.0",
        "status": "running",
        "docs": "/docs",
        "redoc": "/redoc",
        "seed_users": [
            {"id": 1, "username": "admin", "role": "admin"},
            {"id": 2, "username": "lead_wang", "role": "lead"},
            {"id": 3, "username": "reviewer_li", "role": "reviewer"},
            {"id": 4, "username": "reviewer_zhang", "role": "reviewer"},
            {"id": 5, "username": "submitter_chen", "role": "submitter"},
            {"id": 6, "username": "submitter_zhao", "role": "submitter"},
        ],
        "usage": "在请求 Header 中添加 X-User-Id: <用户ID> 进行身份识别"
    }


@app.get("/health", tags=["系统"])
def health_check():
    return {"status": "healthy", "database": "connected"}

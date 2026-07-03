from __future__ import annotations

import math
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect

from src.semantic_registry.api.schemas import ListResponse, Pagination, StatusResponse, SyncRequest
from src.semantic_registry.config import get_settings
from src.semantic_registry.database import get_db_session
from src.semantic_registry.evaluation.cases import EvalCaseStore
from src.semantic_registry.evaluation.dashboard import aggregate_run_metrics, render_eval_dashboard
from src.semantic_registry.evaluation.models import EvalCase, EvalResult
from src.semantic_registry.evaluation.runner import EvalRunner
from src.semantic_registry.models import (
    FeedbackLog,
    QueryLog,
    SemanticConcept,
    SemanticDimension,
    SemanticEntity,
    SemanticJoinPath,
    SemanticMetric,
    SemanticTerm,
)
from src.semantic_registry.pipeline import NL2SQLPipeline, PipelineResponse
from src.semantic_registry.repair import FeedbackCapture
from src.semantic_registry.retrieval.debug_ui import router as retrieval_debug_router
from src.semantic_registry.resolver import (
    ClarificationResponse,
    SemanticQueryPlan,
    SemanticResolver,
    TermExtractor,
    load_semantic_registry,
)
from src.semantic_registry.sync import sync_all


ModelType = type[SemanticTerm]


class ResolveRequest(BaseModel):
    question: str
    domain: str | None = None


class ExtractRequest(BaseModel):
    question: str


class ClarifyRequest(BaseModel):
    question: str
    context: dict[str, Any] = Field(default_factory=dict)


class NL2SQLQueryRequest(BaseModel):
    question: str
    domain: str | None = None
    user: str = "anonymous"


class QueryFeedbackRequest(BaseModel):
    feedback_type: str
    corrected_sql: str | None = None
    user_comment: str | None = None
    user: str = "anonymous"


class EvalRunRequest(BaseModel):
    case_ids: list[str] | None = None
    domain: str | None = None


_eval_runs: dict[str, EvalResult] = {}


def row_to_dict(row: Any) -> dict[str, Any]:
    mapper = inspect(row).mapper
    return {column.key: getattr(row, column.key) for column in mapper.column_attrs}


def error_response(status_code: int, code: str, message: str, details: object | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
    )


async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    code = "not_found" if exc.status_code == 404 else "http_error"
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    details = None if isinstance(exc.detail, str) else exc.detail
    return error_response(exc.status_code, code, detail, details)


async def sqlalchemy_exception_handler(_request: Request, exc: SQLAlchemyError) -> JSONResponse:
    return error_response(500, "database_error", "Database operation failed", {"error": str(exc)})


def create_app(
    session_dependency: Callable[[], AsyncGenerator[AsyncSession, None]] = get_db_session,
) -> FastAPI:
    app = FastAPI(title="Semantic Registry API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost", "http://localhost:3000", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_exception_handler)
    app.include_router(retrieval_debug_router)
    app.state.eval_case_store = EvalCaseStore(EvalCaseStore.load_cases_from_yaml("eval_cases"))

    async def list_rows(
        model: type,
        session: AsyncSession,
        page: int,
        page_size: int,
        domain: str | None = None,
        status: str | None = None,
        from_table: str | None = None,
        to_table: str | None = None,
    ) -> ListResponse:
        stmt = select(model)
        count_stmt = select(func.count()).select_from(model)
        filters = []
        if domain and hasattr(model, "domain"):
            filters.append(model.domain == domain)
        if status and hasattr(model, "status"):
            filters.append(model.status == status)
        if from_table and hasattr(model, "from_table"):
            filters.append(model.from_table == from_table)
        if to_table and hasattr(model, "to_table"):
            filters.append(model.to_table == to_table)
        for clause in filters:
            stmt = stmt.where(clause)
            count_stmt = count_stmt.where(clause)
        total = int((await session.execute(count_stmt)).scalar_one())
        rows = (
            await session.execute(
                stmt.order_by(model.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()
        return ListResponse(
            data=jsonable_encoder([row_to_dict(row) for row in rows]),
            pagination=Pagination(
                page=page,
                page_size=page_size,
                total=total,
                total_pages=math.ceil(total / page_size) if total else 0,
            ),
        )

    async def get_by_uuid(model: type, object_id: uuid.UUID, session: AsyncSession) -> dict[str, Any]:
        row = await session.get(model, object_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{model.__tablename__} record not found")
        return jsonable_encoder(row_to_dict(row))

    @app.get("/api/v1/terms", response_model=ListResponse)
    async def list_terms(
        domain: str | None = None,
        status: str | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        session: AsyncSession = Depends(session_dependency),
    ) -> ListResponse:
        return await list_rows(SemanticTerm, session, page, page_size, domain=domain, status=status)

    @app.get("/api/v1/terms/{term_id}")
    async def get_term(term_id: uuid.UUID, session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
        return await get_by_uuid(SemanticTerm, term_id, session)

    @app.get("/api/v1/concepts", response_model=ListResponse)
    async def list_concepts(
        domain: str | None = None,
        status: str | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        session: AsyncSession = Depends(session_dependency),
    ) -> ListResponse:
        return await list_rows(SemanticConcept, session, page, page_size, domain=domain, status=status)

    @app.get("/api/v1/metrics", response_model=ListResponse)
    async def list_metrics(
        domain: str | None = None,
        status: str | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        session: AsyncSession = Depends(session_dependency),
    ) -> ListResponse:
        return await list_rows(SemanticMetric, session, page, page_size, domain=domain, status=status)

    @app.get("/api/v1/dimensions", response_model=ListResponse)
    async def list_dimensions(
        domain: str | None = None,
        status: str | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        session: AsyncSession = Depends(session_dependency),
    ) -> ListResponse:
        return await list_rows(SemanticDimension, session, page, page_size, domain=domain, status=status)

    @app.get("/api/v1/entities", response_model=ListResponse)
    async def list_entities(
        domain: str | None = None,
        status: str | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        session: AsyncSession = Depends(session_dependency),
    ) -> ListResponse:
        return await list_rows(SemanticEntity, session, page, page_size, domain=domain, status=status)

    @app.get("/api/v1/join-paths", response_model=ListResponse)
    async def list_join_paths(
        from_table: str | None = None,
        to_table: str | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        session: AsyncSession = Depends(session_dependency),
    ) -> ListResponse:
        return await list_rows(
            SemanticJoinPath,
            session,
            page,
            page_size,
            from_table=from_table,
            to_table=to_table,
        )

    @app.post("/api/v1/sync")
    async def trigger_sync(
        request: SyncRequest,
        session: AsyncSession = Depends(session_dependency),
    ) -> dict[str, Any]:
        report = await sync_all(session=session, semantic_dir=get_settings().semantic_dir, dry_run=request.dry_run)
        return report.model_dump()

    @app.post("/api/v1/resolve", response_model=SemanticQueryPlan)
    async def resolve_question(request: ResolveRequest) -> SemanticQueryPlan:
        resolver = SemanticResolver.from_semantic_dir(get_settings().semantic_dir)
        return resolver.resolve(request.question, domain=request.domain)

    @app.post("/api/v1/extract")
    async def extract_terms(request: ExtractRequest) -> list[dict[str, Any]]:
        registry_data = load_semantic_registry(get_settings().semantic_dir)
        extractor = TermExtractor(registry_data.terms)
        return [term.model_dump(mode="json") for term in extractor.extract(request.question)]

    @app.post("/api/v1/clarify", response_model=ClarificationResponse)
    async def clarify_question(request: ClarifyRequest) -> ClarificationResponse:
        resolver = SemanticResolver.from_semantic_dir(get_settings().semantic_dir)
        return resolver.build_clarification(request.question, context=request.context)

    @app.post("/api/v1/query", response_model=PipelineResponse)
    async def run_query_pipeline(
        request: NL2SQLQueryRequest,
        session: AsyncSession = Depends(session_dependency),
    ) -> PipelineResponse:
        pipeline = NL2SQLPipeline(semantic_dir=get_settings().semantic_dir)
        context = pipeline.run(request.question, domain=request.domain, user=request.user)
        if context.response is None:
            raise HTTPException(status_code=500, detail="Pipeline did not produce a response")
        session.add(_query_log_from_context(context, user=request.user))
        await session.commit()
        return context.response

    @app.post("/api/v1/eval/run", response_model=EvalResult)
    async def run_eval(request: EvalRunRequest) -> EvalResult:
        store: EvalCaseStore = app.state.eval_case_store
        if request.case_ids:
            cases = [case for case_id in request.case_ids if (case := store.get_case(case_id)) is not None]
        else:
            cases = store.list_cases(domain=request.domain)
        pipeline = NL2SQLPipeline(semantic_dir=get_settings().semantic_dir)
        result = EvalRunner().run_sql_eval(cases, pipeline)
        _eval_runs[str(uuid.uuid4())] = result
        return result

    @app.get("/api/v1/eval/runs")
    async def list_eval_runs() -> list[dict[str, Any]]:
        return [
            {
                "run_id": run_id,
                "total_cases": result.total_cases,
                "passed": result.passed,
                "failed": result.failed,
                "success_rate": result.success_rate,
            }
            for run_id, result in _eval_runs.items()
        ]

    @app.get("/api/v1/eval/runs/{run_id}", response_model=EvalResult)
    async def get_eval_run(run_id: str) -> EvalResult:
        result = _eval_runs.get(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="eval run not found")
        return result

    @app.get("/api/v1/eval/cases")
    async def list_eval_cases(
        domain: str | None = None,
        difficulty: str | None = None,
        active_only: bool = True,
        tags: list[str] | None = Query(None),
    ) -> list[dict[str, Any]]:
        store: EvalCaseStore = app.state.eval_case_store
        cases = store.list_cases(domain=domain, difficulty=difficulty, active_only=active_only, tags=tags)
        return [case.model_dump(mode="json") for case in cases]

    @app.post("/api/v1/eval/cases", response_model=EvalCase)
    async def add_eval_case(case: EvalCase) -> EvalCase:
        store: EvalCaseStore = app.state.eval_case_store
        store.add_case(case)
        return case

    @app.get("/api/v1/eval/metrics")
    async def get_eval_metrics() -> dict[str, Any]:
        return aggregate_run_metrics(_eval_runs)

    @app.get("/debug/eval")
    async def eval_dashboard():
        return render_eval_dashboard(_eval_runs)

    @app.get("/api/v1/queries", response_model=ListResponse)
    async def list_queries(
        user: str | None = None,
        domain: str | None = None,
        status: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        session: AsyncSession = Depends(session_dependency),
    ) -> ListResponse:
        stmt = select(QueryLog)
        count_stmt = select(func.count()).select_from(QueryLog)
        filters = []
        if user:
            filters.append(QueryLog.user == user)
        if domain:
            filters.append(QueryLog.domain == domain)
        if status:
            filters.append(QueryLog.status == status)
        if created_from:
            filters.append(QueryLog.created_at >= created_from)
        if created_to:
            filters.append(QueryLog.created_at <= created_to)
        for clause in filters:
            stmt = stmt.where(clause)
            count_stmt = count_stmt.where(clause)
        total = int((await session.execute(count_stmt)).scalar_one())
        rows = (
            await session.execute(
                stmt.order_by(QueryLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()
        return ListResponse(
            data=jsonable_encoder([row_to_dict(row) for row in rows]),
            pagination=Pagination(
                page=page,
                page_size=page_size,
                total=total,
                total_pages=math.ceil(total / page_size) if total else 0,
            ),
        )

    @app.get("/api/v1/queries/{query_id}")
    async def get_query_detail(query_id: str, session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
        query_log = await _get_query_log(session, query_id)
        return jsonable_encoder(row_to_dict(query_log))

    @app.post("/api/v1/queries/{query_id}/feedback")
    async def submit_query_feedback(
        query_id: str,
        request: QueryFeedbackRequest,
        session: AsyncSession = Depends(session_dependency),
    ) -> dict[str, Any]:
        query_log = await _get_query_log(session, query_id)
        corrected_sql = request.corrected_sql or query_log.generated_sql
        record = FeedbackCapture().capture(
            query_id=query_id,
            original_sql=query_log.generated_sql,
            corrected_sql=corrected_sql,
            feedback_type=request.feedback_type,
            user=request.user,
            comment=request.user_comment,
        )
        session.add(
            FeedbackLog(
                query_id=record.query_id,
                original_sql=record.original_sql,
                corrected_sql=record.corrected_sql,
                user=record.user,
                feedback_type=record.feedback_type,
                comment=record.comment,
            )
        )
        query_log.feedback_type = request.feedback_type
        query_log.corrected_sql = corrected_sql
        query_log.user_comment = request.user_comment
        query_log.reviewer = request.user
        await session.commit()
        return {"query_id": query_id, "status": "feedback_recorded"}

    @app.get("/api/v1/query/{query_id}")
    async def get_query(query_id: str, session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
        query_log = await _get_query_log(session, query_id)
        return jsonable_encoder(row_to_dict(query_log))

    @app.get("/api/v1/status", response_model=StatusResponse)
    async def status(session: AsyncSession = Depends(session_dependency)) -> StatusResponse:
        counts: dict[str, int] = {}
        for key, model in {
            "terms": SemanticTerm,
            "concepts": SemanticConcept,
            "metrics": SemanticMetric,
            "dimensions": SemanticDimension,
            "entities": SemanticEntity,
            "join_paths": SemanticJoinPath,
        }.items():
            counts[key] = int((await session.execute(select(func.count()).select_from(model))).scalar_one())
        return StatusResponse(db_connected=True, counts=counts)

    return app


def _query_log_from_context(context: Any, user: str) -> QueryLog:
    selected = context.selected_sql
    response = context.response
    status = "clarification" if context.requires_clarification else "failure"
    if response is not None and response.validation_status == "pass" and context.error is None:
        status = "success"
    return QueryLog(
        query_id=context.query_id,
        question=context.question,
        domain=context.domain,
        generated_sql=response.generated_sql if response else (selected.sql if selected else ""),
        semantic_plan_json=context.semantic_plan.model_dump(mode="json") if context.semantic_plan else {},
        validation_results_json=context.validation_results,
        execution_results_json=None,
        metadata_snapshot_version=None,
        model_version=get_settings().model_dump().get("llm_model") if hasattr(get_settings(), "model_dump") else None,
        status=status,
        user=user,
    )


async def _get_query_log(session: AsyncSession, query_id: str) -> QueryLog:
    query_log = (
        await session.execute(select(QueryLog).where(QueryLog.query_id == query_id))
    ).scalar_one_or_none()
    if query_log is None:
        raise HTTPException(status_code=404, detail="query record not found")
    return query_log


app = create_app()

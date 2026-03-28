from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core.hub_core import _admin_case_detail, _create_audit_log, _delete_training_case, _render_admin, _validate_csrf_token
from ..config.settings import TrainingHubSettings
from .admin_utils import request_meta as _request_meta


def register_admin_case_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.get("/admin/cases/{case_db_id}", response_class=HTMLResponse)
    async def admin_case_detail_page(request: Request, case_db_id: int):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")

        details = await run_in_threadpool(_admin_case_detail, settings.database_path, case_db_id)
        if details is None:
            raise HTTPException(status_code=404, detail="Case not found.")

        context = {
            "request": request,
            "current_user": user,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "details": details,
        }
        return app.state.templates.TemplateResponse(request, "case_detail.html", context)

    @app.post("/admin/cases/{case_db_id}/delete", response_class=HTMLResponse)
    async def admin_delete_case(
        request: Request,
        case_db_id: int,
        return_to: str = Form(default="admin"),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        _validate_csrf_token(request, csrf_token)

        deleted = await run_in_threadpool(_delete_training_case, settings.database_path, case_db_id)
        if deleted is None:
            if return_to == "detail":
                raise HTTPException(status_code=404, detail="Case not found.")
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Case not found.",
                status_code=404,
            )

        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="case.delete",
            target_type="case",
            target_id=case_db_id,
            details=f"Deleted case {deleted['case_id']}.",
            source_ip=source_ip,
            user_agent=user_agent,
        )

        notice = f"Deleted case {deleted['case_id']}."
        if return_to == "detail":
            return RedirectResponse(url=f"/admin?notice={quote_plus(notice)}", status_code=303)
        return await run_in_threadpool(
            _render_admin,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=notice,
        )


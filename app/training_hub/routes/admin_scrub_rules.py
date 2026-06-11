from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config.settings import TrainingHubSettings
from ..core.hub_core import (
    _create_audit_log,
    _create_content_scrub_rule,
    _delete_content_scrub_rule,
    _render_admin,
    _validate_csrf_token,
)
from .admin_utils import request_meta as _request_meta


def register_admin_scrub_rule_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    def _require_admin(request: Request):
        user = request.state.user
        if user is None:
            return None, RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        return user, None

    @app.get("/admin/scrub-rules", response_class=HTMLResponse)
    async def admin_scrub_rules_page(request: Request, notice: str = "", error: str = ""):
        user, redirect = _require_admin(request)
        if redirect is not None:
            return redirect
        return await run_in_threadpool(
            _render_admin,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=notice,
            error=error,
            page="scrub_rules",
        )

    @app.post("/admin/scrub-rules", response_class=HTMLResponse)
    async def admin_create_scrub_rule(
        request: Request,
        pattern_text: str = Form(...),
        match_mode: str = Form(...),
        use_regex: str = Form(default=""),
        csrf_token: str = Form(...),
    ):
        user, redirect = _require_admin(request)
        if redirect is not None:
            return redirect
        _validate_csrf_token(request, csrf_token)

        try:
            created_rule = await run_in_threadpool(
                _create_content_scrub_rule,
                settings.database_path,
                actor_user_id=int(user["id"]),
                pattern_text=pattern_text,
                match_mode=match_mode,
                use_regex=str(use_regex or "").strip().lower() in {"1", "true", "yes", "on"},
            )
        except ValueError as exc:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(exc),
                status_code=400,
                page="scrub_rules",
            )

        source_ip, user_agent = _request_meta(request, settings)
        details = (
            f"Created content scrub rule #{int(created_rule['id'])} "
            f"mode={str(created_rule['match_mode'])} regex={bool(created_rule['use_regex'])}."
        )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="content_scrub_rule.create",
            target_type="content_scrub_rule",
            target_id=int(created_rule["id"]),
            details=details,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return RedirectResponse(url=f"/admin/scrub-rules?notice={quote_plus(details)}", status_code=303)

    @app.post("/admin/scrub-rules/{rule_id}/delete", response_class=HTMLResponse)
    async def admin_delete_scrub_rule(request: Request, rule_id: int, csrf_token: str = Form(...)):
        user, redirect = _require_admin(request)
        if redirect is not None:
            return redirect
        _validate_csrf_token(request, csrf_token)

        deleted_rule = await run_in_threadpool(_delete_content_scrub_rule, settings.database_path, int(rule_id))
        if deleted_rule is None:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Content scrub rule not found.",
                status_code=404,
                page="scrub_rules",
            )

        source_ip, user_agent = _request_meta(request, settings)
        details = (
            f"Deleted content scrub rule #{int(deleted_rule['id'])} "
            f"mode={str(deleted_rule['match_mode'])} regex={bool(deleted_rule['use_regex'])}."
        )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="content_scrub_rule.delete",
            target_type="content_scrub_rule",
            target_id=int(deleted_rule["id"]),
            details=details,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return RedirectResponse(url=f"/admin/scrub-rules?notice={quote_plus(details)}", status_code=303)

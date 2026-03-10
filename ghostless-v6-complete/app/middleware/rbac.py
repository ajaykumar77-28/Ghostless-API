"""
Ghostless API — Role-Based Access Control (v6)

Implements fine-grained RBAC on top of the existing API key system.

Roles:
  - worker       : can submit tasks, view own scores/earnings
  - reviewer     : can read any worker's score; flag/unflag fraud events
  - admin        : full access; can modify tenants, bulk-suspend workers
  - super_admin  : cross-tenant access (Ghostless staff only)

Permission strings follow the pattern:
  <resource>:<action>

Examples:
  workers:read, workers:suspend, scores:read, fraud:review,
  ledger:read, ledger:clawback, tenants:create, tenants:delete

Permissions are checked via require_permission() dependency.

Usage:
    @router.get("/admin/workers")
    async def list_workers(
        auth: AuthContext = Depends(require_auth),
        _: None = Depends(require_permission("workers:read")),
    ):
        ...
"""
from __future__ import annotations

from functools import lru_cache
from typing import FrozenSet, Set

from fastapi import Depends, HTTPException, status

from app.middleware.auth import AuthContext, require_auth


# ─── Role → Permission matrix ─────────────────────────────────────────────────

ROLE_PERMISSIONS: dict[str, FrozenSet[str]] = {
    "worker": frozenset({
        "tasks:submit",
        "scores:read:own",
        "earnings:read:own",
        "hub:read",
        "hub:write",
        "appeals:create",
    }),
    "reviewer": frozenset({
        "tasks:submit",
        "scores:read:own",
        "scores:read:any",
        "earnings:read:own",
        "fraud:read",
        "fraud:review",
        "hub:read",
        "hub:write",
        "appeals:read",
    }),
    "admin": frozenset({
        "tasks:submit",
        "tasks:read",
        "scores:read:own",
        "scores:read:any",
        "scores:recalculate",
        "earnings:read:own",
        "earnings:read:any",
        "earnings:clawback",
        "fraud:read",
        "fraud:review",
        "fraud:action",
        "workers:read",
        "workers:suspend",
        "workers:ban",
        "workers:unban",
        "workers:shadow_ban",
        "tenants:read",
        "tenants:update",
        "hub:read",
        "hub:write",
        "hub:moderate",
        "ledger:read",
        "ledger:clawback",
        "appeals:read",
        "appeals:resolve",
        "admin_audit:read",
        "baselines:read",
        "baselines:rollback",
        "events:read",
    }),
    "super_admin": frozenset({
        # All admin permissions + cross-tenant + destructive ops
        "tasks:submit",
        "tasks:read",
        "scores:read:own",
        "scores:read:any",
        "scores:recalculate",
        "earnings:read:own",
        "earnings:read:any",
        "earnings:clawback",
        "fraud:read",
        "fraud:review",
        "fraud:action",
        "workers:read",
        "workers:suspend",
        "workers:ban",
        "workers:unban",
        "workers:shadow_ban",
        "tenants:read",
        "tenants:create",
        "tenants:update",
        "tenants:delete",
        "hub:read",
        "hub:write",
        "hub:moderate",
        "ledger:read",
        "ledger:clawback",
        "appeals:read",
        "appeals:resolve",
        "admin_audit:read",
        "baselines:read",
        "baselines:rollback",
        "events:read",
        "cross_tenant:read",
        "feature_flags:write",
        "feature_flags:read",
    }),
}


def get_permissions(role: str) -> FrozenSet[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: str, permission: str) -> bool:
    return permission in get_permissions(role)


# ─── FastAPI dependency ───────────────────────────────────────────────────────

def require_permission(permission: str):
    """
    Returns a FastAPI dependency that raises 403 if the auth context
    does not have the required permission.

    Usage:
        @router.post("/admin/workers/{id}/suspend")
        async def suspend_worker(
            auth: AuthContext = Depends(require_auth),
            _: None = Depends(require_permission("workers:suspend")),
        ):
    """
    async def _check(auth: AuthContext = Depends(require_auth)):
        role = getattr(auth, "role", "worker")
        if not has_permission(role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error":      "permission_denied",
                    "required":   permission,
                    "your_role":  role,
                    "message":    f"Role '{role}' cannot perform '{permission}'.",
                },
            )
    return _check


def require_admin():
    """Shorthand: require role = admin or super_admin."""
    return require_permission("workers:suspend")


def require_super_admin():
    """Shorthand: require super_admin role."""
    return require_permission("cross_tenant:read")

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import AuthUser, Role, create_access_token, get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    user_id: str
    password: str


#: Demo users. Passwords are intentionally shared for the demo environment;
#: replace this store with the real identity provider before any real use.
USER_STORE: dict[str, dict[str, str]] = {
    "admin.user": {
        "password": "procure2pay",
        "role": "admin",
        "name": "Admin",
        "title": "P2P Administrator",
    },
    "ap.user": {
        "password": "procure2pay",
        "role": "ap_ops",
        "name": "AP Operations",
        "title": "Accounts Payable",
    },
    "treasury.user": {
        "password": "procure2pay",
        "role": "treasury",
        "name": "Treasury",
        "title": "Cash & Payments",
    },
    "procurement.user": {
        "password": "procure2pay",
        "role": "procurement",
        "name": "Procurement",
        "title": "Category Management",
    },
    "exec.user": {
        "password": "procure2pay",
        "role": "exec",
        "name": "Executive",
        "title": "Finance Leadership",
    },
}


@router.post("/login")
def login(body: LoginRequest):
    import os
    if os.getenv("DEMO_AUTH_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        raise HTTPException(status_code=404, detail="Demo login is disabled; use Microsoft Entra authentication.")
    record = USER_STORE.get(body.user_id.strip().lower())
    if not record or record["password"] != body.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    role: Role = record["role"]  # type: ignore[assignment]
    token = create_access_token(record["name"], role)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"name": record["name"], "role": role, "title": record["title"]},
    }


@router.get("/me")
def me(user: AuthUser = Depends(get_current_user)):
    return {"name": user.name, "role": user.role}

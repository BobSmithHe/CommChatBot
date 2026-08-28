from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...infra.database import User, get_db
from ...infra.security import create_access_token, hash_password, verify_password
from ..deps import current_user
from ..schemas import LoginRequest, RegisterRequest

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
def register(req: RegisterRequest, db: Session = Depends(get_db)) -> dict:
    exists = db.query(User).filter((User.username == req.username) | (User.email == req.email)).first()
    if exists:
        raise HTTPException(status_code=400, detail="Username or email already exists")
    user = User(username=req.username, email=req.email, password_hash=hash_password(req.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "email": user.email, "is_active": user.is_active}


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)) -> dict:
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {"access_token": create_access_token(str(user.id)), "token_type": "bearer"}


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"id": user.id, "username": user.username, "email": user.email}


from fastapi import FastAPI, Depends, HTTPException, Form
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.openapi.utils import get_openapi
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
import string, random
from fastapi import Request
from app.database import get_db, engine
from app.models import Base, ShortenedLink, User
from app.auth import hash_password, verify_password, create_access_token, decode_access_token
import asyncio
from datetime import timedelta
from sqlalchemy import delete

app = FastAPI()
security = HTTPBearer(auto_error=False) 
INACTIVITY_DAYS_LIMIT = 7 

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="Shorty API",
        version="1.0.0",
        description="Link Shortener with Auth",
        routes=app.routes,
    )

    openapi_schema["components"]["securitySchemes"] = {
        "HTTPBearer": {
            "type": "http",
            "scheme": "bearer"
        }
    }

    if "/links/shorten" in openapi_schema["paths"]:
        for method in openapi_schema["paths"]["/links/shorten"].values():
            method["security"] = [{"HTTPBearer": []}]

    app.openapi_schema = openapi_schema
    return app.openapi_schema



app.openapi = custom_openapi

class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

@app.post("/register")
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    hashed = hash_password(data.password)
    user = User(email=data.username, hashed_password=hashed)
    db.add(user)
    await db.commit()
    return {"message": "User registered successfully"}

@app.post("/login")
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == request.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(request.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": str(user.id)})
    return {"access_token": token, "token_type": "bearer"}

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
):
    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    user_id = int(payload.get("sub"))
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

class ShortenLinkRequest(BaseModel):
    original_url: str = Field(..., example="https://example.com")
    custom_alias: Optional[str] = Field(None, example="mycustom")
    expires_at: Optional[datetime] = Field(None, example="2025-03-30T23:59")

def generate_short_code(length=6):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> Optional[User]:
    auth_header = request.headers.get("authorization")
    if not auth_header:
        return None

    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    payload = decode_access_token(token)
    if not payload:
        return None

    user_id = int(payload.get("sub"))
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()

@app.post("/links/shorten")
async def shorten_link(
    request_data: ShortenLinkRequest,
    request: Request,  
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional)
):
    short_code = request_data.custom_alias or generate_short_code()

    existing = await db.execute(
        select(ShortenedLink).where(ShortenedLink.short_code == short_code)
    )
    if existing.scalar():
        raise HTTPException(status_code=400, detail="Alias is already taken")

    new_link = ShortenedLink(
        original_url=request_data.original_url,
        short_code=short_code,
        expires_at=request_data.expires_at,
        user_id=current_user.id if current_user else None
    )
    db.add(new_link)
    await db.commit()
    await db.refresh(new_link)

    return {
        "short_url": str(request.base_url) + short_code,
        "created_by": current_user.email if current_user else "anonymous"
    }



@app.get("/{short_code}", include_in_schema=False)
async def redirect_to_original_url(short_code: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ShortenedLink).where(ShortenedLink.short_code == short_code))
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Short URL not found")
    if link.expires_at and link.expires_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Link has expired")
    link.click_count += 1
    link.last_used_at = datetime.utcnow()
    await db.commit()
    return RedirectResponse(url=link.original_url)

@app.get("/links/{short_code}")
async def get_original_url(short_code: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ShortenedLink).where(ShortenedLink.short_code == short_code))
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Short URL not found")
    return {"original_url": link.original_url}

@app.get("/links/{short_code}/stats")
async def get_link_stats(
    short_code: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(select(ShortenedLink).where(ShortenedLink.short_code == short_code))
    link = result.scalar_one_or_none()
    if not link or link.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return {
        "original_url": link.original_url,
        "created_at": link.created_at,
        "click_count": link.click_count,
        "last_used_at": link.last_used_at
    }

@app.get("/me/links")
async def get_my_links(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(select(ShortenedLink).where(ShortenedLink.user_id == current_user.id))
    links = result.scalars().all()
    return [
        {
            "short_code": link.short_code,
            "original_url": link.original_url,
            "click_count": link.click_count
        } for link in links
    ]

@app.delete("/links/{short_code}")
async def delete_short_link(
    short_code: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(select(ShortenedLink).where(ShortenedLink.short_code == short_code))
    link = result.scalar_one_or_none()
    if not link or link.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    await db.delete(link)
    await db.commit()
    return {"message": f"Short URL '{short_code}' deleted"}

@app.put("/links/{short_code}")
async def update_short_link(
    short_code: str,
    request: Request,
    new_original_url: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(select(ShortenedLink).where(ShortenedLink.short_code == short_code))
    link = result.scalar_one_or_none()
    if not link or link.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    link.original_url = new_original_url
    await db.commit()
    
    base_url = str(request.base_url).rstrip("/")  
    return {"short_url": f"{base_url}/{short_code}"}


async def cleanup_old_links_task():
    while True:
        await asyncio.sleep(3600) 
        async with engine.begin() as conn:
            session = AsyncSession(bind=conn)
            threshold_date = datetime.utcnow() - timedelta(days=N_DAYS_INACTIVE)
            await session.execute(
                delete(ShortenedLink).where(
                    ShortenedLink.last_used_at < threshold_date
                )
            )
            await session.commit()

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    asyncio.create_task(cleanup_old_links_task())
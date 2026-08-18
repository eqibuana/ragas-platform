from fastapi import APIRouter, HTTPException

from .. import platform_client, schemas

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=schemas.LoginResponse)
async def login(body: schemas.LoginRequest):
    try:
        result = await platform_client.login(body.email, body.password)
    except platform_client.PlatformAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return schemas.LoginResponse(**result)

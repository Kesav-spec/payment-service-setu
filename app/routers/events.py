from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.schemas import EventCreate, EventIngestResponse
from app.services.events import ingest_event

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventIngestResponse)
async def create_event(
    payload: EventCreate,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> EventIngestResponse:
    result = await ingest_event(session, payload)
    # 201 for a genuinely new event, 200 for an exact resend -- no new
    # resource was created the second time.
    response.status_code = status.HTTP_201_CREATED if result.status == "accepted" else status.HTTP_200_OK
    return result

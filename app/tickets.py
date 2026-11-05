from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ticket


@dataclass(frozen=True, slots=True)
class NewTicket:
    event_id: str
    ticket_id: str
    customer_email: str
    message: str
    reply_url: str


async def insert_ticket_if_new(session: AsyncSession, ticket: NewTicket) -> bool:
    statement = (
        insert(Ticket)
        .values(
            event_id=ticket.event_id,
            ticket_id=ticket.ticket_id,
            customer_email=ticket.customer_email,
            message=ticket.message,
            reply_url=ticket.reply_url,
        )
        .on_conflict_do_nothing(index_elements=[Ticket.event_id])
        .returning(Ticket.id)
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none() is not None

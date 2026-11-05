from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TicketStatus(StrEnum):
    RECEIVED = "received"
    GENERATING = "generating"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    SENDING = "sending"
    SENT = "sent"
    REJECTED = "rejected"
    DRAFT_FAILED = "draft_failed"
    SEND_FAILED = "send_failed"
    DELIVERY_UNKNOWN = "delivery_unknown"


_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in TicketStatus)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_valid"),
        Index(
            "ix_tickets_generation_queue",
            "next_attempt_at",
            "id",
            postgresql_where=text("status IN ('received', 'generating')"),
        ),
        Index(
            "ix_tickets_delivery_queue",
            "next_attempt_at",
            "id",
            postgresql_where=text("status IN ('approved', 'sending')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, unique=True)
    ticket_id: Mapped[str] = mapped_column(Text)
    customer_email: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    reply_url: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=TicketStatus.RECEIVED.value)
    draft_text: Mapped[str | None] = mapped_column(Text)
    draft_sources: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    tags: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LLMRateLimit(Base):
    __tablename__ = "llm_rate_limit"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    next_slot: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

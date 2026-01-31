from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime

from database import Base


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    telegram_user_id = Column(Integer, nullable=False)

    status = Column(String(20), nullable=False)

    title = Column(String(255), nullable=False)
    description = Column(String(1024))

    deadline_at = Column(DateTime, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

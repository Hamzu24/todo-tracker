from __future__ import annotations

from pydantic import BaseModel


class TaskIn(BaseModel):
    date: str
    headline: str
    context: str = ""


class TaskOut(BaseModel):
    id: int
    date: str
    headline: str
    context: str

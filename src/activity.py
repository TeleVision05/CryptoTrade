from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any


@dataclass
class ActivityEvent:
  id: int
  timestamp: str
  stage: str  # scan | found | evaluate | skip | trade | idle | error | info
  message: str
  chain: str | None = None
  pair: str | None = None
  spread_bps: float | None = None
  detail: str | None = None
  outcome: str | None = None  # checking | not_worth | worth_it | executed | cooldown | none

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


class ActivityBuffer:
  """Ring buffer of engine activity for the live dashboard monitor."""

  def __init__(self, maxlen: int = 100) -> None:
    self._events: deque[ActivityEvent] = deque(maxlen=maxlen)
    self._lock = Lock()
    self._next_id = 1
    self._latest_stage = "idle"
    self._latest_headline = "Waiting for first scan…"
    self._cycle = 0

  def push(
    self,
    stage: str,
    message: str,
    *,
    chain: str | None = None,
    pair: str | None = None,
    spread_bps: float | None = None,
    detail: str | None = None,
    outcome: str | None = None,
  ) -> ActivityEvent:
    with self._lock:
      event = ActivityEvent(
        id=self._next_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        stage=stage,
        message=message,
        chain=chain,
        pair=pair,
        spread_bps=spread_bps,
        detail=detail,
        outcome=outcome,
      )
      self._next_id += 1
      self._events.appendleft(event)
      self._latest_stage = stage
      self._latest_headline = message
      if stage == "scan":
        self._cycle += 1
      return event

  def snapshot(self, limit: int = 40) -> dict[str, Any]:
    with self._lock:
      events = [e.to_dict() for e in list(self._events)[:limit]]
      return {
        "cycle": self._cycle,
        "stage": self._latest_stage,
        "headline": self._latest_headline,
        "events": events,
      }

from typing import Literal

from pydantic import BaseModel


class MarketSnapshot(BaseModel):
    symbol: str
    price: float | None
    change24h: float | None
    volume24h: float | None
    high24h: float | None
    low24h: float | None
    source: str
    eventTime: int | None
    receivedAt: int
    latencyMs: int | None
    status: Literal["live", "stale", "unavailable"]


class Level(BaseModel):
    price: float
    quantity: float
    total: float


class BookSnapshot(BaseModel):
    symbol: str
    bids: list[Level]
    asks: list[Level]
    spread: float
    imbalance: float
    eventTime: int | None
    receivedAt: int
    status: str
    source: str = "Binance"


class Trade(BaseModel):
    id: int
    time: int
    price: float
    quantity: float
    side: Literal["buy", "sell"]


class Candle(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

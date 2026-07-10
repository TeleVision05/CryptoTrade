from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenInfo:
    symbol: str
    address: str
    decimals: int


CHAIN_TOKENS: dict[str, dict[str, TokenInfo]] = {
    "base": {
        "WETH": TokenInfo("WETH", "0x4200000000000000000000000000000000000006", 18),
        "USDC": TokenInfo("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
        "USDT": TokenInfo("USDT", "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", 6),
    },
    "arbitrum": {
        "WETH": TokenInfo("WETH", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18),
        "USDC": TokenInfo("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
        "USDT": TokenInfo("USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6),
    },
}

CHAIN_ID_TO_NAME: dict[int, str] = {
    8453: "base",
    42161: "arbitrum",
}


def get_token(chain: str, symbol: str) -> TokenInfo:
    try:
        return CHAIN_TOKENS[chain][symbol]
    except KeyError as exc:
        raise ValueError(f"Unknown token {symbol} on chain {chain}") from exc


def to_raw_amount(amount: float, decimals: int) -> int:
    return int(amount * (10**decimals))


def from_raw_amount(amount: int, decimals: int) -> float:
    return amount / (10**decimals)

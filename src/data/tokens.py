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
        "VIRTUAL": TokenInfo("VIRTUAL", "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b", 18),
        "AERO": TokenInfo("AERO", "0x940181a94A35A7119F4537E62754e5ceF2E62daC", 18),
        # Long-tail / higher-vol Base majors — more chance of temporary mispricing
        "BRETT": TokenInfo("BRETT", "0x532f27101965dd16442E59d40670FaF5eBB142E4", 18),
        "DEGEN": TokenInfo("DEGEN", "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", 18),
        "TOSHI": TokenInfo("TOSHI", "0xAC1Bd2486aAf3B5C0fc3Fd868558b082a531B2B4", 18),
        "cbBTC": TokenInfo("cbBTC", "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf", 8),
    },
    "arbitrum": {
        "WETH": TokenInfo("WETH", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", 18),
        "USDC": TokenInfo("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
        "USDT": TokenInfo("USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6),
        "ARB": TokenInfo("ARB", "0x912CE59144191C1204E64559FE8253a0e49E6548", 18),
        "GMX": TokenInfo("GMX", "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a", 18),
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


def has_token(chain: str, symbol: str) -> bool:
    return symbol in CHAIN_TOKENS.get(chain, {})


def to_raw_amount(amount: float, decimals: int) -> int:
    return int(amount * (10**decimals))


def from_raw_amount(amount: int, decimals: int) -> float:
    return amount / (10**decimals)

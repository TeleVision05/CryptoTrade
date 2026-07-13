from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from web3 import Web3

from src.config import EnvSettings
from src.data.tokens import CHAIN_TOKENS, from_raw_amount, get_token, to_raw_amount
from src.models import DexPair, Opportunity
from src.quoting.oneinch_roundtrip import (
  STABLES,
  parse_pair_symbols,
  quote_amount_from_usd,
  quote_out_to_usd,
)
from src.rpc import ResilientWeb3

# Uniswap V3 QuoterV2
UNI_QUOTER_V2: dict[str, str] = {
  "base": "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a",
  "arbitrum": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
}

# PancakeSwap V3 QuoterV2 (same deploy addr on Base / Arbitrum per PCS docs)
PANCAKE_QUOTER_V2: dict[str, str] = {
  "base": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
  "arbitrum": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
}

# Aerodrome Slipstream QuoterV2 (Base only) — uses tickSpacing, not fee.
AERO_QUOTER_V2: dict[str, str] = {
  "base": "0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0",
}

QUOTER_COMPATIBLE_DEXES = {"uniswap", "aerodrome", "pancakeswap"}

POOL_ABI = [
  {
    "inputs": [],
    "name": "fee",
    "outputs": [{"name": "", "type": "uint24"}],
    "stateMutability": "view",
    "type": "function",
  },
  {
    "inputs": [],
    "name": "tickSpacing",
    "outputs": [{"name": "", "type": "int24"}],
    "stateMutability": "view",
    "type": "function",
  },
  {
    "inputs": [],
    "name": "token0",
    "outputs": [{"name": "", "type": "address"}],
    "stateMutability": "view",
    "type": "function",
  },
  {
    "inputs": [],
    "name": "token1",
    "outputs": [{"name": "", "type": "address"}],
    "stateMutability": "view",
    "type": "function",
  },
  {
    "inputs": [],
    "name": "slot0",
    "outputs": [
      {"name": "sqrtPriceX96", "type": "uint160"},
      {"name": "tick", "type": "int24"},
      {"name": "observationIndex", "type": "uint16"},
      {"name": "observationCardinality", "type": "uint16"},
      {"name": "observationCardinalityNext", "type": "uint16"},
      {"name": "unlocked", "type": "bool"},
    ],
    "stateMutability": "view",
    "type": "function",
  },
  {
    "inputs": [
      {"name": "amountIn", "type": "uint256"},
      {"name": "tokenIn", "type": "address"},
    ],
    "name": "getAmountOut",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function",
  },
]

UNI_QUOTER_ABI = [
  {
    "inputs": [
      {
        "components": [
          {"name": "tokenIn", "type": "address"},
          {"name": "tokenOut", "type": "address"},
          {"name": "amountIn", "type": "uint256"},
          {"name": "fee", "type": "uint24"},
          {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "name": "params",
        "type": "tuple",
      }
    ],
    "name": "quoteExactInputSingle",
    "outputs": [
      {"name": "amountOut", "type": "uint256"},
      {"name": "sqrtPriceX96After", "type": "uint160"},
      {"name": "initializedTicksCrossed", "type": "uint32"},
      {"name": "gasEstimate", "type": "uint256"},
    ],
    "stateMutability": "nonpayable",
    "type": "function",
  }
]

AERO_QUOTER_ABI = [
  {
    "inputs": [
      {
        "components": [
          {"name": "tokenIn", "type": "address"},
          {"name": "tokenOut", "type": "address"},
          {"name": "amountIn", "type": "uint256"},
          {"name": "tickSpacing", "type": "int24"},
          {"name": "sqrtPriceLimitX96", "type": "uint160"},
        ],
        "name": "params",
        "type": "tuple",
      }
    ],
    "name": "quoteExactInputSingle",
    "outputs": [
      {"name": "amountOut", "type": "uint256"},
      {"name": "sqrtPriceX96After", "type": "uint160"},
      {"name": "initializedTicksCrossed", "type": "uint32"},
      {"name": "gasEstimate", "type": "uint256"},
    ],
    "stateMutability": "nonpayable",
    "type": "function",
  }
]


@dataclass
class OnchainRoundTrip:
  quote_symbol: str
  base_symbol: str
  quote_amount: float
  base_amount: float
  quote_out: float
  sell_output_usd: float
  quote_amount_raw: int
  base_amount_raw: int
  buy_fee: int
  sell_fee: int
  buy_gas_estimate: int
  sell_gas_estimate: int
  effective_spread_bps: float
  quote_source: str = "onchain_quoter"


@dataclass
class _PoolMeta:
  kind: str  # uni_v3 | pancake_v3 | aero_slipstream | aero_classic
  fee_or_spacing: int
  token0: str
  token1: str


def _is_uniswap_v2(pair: DexPair) -> bool:
  labels = {label.lower() for label in pair.labels}
  return "v2" in labels and "v3" not in labels


def _known_addresses(chain: str) -> dict[str, str]:
  """symbol(upper) -> address for all registered tokens on chain."""
  return {sym.upper(): info.address for sym, info in CHAIN_TOKENS.get(chain, {}).items()}


def _resolve_base_quote_addrs(
  chain: str,
  base_symbol: str,
  quote_symbol: str,
  buy: DexPair,
  sell: DexPair,
  buy_meta: _PoolMeta,
  sell_meta: _PoolMeta,
) -> tuple[str, str] | None:
  """
  Pick base/quote addresses that exist on BOTH pools.
  Prefer registry addresses — reject pools that don't hold the tokens we trade.
  """
  known = _known_addresses(chain)
  known_base = known.get(base_symbol.upper(), "")
  known_quote = known.get(quote_symbol.upper(), "")
  buy_tokens = {buy_meta.token0.lower(), buy_meta.token1.lower()}
  sell_tokens = {sell_meta.token0.lower(), sell_meta.token1.lower()}
  shared = buy_tokens & sell_tokens
  if len(shared) < 2:
    return None

  # Hard require registry tokens when we know them (kills mislabeled / empty-tier ghosts).
  if known_base and known_quote:
    kb, kq = known_base.lower(), known_quote.lower()
    if kb not in shared or kq not in shared:
      return None
    if kb == kq:
      return None
    return known_base, known_quote

  candidates_base = [
    known_base,
    buy.base_token_address,
    sell.base_token_address,
  ]
  candidates_quote = [
    known_quote,
    buy.quote_token_address,
    sell.quote_token_address,
  ]

  def pick(cands: list[str], exclude: str | None = None) -> str | None:
    for addr in cands:
      if not addr:
        continue
      low = addr.lower()
      if exclude and low == exclude.lower():
        continue
      if low in shared:
        return addr
    return None

  base_addr = pick(candidates_base)
  quote_addr = pick(candidates_quote, exclude=base_addr)
  if base_addr and quote_addr and base_addr.lower() != quote_addr.lower():
    return base_addr, quote_addr

  shared_list = list(shared)
  if len(shared_list) != 2:
    return None
  t0, t1 = shared_list[0], shared_list[1]
  if known_base.lower() == t0:
    return t0, t1
  if known_base.lower() == t1:
    return t1, t0
  if known_quote.lower() == t0:
    return t1, t0
  if known_quote.lower() == t1:
    return t0, t1
  return t0, t1


def _sqrt_price_to_usd_per_base(
  sqrt_price_x96: int,
  token0: str,
  token1: str,
  base_addr: str,
  quote_addr: str,
  base_decimals: int,
  quote_decimals: int,
) -> float | None:
  """Convert Uniswap/Aero slot0 sqrtPriceX96 into USD-per-base when quote is ~$1 or WETH."""
  if sqrt_price_x96 <= 0:
    return None
  ratio = (sqrt_price_x96 / (2**96)) ** 2  # raw token1 per token0
  t0 = token0.lower()
  t1 = token1.lower()
  base = base_addr.lower()
  quote = quote_addr.lower()
  if t0 == base and t1 == quote:
    # quote per base in human units
    return ratio * (10 ** (base_decimals - quote_decimals))
  if t0 == quote and t1 == base:
    human = ratio * (10 ** (quote_decimals - base_decimals))
    if human <= 0:
      return None
    return 1.0 / human
  return None


class OnchainPoolQuoter:
  """
  Spatial arb quotes via on-chain quoters / pool getAmountOut.
  Supports Uniswap V3 + Pancake V3 + Aerodrome (Slipstream + classic) on Base.
  Public RPC eth_call only — no private key / no broadcast.
  """

  def __init__(self, env: EnvSettings) -> None:
    self._env = env
    self._clients: dict[str, ResilientWeb3] = {}
    self._meta_cache: dict[str, _PoolMeta] = {}
    self._health_cache: dict[str, bool] = {}
    self._mid_cache: dict[str, tuple[float, float]] = {}  # key -> (mid, monotonic_ts)
    self.last_error: str | None = None
    self._executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="onchain")

  def supports_pair(self, pair: DexPair) -> bool:
    dex = pair.dex_id.lower()
    if dex not in QUOTER_COMPATIBLE_DEXES:
      return False
    if dex in {"uniswap", "pancakeswap"} and _is_uniswap_v2(pair):
      return False
    if pair.chain == "arbitrum" and dex == "aerodrome":
      return False
    return True

  def supports_opportunity(self, opportunity: Opportunity) -> bool:
    if opportunity.chain not in {"base", "arbitrum"}:
      return False
    return self.supports_pair(opportunity.buy_pair) and self.supports_pair(
      opportunity.sell_pair
    )

  def _client(self, chain: str) -> ResilientWeb3:
    if chain not in self._clients:
      preferred = self._env.rpc_url_for(chain)
      self._clients[chain] = ResilientWeb3(chain, preferred=preferred)
    return self._clients[chain]

  def _read_pool_meta(self, client: ResilientWeb3, pair: DexPair) -> _PoolMeta | None:
    key = f"{pair.chain}:{pair.pair_address.lower()}"
    if key in self._meta_cache:
      return self._meta_cache[key]

    if pair.dex_id.lower() in {"uniswap", "pancakeswap"} and _is_uniswap_v2(pair):
      return None

    def _load() -> _PoolMeta | None:
      w3 = client.connect()
      pool = w3.eth.contract(
        address=Web3.to_checksum_address(pair.pair_address),
        abi=POOL_ABI,
      )
      token0 = pool.functions.token0().call()
      token1 = pool.functions.token1().call()

      dex = pair.dex_id.lower()
      tick_spacing = None
      fee = None
      try:
        fee = int(pool.functions.fee().call())
      except Exception:
        pass
      try:
        tick_spacing = int(pool.functions.tickSpacing().call())
      except Exception:
        pass

      if fee is None and dex in {"uniswap", "pancakeswap"} and pair.fee_bps > 0:
        fee = int(round(pair.fee_bps * 100))

      if dex == "aerodrome" and tick_spacing is not None:
        return _PoolMeta("aero_slipstream", tick_spacing, token0, token1)
      if dex == "aerodrome":
        pool.functions.getAmountOut(1, Web3.to_checksum_address(token0)).call()
        return _PoolMeta("aero_classic", 0, token0, token1)
      if dex == "pancakeswap" and fee is not None and fee > 0:
        return _PoolMeta("pancake_v3", fee, token0, token1)
      if dex == "uniswap" and fee is not None and fee > 0:
        return _PoolMeta("uni_v3", fee, token0, token1)
      return None

    try:
      meta = client.call(_load)
    except Exception as exc:
      self.last_error = f"meta {pair.pair_address[:10]}: {exc}"
      return None
    if meta is None:
      return None
    self._meta_cache[key] = meta
    return meta

  def pool_holds_registry_tokens(
    self, chain: str, pair: DexPair, base_symbol: str, quote_symbol: str
  ) -> bool:
    """True when on-chain pool tokens match our registry base+quote."""
    known = _known_addresses(chain)
    kb = known.get(base_symbol.upper(), "").lower()
    kq = known.get(quote_symbol.upper(), "").lower()
    if not kb or not kq:
      return True
    client = self._client(chain)
    meta = self._read_pool_meta(client, pair)
    if meta is None:
      return False
    tokens = {meta.token0.lower(), meta.token1.lower()}
    return kb in tokens and kq in tokens

  def read_mid_price_usd(
    self,
    chain: str,
    pair: DexPair,
    base_symbol: str,
    quote_symbol: str,
  ) -> float | None:
    """On-chain mid from slot0 (CLAMM) — far better than DexScreener for ranking."""
    import time

    cache_key = f"{chain}:{pair.pair_address.lower()}"
    cached = self._mid_cache.get(cache_key)
    if cached and time.monotonic() - cached[1] < 2.0:
      return cached[0]

    client = self._client(chain)
    meta = self._read_pool_meta(client, pair)
    if meta is None or meta.kind == "aero_classic":
      return pair.price_usd if pair.price_usd > 0 else None

    known = _known_addresses(chain)
    base_addr = known.get(base_symbol.upper(), pair.base_token_address)
    quote_addr = known.get(quote_symbol.upper(), pair.quote_token_address)
    if not base_addr or not quote_addr:
      return None
    try:
      base_decimals = get_token(chain, base_symbol).decimals
      quote_decimals = get_token(chain, quote_symbol).decimals
    except ValueError:
      base_decimals = 18
      quote_decimals = 6 if quote_symbol.upper() in STABLES else 18

    def _load() -> float | None:
      w3 = client.connect()
      pool = w3.eth.contract(
        address=Web3.to_checksum_address(pair.pair_address),
        abi=POOL_ABI,
      )
      slot0 = pool.functions.slot0().call()
      sqrt_price = int(slot0[0])
      mid = _sqrt_price_to_usd_per_base(
        sqrt_price,
        meta.token0,
        meta.token1,
        base_addr,
        quote_addr,
        base_decimals,
        quote_decimals,
      )
      if mid is None or mid <= 0:
        return None
      # TOKEN/WETH: slot0 gives base-per-quote or similar in quote units (WETH).
      # Convert to USD using a rough ETH price when quote is WETH.
      if quote_symbol.upper() == "WETH" and base_symbol.upper() != "WETH":
        return mid * 1800.0  # base USD ≈ (base/WETH) * ETH_USD
      if quote_symbol.upper() in STABLES:
        return mid
      if base_symbol.upper() == "WETH" and quote_symbol.upper() in STABLES:
        return mid
      return mid

    try:
      mid = client.call(_load)
    except Exception:
      return pair.price_usd if pair.price_usd > 0 else None
    if mid is None or mid <= 0:
      return None
    self._mid_cache[cache_key] = (mid, time.monotonic())
    return mid

  def is_pool_healthy(
    self,
    chain: str,
    pair: DexPair,
    base_symbol: str,
    quote_symbol: str,
    probe_usd: float = 15.0,
  ) -> bool:
    """
    Reject concentrated-liquidity ghosts: TVL exists but no depth at spot.
    Deep liquid majors skip the probe (slot0 mid is enough).
    """
    key = f"{chain}:{pair.pair_address.lower()}:health"
    if key in self._health_cache:
      return self._health_cache[key]

    if not self.pool_holds_registry_tokens(chain, pair, base_symbol, quote_symbol):
      self._health_cache[key] = False
      return False

    # Deep / low-fee majors almost always have spot depth — skip slow probe.
    if (
      pair.liquidity_usd >= 200_000
      and pair.fee_bps <= 5.5
      and base_symbol.upper() in {"WETH", "CBBTC", "CBBTC"}
      and quote_symbol.upper() in STABLES | {"WETH"}
    ):
      self._health_cache[key] = True
      return True
    if pair.liquidity_usd >= 1_000_000 and pair.fee_bps <= 30:
      self._health_cache[key] = True
      return True

    client = self._client(chain)
    meta = self._read_pool_meta(client, pair)
    if meta is None:
      self._health_cache[key] = False
      return False

    known = _known_addresses(chain)
    base_addr = known.get(base_symbol.upper(), "")
    quote_addr = known.get(quote_symbol.upper(), "")
    if not base_addr or not quote_addr:
      self._health_cache[key] = True
      return True

    try:
      quote_decimals = get_token(chain, quote_symbol).decimals
      base_decimals = get_token(chain, base_symbol).decimals
    except ValueError:
      self._health_cache[key] = False
      return False

    mid = self.read_mid_price_usd(chain, pair, base_symbol, quote_symbol) or pair.price_usd
    if mid <= 0:
      self._health_cache[key] = False
      return False

    if quote_symbol.upper() in STABLES:
      quote_amount = probe_usd
    elif quote_symbol.upper() == "WETH":
      quote_amount = probe_usd / 1800.0
    else:
      quote_amount = probe_usd / max(mid, 1e-9)

    quote_raw = to_raw_amount(quote_amount, quote_decimals)
    if quote_raw <= 0:
      self._health_cache[key] = False
      return False

    try:
      base_raw, _, _ = self._quote_leg(
        client, chain, pair, quote_addr, base_addr, quote_raw
      )
    except Exception:
      self._health_cache[key] = False
      return False

    if base_raw <= 0:
      self._health_cache[key] = False
      return False

    base_out = from_raw_amount(base_raw, base_decimals)
    recovered = base_out * mid
    ok = recovered >= probe_usd * 0.90
    self._health_cache[key] = ok
    return ok

  def _quote_leg(
    self,
    client: ResilientWeb3,
    chain: str,
    pair: DexPair,
    token_in: str,
    token_out: str,
    amount_in: int,
  ) -> tuple[int, int, int]:
    """Returns (amount_out, fee_or_spacing, gas_estimate)."""
    meta = self._read_pool_meta(client, pair)
    if meta is None:
      raise RuntimeError(f"Unsupported pool {pair.dex_id} {pair.pair_address}")

    pool_tokens = {meta.token0.lower(), meta.token1.lower()}
    if token_in.lower() not in pool_tokens or token_out.lower() not in pool_tokens:
      raise RuntimeError("Token mismatch vs pool")

    def _do() -> tuple[int, int, int]:
      w3 = client.connect()
      if meta.kind == "aero_classic":
        pool = w3.eth.contract(
          address=Web3.to_checksum_address(pair.pair_address),
          abi=POOL_ABI,
        )
        out = int(
          pool.functions.getAmountOut(
            int(amount_in),
            Web3.to_checksum_address(token_in),
          ).call()
        )
        return out, 0, 80_000

      if meta.kind == "aero_slipstream":
        quoter_addr = AERO_QUOTER_V2.get(chain)
        if not quoter_addr:
          raise RuntimeError("No Aerodrome quoter on this chain")
        quoter = w3.eth.contract(
          address=Web3.to_checksum_address(quoter_addr),
          abi=AERO_QUOTER_ABI,
        )
        params = (
          Web3.to_checksum_address(token_in),
          Web3.to_checksum_address(token_out),
          int(amount_in),
          int(meta.fee_or_spacing),
          0,
        )
        amount_out, _sqrt, _ticks, gas_est = quoter.functions.quoteExactInputSingle(
          params
        ).call()
        return int(amount_out), int(meta.fee_or_spacing), int(gas_est)

      if meta.kind == "pancake_v3":
        quoter_addr = PANCAKE_QUOTER_V2.get(chain)
      else:
        quoter_addr = UNI_QUOTER_V2.get(chain)
      if not quoter_addr:
        raise RuntimeError(f"No QuoterV2 for {meta.kind} on {chain}")
      quoter = w3.eth.contract(
        address=Web3.to_checksum_address(quoter_addr),
        abi=UNI_QUOTER_ABI,
      )
      params = (
        Web3.to_checksum_address(token_in),
        Web3.to_checksum_address(token_out),
        int(amount_in),
        int(meta.fee_or_spacing),
        0,
      )
      amount_out, _sqrt, _ticks, gas_est = quoter.functions.quoteExactInputSingle(
        params
      ).call()
      return int(amount_out), int(meta.fee_or_spacing), int(gas_est)

    return client.call(_do)

  async def quote_round_trip(
    self,
    opportunity: Opportunity,
    trade_size_usd: float,
  ) -> OnchainRoundTrip | None:
    if trade_size_usd <= 0 or opportunity.strategy != "spatial":
      return None
    if not self.supports_opportunity(opportunity):
      self.last_error = "unsupported opportunity"
      return None
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
      self._executor,
      self._quote_round_trip_sync,
      opportunity,
      trade_size_usd,
    )

  async def quote_round_trips_parallel(
    self,
    jobs: list[tuple[Opportunity, float]],
  ) -> list[OnchainRoundTrip | None]:
    """Quote many routes concurrently (bounded thread pool)."""
    if not jobs:
      return []
    loop = asyncio.get_running_loop()
    futs = [
      loop.run_in_executor(self._executor, self._quote_round_trip_sync, opp, size)
      for opp, size in jobs
    ]
    return list(await asyncio.gather(*futs, return_exceptions=False))

  def _quote_round_trip_sync(
    self,
    opportunity: Opportunity,
    trade_size_usd: float,
  ) -> OnchainRoundTrip | None:
    chain = opportunity.chain
    try:
      client = self._client(chain)
    except Exception as exc:
      self.last_error = str(exc)
      return None

    base_symbol, quote_symbol = parse_pair_symbols(opportunity.pair_label)
    buy = opportunity.buy_pair
    sell = opportunity.sell_pair

    buy_meta = self._read_pool_meta(client, buy)
    sell_meta = self._read_pool_meta(client, sell)
    if buy_meta is None or sell_meta is None:
      self.last_error = "missing pool meta"
      return None

    resolved = _resolve_base_quote_addrs(
      chain, base_symbol, quote_symbol, buy, sell, buy_meta, sell_meta
    )
    if resolved is None:
      self.last_error = "buy/sell pools do not share token pair"
      return None
    base_addr, quote_addr = resolved

    try:
      base_decimals = get_token(chain, base_symbol).decimals
      quote_decimals = get_token(chain, quote_symbol).decimals
    except ValueError:
      base_decimals = 18
      quote_decimals = 6 if quote_symbol.upper() in STABLES else 18

    eth_usd = 1800.0
    if base_symbol == "WETH":
      eth_usd = max(opportunity.buy_price_usd, opportunity.sell_price_usd, 1.0)

    quote_amount = quote_amount_from_usd(trade_size_usd, quote_symbol, opportunity)
    if quote_symbol.upper() == "WETH":
      quote_amount = trade_size_usd / eth_usd
    elif quote_symbol.upper() not in STABLES and not opportunity.pair_label.startswith(
      "WETH/"
    ):
      quote_amount = trade_size_usd / eth_usd
    quote_amount_raw = to_raw_amount(quote_amount, quote_decimals)
    if quote_amount_raw <= 0:
      return None

    try:
      base_raw, buy_fee, buy_gas = self._quote_leg(
        client,
        chain,
        buy,
        quote_addr,
        base_addr,
        quote_amount_raw,
      )
      if base_raw <= 0:
        self.last_error = "buy leg returned 0"
        return None
      quote_out_raw, sell_fee, sell_gas = self._quote_leg(
        client,
        chain,
        sell,
        base_addr,
        quote_addr,
        base_raw,
      )
    except Exception as exc:
      self.last_error = str(exc)
      return None

    if quote_out_raw <= 0:
      self.last_error = "sell leg returned 0"
      return None

    base_amount = from_raw_amount(base_raw, base_decimals)
    quote_out = from_raw_amount(quote_out_raw, quote_decimals)
    sell_output_usd = quote_out_to_usd(quote_out, quote_symbol, opportunity, eth_usd)
    effective = ((sell_output_usd - trade_size_usd) / trade_size_usd) * 10_000

    return OnchainRoundTrip(
      quote_symbol=quote_symbol,
      base_symbol=base_symbol,
      quote_amount=quote_amount,
      base_amount=base_amount,
      quote_out=quote_out,
      sell_output_usd=sell_output_usd,
      quote_amount_raw=quote_amount_raw,
      base_amount_raw=base_raw,
      buy_fee=buy_fee,
      sell_fee=sell_fee,
      buy_gas_estimate=buy_gas,
      sell_gas_estimate=sell_gas,
      effective_spread_bps=effective,
    )

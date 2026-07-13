from __future__ import annotations

from web3 import Web3

from src.config import EnvSettings
from src.models import DexPair
from src.quoting.onchain_quoter import POOL_ABI, QUOTER_COMPATIBLE_DEXES
from src.rpc import ResilientWeb3


class PoolFeeEnricher:
  """Overwrite DexScreener-inferred fees with on-chain pool.fee() when available."""

  def __init__(self, env: EnvSettings, max_workers: int = 4) -> None:
    self._env = env
    self._clients: dict[str, ResilientWeb3] = {}
    self._cache: dict[str, float] = {}
    self._max_workers = max_workers

  def _client(self, chain: str) -> ResilientWeb3 | None:
    if chain not in self._clients:
      preferred = self._env.rpc_url_for(chain)
      if not preferred and chain not in {"base", "arbitrum"}:
        return None
      try:
        self._clients[chain] = ResilientWeb3(chain, preferred=preferred)
      except Exception:
        return None
    return self._clients[chain]

  def _read_fee_bps(self, chain: str, pair: DexPair) -> float | None:
    key = f"{chain}:{pair.pair_address.lower()}"
    if key in self._cache:
      return self._cache[key]
    if pair.dex_id.lower() not in QUOTER_COMPATIBLE_DEXES:
      return None
    client = self._client(chain)
    if client is None:
      return None

    def _load() -> float | None:
      w3 = client.connect()
      pool = w3.eth.contract(
        address=Web3.to_checksum_address(pair.pair_address),
        abi=POOL_ABI,
      )
      fee = int(pool.functions.fee().call())
      bps = fee / 100.0
      return bps if bps > 0 else None

    try:
      bps = client.call(_load, retries=3)
    except Exception:
      return None
    if bps is None:
      return None
    self._cache[key] = bps
    return bps

  async def enrich_pairs(self, chain: str, pairs: list[DexPair]) -> list[DexPair]:
    import asyncio

    return await asyncio.to_thread(self._enrich_sync, chain, pairs)

  def _enrich_sync(self, chain: str, pairs: list[DexPair]) -> list[DexPair]:
    # Sequential to avoid blasting public RPCs into 429s.
    out: list[DexPair] = []
    for pair in pairs:
      onchain = self._read_fee_bps(chain, pair)
      if onchain is not None and onchain > 0:
        out.append(pair.model_copy(update={"fee_bps": onchain}))
      else:
        out.append(pair)
    return out

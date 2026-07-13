from __future__ import annotations

from src.config import AppConfig, EnvSettings
from src.cost_model import CostModel
from src.data.oneinch import OneInchClient
from src.data.tokens import get_token
from src.execution.swap_runner import SwapRunner
from src.ledger import Ledger
from src.models import PaperTrade, TradeEvaluation
from src.quoting.oneinch_roundtrip import quote_spatial_round_trip
from src.quoting.onchain_quoter import OnchainPoolQuoter
from src.quoting.pool_quotes import pool_round_trip_usd
from src.simulator import PaperExecutor


class OnlineDryRunExecutor:
  """
  Online dry-run: never broadcasts. No wallet required for onchain/pool_direct.

  - onchain: Uniswap QuoterV2 on exact buy/sell fee tiers (closest to live).
  - pool_direct: local AMM model.
  - oneinch: aggregator + optional eth_call.
  """

  def __init__(
    self,
    config: AppConfig,
    env: EnvSettings,
    ledger: Ledger,
    oneinch: OneInchClient,
    cost_model: CostModel | None = None,
    onchain_quoter: OnchainPoolQuoter | None = None,
  ) -> None:
    self._config = config
    self._env = env
    self._ledger = ledger
    self._oneinch = oneinch
    self._cost_model = cost_model or CostModel(config)
    self._onchain = onchain_quoter or OnchainPoolQuoter(env)
    self._web3_clients: dict = {}

  @property
  def ready(self) -> bool:
    if self._config.simulate_quote_mode == "oneinch":
      return bool(self._oneinch.enabled)
    return True

  def _get_web3(self, chain: str):
    from web3 import Web3

    rpc_url = self._env.rpc_url_for(chain)
    if not rpc_url:
      raise RuntimeError(f"No RPC URL configured for chain {chain}")
    if chain not in self._web3_clients:
      self._web3_clients[chain] = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
      if not self._web3_clients[chain].is_connected():
        raise RuntimeError(f"RPC not reachable for {chain}: {rpc_url}")
    return self._web3_clients[chain]

  async def execute(self, evaluation: TradeEvaluation) -> PaperTrade:
    mode = self._config.simulate_quote_mode
    if mode == "oneinch":
      return await self._execute_oneinch(evaluation)
    if mode == "onchain":
      return await self._execute_onchain(evaluation)
    return await self._execute_pool_direct(evaluation)

  async def _execute_onchain(self, evaluation: TradeEvaluation) -> PaperTrade:
    opportunity = evaluation.opportunity
    if not self._onchain.supports_opportunity(opportunity):
      raise RuntimeError("Pools not QuoterV2-compatible")

    # Prefer a fresh QuoterV2 print. Only fall back to the evaluation quote when
    # the RPC call fails entirely (not when the edge simply moved away).
    rt = await self._onchain.quote_round_trip(opportunity, evaluation.trade_size_usd)
    cached = (
      evaluation.sell_output_usd
      if evaluation.quote_source.startswith("onchain_quoter") and evaluation.sell_output_usd > 0
      else None
    )
    if rt is not None and rt.sell_output_usd >= evaluation.trade_size_usd:
      sell_output_usd = rt.sell_output_usd
      quote_source = "onchain_quoter"
    elif rt is None and cached is not None and cached >= evaluation.trade_size_usd:
      sell_output_usd = cached
      quote_source = "onchain_quoter_cached"
    else:
      raise RuntimeError(
        "On-chain edge gone on re-quote "
        f"(fresh={rt.sell_output_usd if rt else None}, cached={cached})"
      )

    gas_cost_usd = max(
      self._cost_model.gas_cost_usd(opportunity.chain),
      evaluation.gas_cost_usd,
    )
    gross_pnl = sell_output_usd - evaluation.trade_size_usd
    fees = self._cost_model.latency_cost_usd(evaluation.trade_size_usd)
    net_pnl = gross_pnl - gas_cost_usd - fees

    if net_pnl < self._config.min_net_profit_usd:
      raise RuntimeError(
        f"Simulated net ${net_pnl:.4f} below min ${self._config.min_net_profit_usd:.2f} "
        f"(onchain out ${sell_output_usd:.4f})"
      )

    sim_eval = evaluation.model_copy(
      update={
        "sell_output_usd": sell_output_usd,
        "gross_pnl_usd": gross_pnl,
        "fees_usd": fees,
        "gas_cost_usd": gas_cost_usd,
        "mev_haircut_usd": 0.0,
        "net_pnl_usd": net_pnl,
        "quote_source": quote_source,
      }
    )
    return await PaperExecutor(self._ledger).execute(sim_eval)

  async def _execute_pool_direct(self, evaluation: TradeEvaluation) -> PaperTrade:
    opportunity = evaluation.opportunity
    quote = pool_round_trip_usd(opportunity, evaluation.trade_size_usd)
    if quote is None:
      raise RuntimeError("Pool-direct round-trip quote failed")

    sell_output_usd = quote.sell_output_usd
    gas_cost_usd = self._cost_model.gas_cost_usd(opportunity.chain)
    gross_pnl, fees, _, mev_haircut, _ = self._cost_model.compute_net_pnl(
      opportunity=opportunity,
      trade_size_usd=evaluation.trade_size_usd,
      buy_input_usd=evaluation.trade_size_usd,
      sell_output_usd=sell_output_usd,
      gross_spread_bps=opportunity.gross_spread_bps,
    )
    mev_haircut = 0.0
    net_pnl = gross_pnl - gas_cost_usd - fees

    if net_pnl < self._config.min_net_profit_usd:
      raise RuntimeError(
        f"Simulated net ${net_pnl:.4f} below min ${self._config.min_net_profit_usd:.2f} "
        f"(pool_direct out ${sell_output_usd:.4f}, gas ${gas_cost_usd:.4f}, "
        f"eff {quote.effective_spread_bps:+.1f}bps)"
      )

    sim_eval = evaluation.model_copy(
      update={
        "sell_output_usd": sell_output_usd,
        "gross_pnl_usd": gross_pnl,
        "fees_usd": fees,
        "gas_cost_usd": gas_cost_usd,
        "mev_haircut_usd": mev_haircut,
        "net_pnl_usd": net_pnl,
        "quote_source": "pool_direct",
      }
    )
    return await PaperExecutor(self._ledger).execute(sim_eval)

  async def _execute_oneinch(self, evaluation: TradeEvaluation) -> PaperTrade:
    if not self._oneinch.enabled:
      raise RuntimeError("Simulate mode (oneinch) requires ONEINCH_API_KEY")

    opportunity = evaluation.opportunity
    chain = opportunity.chain
    chain_id = self._config.chains[chain].chain_id
    from_address = self._env.resolve_simulate_from_address()
    web3 = self._get_web3(chain)
    runner = SwapRunner(web3, account=None, slippage_bps=self._config.slippage_bps)

    rt = await quote_spatial_round_trip(
      oneinch=self._oneinch,
      config=self._config,
      opportunity=opportunity,
      trade_size_usd=evaluation.trade_size_usd,
    )
    if rt is None:
      raise RuntimeError("1inch round-trip quote failed")

    base_token = get_token(chain, rt.base_symbol)
    quote_token = get_token(chain, rt.quote_symbol)

    buy_tx = await self._oneinch.get_swap_transaction(
      chain_id=chain_id,
      src_token=quote_token.address,
      dst_token=base_token.address,
      amount_raw=rt.quote_amount_raw,
      from_address=from_address,
      src_decimals=quote_token.decimals,
      dst_decimals=base_token.decimals,
      slippage_bps=self._config.slippage_bps,
      disable_estimate=True,
    )
    if buy_tx is None:
      raise RuntimeError("1inch buy swap build failed")

    sell_tx = await self._oneinch.get_swap_transaction(
      chain_id=chain_id,
      src_token=base_token.address,
      dst_token=quote_token.address,
      amount_raw=rt.base_amount_raw,
      from_address=from_address,
      src_decimals=base_token.decimals,
      dst_decimals=quote_token.decimals,
      slippage_bps=self._config.slippage_bps,
      disable_estimate=True,
    )
    if sell_tx is None:
      raise RuntimeError("1inch sell swap build failed")

    buy_gas = int(buy_tx.get("gas") or 350_000)
    sell_gas = int(sell_tx.get("gas") or 350_000)
    try:
      buy_gas = await runner.simulate_call(buy_tx, from_address)
      sell_gas = await runner.simulate_call(sell_tx, from_address)
    except Exception:
      pass

    eth_price = 1800.0
    if rt.base_symbol == "WETH":
      eth_price = max(opportunity.buy_price_usd, opportunity.sell_price_usd, 1.0)
    gas_cost_usd = runner.gas_cost_usd(buy_gas + sell_gas, eth_price_usd=eth_price)

    sell_output_usd = rt.sell_output_usd
    gross_pnl, fees, _, mev_haircut, _ = self._cost_model.compute_net_pnl(
      opportunity=opportunity,
      trade_size_usd=evaluation.trade_size_usd,
      buy_input_usd=evaluation.trade_size_usd,
      sell_output_usd=sell_output_usd,
      gross_spread_bps=opportunity.gross_spread_bps,
    )
    mev_haircut = 0.0
    net_pnl = gross_pnl - gas_cost_usd - fees

    if net_pnl < self._config.min_net_profit_usd:
      raise RuntimeError(
        f"Simulated net ${net_pnl:.4f} below min ${self._config.min_net_profit_usd:.2f} "
        f"(1inch out ${sell_output_usd:.4f}, gas ${gas_cost_usd:.4f})"
      )

    sim_eval = evaluation.model_copy(
      update={
        "sell_output_usd": sell_output_usd,
        "gross_pnl_usd": gross_pnl,
        "fees_usd": fees,
        "gas_cost_usd": gas_cost_usd,
        "mev_haircut_usd": mev_haircut,
        "net_pnl_usd": net_pnl,
        "quote_source": "1inch_simulate",
      }
    )
    return await PaperExecutor(self._ledger).execute(sim_eval)

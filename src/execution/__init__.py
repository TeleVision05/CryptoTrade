from __future__ import annotations

from src.config import AppConfig, EnvSettings
from src.cost_model import CostModel
from src.data.oneinch import OneInchClient
from src.data.tokens import from_raw_amount, get_token
from src.execution.simulate import OnlineDryRunExecutor
from src.execution.swap_runner import SwapRunner
from src.ledger import Ledger
from src.models import PaperTrade, TradeEvaluation
from src.quoting.onchain_quoter import OnchainPoolQuoter
from src.simulator import PaperExecutor


class LiveExecutor:
  """
  Live on-chain execution via 1inch swap transactions.
  Requires WALLET_PRIVATE_KEY, chain RPC URLs, and ONEINCH_API_KEY.
  """

  def __init__(
    self,
    config: AppConfig,
    env: EnvSettings,
    ledger: Ledger,
    oneinch: OneInchClient,
  ) -> None:
    self._config = config
    self._env = env
    self._ledger = ledger
    self._oneinch = oneinch
    self._account = None
    self._web3_clients: dict = {}

  @property
  def ready(self) -> bool:
    return bool(
      self._config.live_enabled
      and self._env.wallet_private_key
      and self._oneinch.enabled
    )

  def _get_web3(self, chain: str):
    from web3 import Web3

    rpc_url = self._env.rpc_url_for(chain)
    if not rpc_url:
      raise RuntimeError(f"No RPC URL configured for chain {chain}")
    if chain not in self._web3_clients:
      self._web3_clients[chain] = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    return self._web3_clients[chain]

  def _get_account(self):
    if self._account is None:
      from eth_account import Account

      if not self._env.wallet_private_key:
        raise RuntimeError("WALLET_PRIVATE_KEY is required for live execution")
      self._account = Account.from_key(self._env.wallet_private_key)
    return self._account

  async def execute(self, evaluation: TradeEvaluation) -> PaperTrade:
    if not self._config.live_enabled:
      raise RuntimeError(
        "Live trading is locked (live_enabled=false). Keep simulate until "
        "1inch round-trips are repeatedly profitable, then unlock explicitly."
      )
    if not self.ready:
      raise RuntimeError("Live execution requires WALLET_PRIVATE_KEY and ONEINCH_API_KEY")

    if evaluation.trade_size_usd > self._config.live_max_trade_usd:
      raise RuntimeError(
        f"Trade size ${evaluation.trade_size_usd:.2f} exceeds live_max_trade_usd "
        f"${self._config.live_max_trade_usd:.2f}"
      )

    wins = await self._ledger.count_winning_trades()
    if wins < self._config.live_require_simulate_wins:
      raise RuntimeError(
        f"Live blocked: need {self._config.live_require_simulate_wins} winning "
        f"simulate trades first (have {wins})"
      )

    from src.quoting.oneinch_roundtrip import quote_spatial_round_trip

    opportunity = evaluation.opportunity
    # Fresh 1inch check — refuse broadcast if round-trip is not clearly profitable.
    rt = await quote_spatial_round_trip(
      self._oneinch, self._config, opportunity, evaluation.trade_size_usd
    )
    if rt is None:
      raise RuntimeError("Live blocked: fresh 1inch round-trip quote failed")
    edge_bps = ((rt.sell_output_usd - evaluation.trade_size_usd) / evaluation.trade_size_usd) * 10000
    if edge_bps < 10:
      raise RuntimeError(
        f"Live blocked: fresh 1inch edge {edge_bps:+.1f}bps < +10bps "
        "(would lose money on-chain)"
      )

    chain = opportunity.chain
    chain_id = self._config.chains[chain].chain_id
    base_symbol, quote_symbol = opportunity.pair_label.split("→")[0].split("/")
    base_symbol, quote_symbol = base_symbol.strip(), quote_symbol.strip()

    base_token = get_token(chain, base_symbol)
    quote_token = get_token(chain, quote_symbol)
    account = self._get_account()
    web3 = self._get_web3(chain)
    runner = SwapRunner(web3, account, self._config.slippage_bps)

    buy_amount_raw = rt.quote_amount_raw
    quote_before = runner.token_balance(quote_token.address, account.address)
    base_before = runner.token_balance(base_token.address, account.address)

    buy_tx = await self._oneinch.get_swap_transaction(
      chain_id=chain_id,
      src_token=quote_token.address,
      dst_token=base_token.address,
      amount_raw=buy_amount_raw,
      from_address=account.address,
      src_decimals=quote_token.decimals,
      dst_decimals=base_token.decimals,
      slippage_bps=self._config.slippage_bps,
    )
    if buy_tx is None:
      raise RuntimeError("Failed to build buy swap transaction")

    await runner.ensure_allowance(quote_token.address, buy_tx["to"], buy_amount_raw)
    await runner.send_and_wait(buy_tx)

    base_after_buy = runner.token_balance(base_token.address, account.address)
    base_received_raw = base_after_buy - base_before
    if base_received_raw <= 0:
      raise RuntimeError(f"Buy succeeded but {base_symbol} balance did not increase")

    sell_tx = await self._oneinch.get_swap_transaction(
      chain_id=chain_id,
      src_token=base_token.address,
      dst_token=quote_token.address,
      amount_raw=base_received_raw,
      from_address=account.address,
      src_decimals=base_token.decimals,
      dst_decimals=quote_token.decimals,
      slippage_bps=self._config.slippage_bps,
    )
    if sell_tx is None:
      raise RuntimeError(
        f"Buy succeeded but sell swap build failed — wallet holds "
        f"{from_raw_amount(base_received_raw, base_token.decimals):.6f} {base_symbol} "
        "(manual unwind needed)"
      )

    try:
      await runner.ensure_allowance(base_token.address, sell_tx["to"], base_received_raw)
      await runner.send_and_wait(sell_tx)
    except Exception as exc:
      raise RuntimeError(
        f"Sell failed after buy — stuck with "
        f"{from_raw_amount(base_received_raw, base_token.decimals):.6f} {base_symbol}: {exc}"
      ) from exc

    quote_after = runner.token_balance(quote_token.address, account.address)
    actual_pnl = from_raw_amount(quote_after - quote_before, quote_token.decimals)

    live_eval = evaluation.model_copy(
      update={
        "sell_output_usd": evaluation.trade_size_usd + actual_pnl,
        "gross_pnl_usd": actual_pnl,
        "net_pnl_usd": actual_pnl,
        "quote_source": "1inch_live",
      }
    )
    return await PaperExecutor(self._ledger).execute(live_eval)


def create_executor(
  config: AppConfig,
  env: EnvSettings,
  ledger: Ledger,
  oneinch: OneInchClient,
  cost_model: CostModel | None = None,
  onchain_quoter: OnchainPoolQuoter | None = None,
) -> PaperExecutor | OnlineDryRunExecutor | LiveExecutor:
  mode = config.execution_mode
  if mode == "live":
    if not config.live_enabled:
      raise RuntimeError(
        "execution_mode=live but live_enabled=false — refusing to risk real funds. "
        "Run onchain simulate until QuoterV2 edges are repeatedly positive, then set live_enabled: true."
      )
    return LiveExecutor(config, env, ledger, oneinch)
  if mode == "simulate":
    return OnlineDryRunExecutor(
      config, env, ledger, oneinch, cost_model, onchain_quoter=onchain_quoter
    )
  return PaperExecutor(ledger)

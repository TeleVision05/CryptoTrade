from __future__ import annotations

import asyncio
from typing import Any

from eth_account.signers.local import LocalAccount
from web3 import Web3
from web3.types import TxReceipt

ERC20_ABI = [
  {
    "constant": True,
    "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
    "name": "allowance",
    "outputs": [{"name": "", "type": "uint256"}],
    "type": "function",
  },
  {
    "constant": False,
    "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
    "name": "approve",
    "outputs": [{"name": "", "type": "bool"}],
    "type": "function",
  },
  {
    "constant": True,
    "inputs": [{"name": "account", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "type": "function",
  },
]


class SwapRunner:
  """Sign/broadcast swaps, eth_call simulation, and ERC20 approvals."""

  def __init__(self, web3: Web3, account: LocalAccount | None, slippage_bps: float) -> None:
    self._web3 = web3
    self._account = account
    self._slippage_bps = slippage_bps

  def token_balance(self, token_address: str, owner: str) -> int:
    contract = self._web3.eth.contract(
      address=Web3.to_checksum_address(token_address),
      abi=ERC20_ABI,
    )
    return int(contract.functions.balanceOf(Web3.to_checksum_address(owner)).call())

  async def simulate_call(self, swap_tx: dict[str, Any], from_address: str) -> int:
    """eth_call + estimate_gas. Returns gas estimate. Raises on revert."""
    return await asyncio.to_thread(self._simulate_sync, swap_tx, from_address)

  def _simulate_sync(self, swap_tx: dict[str, Any], from_address: str) -> int:
    tx = {
      "from": Web3.to_checksum_address(from_address),
      "to": Web3.to_checksum_address(swap_tx["to"]),
      "data": swap_tx["data"],
      "value": int(swap_tx.get("value") or 0),
    }
    # eth_call fails if the tx would revert
    self._web3.eth.call(tx)
    try:
      return int(self._web3.eth.estimate_gas(tx))
    except Exception:
      if swap_tx.get("gas"):
        return int(swap_tx["gas"])
      return 350_000

  async def ensure_allowance(
    self,
    token_address: str,
    spender: str,
    amount_raw: int,
  ) -> None:
    if self._account is None:
      raise RuntimeError("Wallet account required for approvals")
    await asyncio.to_thread(self._ensure_allowance_sync, token_address, spender, amount_raw)

  def _ensure_allowance_sync(self, token_address: str, spender: str, amount_raw: int) -> None:
    assert self._account is not None
    token = self._web3.eth.contract(
      address=Web3.to_checksum_address(token_address),
      abi=ERC20_ABI,
    )
    owner = self._account.address
    spender_cs = Web3.to_checksum_address(spender)
    current = int(token.functions.allowance(owner, spender_cs).call())
    if current >= amount_raw:
      return
    tx = token.functions.approve(spender_cs, 2**256 - 1).build_transaction(
      {
        "from": owner,
        "nonce": self._web3.eth.get_transaction_count(owner),
        "gasPrice": self._web3.eth.gas_price,
        "chainId": self._web3.eth.chain_id,
      }
    )
    if "gas" not in tx:
      tx["gas"] = self._web3.eth.estimate_gas(tx)
    signed = self._account.sign_transaction(tx)
    tx_hash = self._web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = self._web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
      raise RuntimeError(f"Approve failed: {tx_hash.hex()}")

  async def send_and_wait(self, swap_tx: dict[str, Any]) -> TxReceipt:
    if self._account is None:
      raise RuntimeError("Wallet account required for broadcast")
    return await asyncio.to_thread(self._send_sync, swap_tx)

  def _send_sync(self, swap_tx: dict[str, Any]) -> TxReceipt:
    assert self._account is not None
    tx = {
      "from": self._account.address,
      "to": Web3.to_checksum_address(swap_tx["to"]),
      "data": swap_tx["data"],
      "value": int(swap_tx.get("value") or 0),
      "chainId": int(swap_tx["chainId"]),
      "nonce": self._web3.eth.get_transaction_count(self._account.address),
    }
    if swap_tx.get("gas"):
      tx["gas"] = int(swap_tx["gas"])
    else:
      tx["gas"] = self._web3.eth.estimate_gas(tx)

    if swap_tx.get("gasPrice"):
      tx["gasPrice"] = int(swap_tx["gasPrice"])
    else:
      tx["gasPrice"] = self._web3.eth.gas_price

    signed = self._account.sign_transaction(tx)
    tx_hash = self._web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = self._web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
      raise RuntimeError(f"Transaction reverted: {tx_hash.hex()}")
    return receipt

  def gas_cost_usd(self, gas_units: int, eth_price_usd: float = 1800.0) -> float:
    gas_price = int(self._web3.eth.gas_price)
    eth_spent = (gas_units * gas_price) / 1e18
    return eth_spent * eth_price_usd

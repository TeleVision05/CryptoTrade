from __future__ import annotations


def american_to_decimal(american: float | int | None) -> float | None:
  if american is None:
    return None
  try:
    a = float(american)
  except (TypeError, ValueError):
    return None
  if a == 0:
    return None
  if a > 0:
    return 1.0 + a / 100.0
  return 1.0 + 100.0 / abs(a)


def decimal_to_american(decimal: float) -> int | None:
  if decimal is None or decimal <= 1.0:
    return None
  if decimal >= 2.0:
    return int(round((decimal - 1.0) * 100))
  return int(round(-100.0 / (decimal - 1.0)))


def implied_prob(decimal: float) -> float:
  return 1.0 / decimal if decimal and decimal > 1.0 else 0.0


def two_way_arb(
  decimal_a: float,
  decimal_b: float,
  stake_total: float = 100.0,
) -> dict | None:
  """Return surebet sizing if 1/da + 1/db < 1."""
  if decimal_a <= 1.0 or decimal_b <= 1.0:
    return None
  inv = implied_prob(decimal_a) + implied_prob(decimal_b)
  if inv >= 0.999:  # no edge (allow tiny float room)
    return None
  profit_pct = (1.0 - inv) * 100.0
  stake_a = stake_total * implied_prob(decimal_a) / inv
  stake_b = stake_total * implied_prob(decimal_b) / inv
  payout = stake_a * decimal_a  # equals stake_b * decimal_b
  return {
    "profit_pct": profit_pct,
    "stake_a": round(stake_a, 2),
    "stake_b": round(stake_b, 2),
    "payout": round(payout, 2),
    "profit_usd": round(payout - stake_total, 2),
    "inv_prob": inv,
  }


def three_way_arb(
  decimals: list[float],
  stake_total: float = 100.0,
) -> dict | None:
  if len(decimals) < 3 or any(d <= 1.0 for d in decimals):
    return None
  inv = sum(implied_prob(d) for d in decimals)
  if inv >= 0.999:
    return None
  profit_pct = (1.0 - inv) * 100.0
  stakes = [round(stake_total * implied_prob(d) / inv, 2) for d in decimals]
  payout = stakes[0] * decimals[0]
  return {
    "profit_pct": profit_pct,
    "stakes": stakes,
    "payout": round(payout, 2),
    "profit_usd": round(payout - stake_total, 2),
    "inv_prob": inv,
  }

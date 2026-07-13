from __future__ import annotations

# Canonical regions we allow to pair within (never across).
REGION_ALIASES = {
  "us": "us",
  "us2": "us",
  "usa": "us",
  "uk": "uk",
  "gb": "uk",
  "eu": "eu",
  "europe": "eu",
  "au": "au",
  "australia": "au",
}

# Fallback when a source does not tag region (Action Network / ESPN ≈ US).
BOOK_REGION_HINTS: dict[str, str] = {
  "draftkings": "us",
  "fanduel": "us",
  "betmgm": "us",
  "caesars": "us",
  "betrivers": "us",
  "fanatics": "us",
  "espn bet": "us",
  "espnbet": "us",
  "pointsbet": "us",
  "bovada": "us",
  "betonline.ag": "us",
  "mybookie.ag": "us",
  "lowvig.ag": "us",
  "betus": "us",
  "superbook": "us",
  "william hill": "uk",
  "paddypower": "uk",
  "sky bet": "uk",
  "skybet": "uk",
  "betfair": "uk",
  "unibet": "eu",
  "betsson": "eu",
  "1xbet": "eu",
  "pinnacle": "eu",
  "marathon": "eu",
  "marathon bet": "eu",
  "bet365": "uk",
  "bwin": "eu",
  "coolbet": "eu",
  "nordicbet": "eu",
  "tipico": "eu",
  "ladbrokes": "uk",
  "coral": "uk",
  "neds": "au",
  "sportsbet": "au",
  "tab": "au",
  "pointsbet au": "au",
}


def normalize_region(raw: str | None) -> str:
  if not raw:
    return "us"
  key = str(raw).strip().lower()
  return REGION_ALIASES.get(key, key)


def infer_book_region(book: str, default: str = "us") -> str:
  name = (book or "").strip().lower()
  if not name:
    return default
  if name in BOOK_REGION_HINTS:
    return BOOK_REGION_HINTS[name]
  for needle, region in BOOK_REGION_HINTS.items():
    if needle in name:
      return region
  return default

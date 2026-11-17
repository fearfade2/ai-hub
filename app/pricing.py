"""Token pricing per model, loaded from data/pricing.json; fnmatch patterns."""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path


class Pricing:
    """USD per 1M prompt/completion tokens. Unknown models fall back to defaults."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.rules: dict[str, dict[str, float]] = {}
        self.default_in = 3.0
        self.default_out = 9.0
        self.load()

    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if "default" in data and isinstance(data["default"], dict):
            self.default_in = float(data["default"].get("in", self.default_in))
            self.default_out = float(data["default"].get("out", self.default_out))
        for pattern, rates in data.items():
            if pattern == "default" or not isinstance(rates, dict):
                continue
            self.rules[pattern] = {
                "in": max(0.0, float(rates.get("in", self.default_in))),
                "out": max(0.0, float(rates.get("out", self.default_out))),
            }

    def rates(self, model: str) -> tuple[float, float]:
        for pattern, rates in self.rules.items():
            if fnmatch.fnmatch(model, pattern):
                return rates["in"], rates["out"]
        return self.default_in, self.default_out

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        rate_in, rate_out = self.rates(model)
        return (prompt_tokens / 1_000_000 * rate_in
                + completion_tokens / 1_000_000 * rate_out)


def tier_for(model: str, rules: dict[str, str]) -> str:
    """Map a model to a tier name using config rules; returns 'base' by default."""
    for pattern, tier in sorted(rules.items()):
        if fnmatch.fnmatch(model, pattern):
            return tier
    return "base"

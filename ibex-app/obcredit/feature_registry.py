"""Feature registry: declare each feature once, with the metadata a bank needs.

A FeatureSpec records:
  name           -- canonical feature name used everywhere downstream
  func           -- the pure function (FeatureContext -> float|None) = f()
  family         -- grouping for selection / monotonic constraints
  monotonic      -- +1 risk-increasing, -1 protective, 0 unconstrained
                    (feeds XGBoost monotone_constraints later)
  kaggle_columns -- the ORIGINAL Home Credit column(s) this feature reconstructs
  parity         -- whether the parity test asserts Kaggle==TrueLayer for it
  description    -- plain-English definition for documentation/audit

The decorator @feature(...) registers the function so the pipeline can discover
all features automatically. Adding a feature = writing one decorated function.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    func: Callable
    family: str
    monotonic: int
    kaggle_columns: tuple
    parity: bool
    description: str


class FeatureRegistry:
    def __init__(self) -> None:
        self._specs: Dict[str, FeatureSpec] = {}

    def register(self, spec: FeatureSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate feature: {spec.name}")
        self._specs[spec.name] = spec

    def all(self) -> List[FeatureSpec]:
        return list(self._specs.values())

    def names(self) -> List[str]:
        return list(self._specs.keys())

    def parity_names(self) -> List[str]:
        return [s.name for s in self._specs.values() if s.parity]

    def monotone_map(self) -> Dict[str, int]:
        return {s.name: s.monotonic for s in self._specs.values()}


REGISTRY = FeatureRegistry()


def feature(name: str, family: str, monotonic: int, kaggle_columns,
            parity: bool = True, description: str = ""):
    """Decorator that registers a feature function under REGISTRY."""
    def wrap(fn: Callable) -> Callable:
        REGISTRY.register(FeatureSpec(
            name=name, func=fn, family=family, monotonic=monotonic,
            kaggle_columns=tuple(kaggle_columns), parity=parity,
            description=description or (fn.__doc__ or "").strip(),
        ))
        return fn
    return wrap

"""obcredit: source-agnostic credit-risk feature construction.

The whole design rests on ONE idea: convert every raw source (Kaggle competition
tables, TrueLayer open-banking JSON) into the SAME canonical objects, then run a
SINGLE feature function library f() on those objects. Identical f() on identical
canonical data => identical features => the train/inference consistency a bank
needs to defend the model.
"""
from .config import DEFAULT, EngineConfig

__all__ = ["DEFAULT", "EngineConfig"]
__version__ = "1.0.0"

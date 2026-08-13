"""Adapter contract.

An adapter's ONLY job is to map a raw source into a list of CanonicalApplicant.
It must perform NO feature maths -- all maths lives in the shared engine and
feature library. This separation is what guarantees Kaggle and TrueLayer produce
features the same way.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List

from ..canonical import CanonicalApplicant


class SourceAdapter(ABC):
    @abstractmethod
    def to_canonical(self) -> List[CanonicalApplicant]:
        """Return one CanonicalApplicant per case_id / connected user."""
        raise NotImplementedError

"""Execution backend implementations for model evaluation."""

from auto_test.evaluation.backends.evalscope import EvalScopeBackend, EvalScopeRuntimeConfig
from auto_test.evaluation.backends.mock import DeterministicMockBackend

__all__ = ["DeterministicMockBackend", "EvalScopeBackend", "EvalScopeRuntimeConfig"]

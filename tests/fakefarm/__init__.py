"""The fake Agent Farm for the CLI tests. See the `farm` module for what it models and why."""

from .farm import (
    ADMIN,
    ALICE,
    BOB,
    EXPIRED,
    HOST,
    OTHER,
    OTHER_TENANT,
    READONLY,
    TENANT,
    ApiProblem,
    Call,
    FakeFarm,
    FakeFarmBug,
    Principal,
)

__all__ = [
    "ADMIN",
    "ALICE",
    "BOB",
    "EXPIRED",
    "HOST",
    "OTHER",
    "OTHER_TENANT",
    "READONLY",
    "TENANT",
    "ApiProblem",
    "Call",
    "FakeFarm",
    "FakeFarmBug",
    "Principal",
]

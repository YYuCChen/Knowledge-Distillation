from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class IdentityFailure(StrEnum):
    INPUT_UNSUPPORTED = "input_unsupported"
    IDENTITY_UNCONFIRMED = "identity_unconfirmed"
    LOGIN_REQUIRED = "login_required"
    UPSTREAM_FAILURE = "upstream_failure"


@dataclass(frozen=True)
class ConfirmedMaterialIdentity:
    platform: str
    platform_item_id: str
    original_url: str
    canonical_url: str
    identity_confirmed: bool = True


@dataclass(frozen=True)
class IdentityResolution:
    identity: ConfirmedMaterialIdentity | None = None
    failure: IdentityFailure | None = None

    def __post_init__(self) -> None:
        if (self.identity is None) == (self.failure is None):
            raise ValueError("Identity resolution must contain one result")

    @classmethod
    def confirmed(cls, identity: ConfirmedMaterialIdentity) -> IdentityResolution:
        return cls(identity=identity)

    @classmethod
    def failed(cls, failure: IdentityFailure) -> IdentityResolution:
        return cls(failure=failure)


class MaterialIdentityResolver(Protocol):
    def identify(self, original_url: str, target_url: str) -> IdentityResolution: ...

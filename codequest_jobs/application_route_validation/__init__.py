"""Opt-in prototype: application-route validation (separate from production lifecycle writes).

Not wired into run_jobready_daily. Use application_route_validation.run_fixture_benchmark for experiments.
"""

from application_route_validation.contracts import (
    APPLICATION_ROUTE_VALIDATOR_VERSION,
    ApplicationRouteValidationResult,
    JobValidationTarget,
    RouteDimension,
)
from application_route_validation.engine import validate_application_route

__all__ = [
    "APPLICATION_ROUTE_VALIDATOR_VERSION",
    "ApplicationRouteValidationResult",
    "JobValidationTarget",
    "RouteDimension",
    "validate_application_route",
]

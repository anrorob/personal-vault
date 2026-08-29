"""Compatibility aliases for the first managed-publication consumer: Theatre."""
from app.arrival_managed_publisher import *  # noqa: F403

# Kept temporarily for the existing Theatre reissue endpoint and callers.
ArrivalTheatrePromotionRequest = ArrivalManagedPublicationRequest  # noqa: F405

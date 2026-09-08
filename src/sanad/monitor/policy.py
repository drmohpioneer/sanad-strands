"""Released operational defaults; not clinical thresholds."""

from datetime import timedelta
from types import MappingProxyType

OWNER_REVIEW_PENDING = True
slot_hours = MappingProxyType({1: (8,), 2: (8, 20), 3: (8, 14, 20), 4: (8, 12, 16, 20)})
slot_tolerance = timedelta(hours=3)
required_coverage = "every_slot"
max_days = 30
max_times_per_day = 4
trend_minimum = 3

"""Released operational defaults; not clinical thresholds."""

from types import MappingProxyType

OWNER_REVIEW_PENDING = True
slot_hours = MappingProxyType({1: (8,), 2: (10, 22), 3: (8, 14, 20), 4: (8, 12, 16, 20)})
required_coverage = "every_slot"
max_days = 30
max_times_per_day = 4
trend_minimum = 3

select *
from (
    values
        ('c1', 'ada@example.com', 'FR', timestamp '2026-01-05 09:00:00'),
        ('c2', 'grace@example.com', 'US', timestamp '2026-02-11 14:30:00')
) as t(customer_id, email, country_code, created_at)

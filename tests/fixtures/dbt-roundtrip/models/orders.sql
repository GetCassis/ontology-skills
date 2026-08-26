select
    o.order_id,
    o.customer_id,
    o.status,
    o.total_amount,
    o.placed_at
from (
    values
        ('o1', 'c1', 'completed', 120.50, timestamp '2026-03-01 10:00:00'),
        ('o2', 'c2', 'cancelled', 80.00, timestamp '2026-03-02 11:00:00')
) as o(order_id, customer_id, status, total_amount, placed_at)
where o.customer_id in (select customer_id from {{ ref('customers') }})

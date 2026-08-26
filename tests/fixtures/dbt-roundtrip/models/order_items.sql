select
    i.order_item_id,
    i.order_id,
    i.quantity,
    i.unit_price
from (
    values
        ('i1', 'o1', 2, 60.25),
        ('i2', 'o2', 1, 80.00)
) as i(order_item_id, order_id, quantity, unit_price)
where i.order_id in (select order_id from {{ ref('orders') }})

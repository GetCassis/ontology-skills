select * from (values
    ('i1', 'o1', 'p1', 1, 50.50),
    ('i2', 'o1', 'p2', 1, 70.00),
    ('i3', 'o2', 'p1', 1, 80.00)
) as i(order_item_id, order_id, product_id, quantity, unit_price)

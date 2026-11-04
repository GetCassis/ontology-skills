select *
from (
    values
        ('p1', 'Espresso cups', 'kitchen'),
        ('p2', 'Pour-over kettle', 'kitchen')
) as p(product_id, product_name, category)

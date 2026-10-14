# What the agent learned

Written by the agent after each run (routes it used, discovery notes).
Edit or delete freely; wrong lines are worse than none.

## parcel_value:portal
- Route for typing/pasting checks on the Parcel Value field: open /shipping-only/create (same field, no existing order needed) (2026-10-14, seeded)
- Route to edit an existing order's Parcel Value: open an order from the orders list -> click 'Edit Order Details' (2026-10-14, TC1 reached)

## crm_settings_shipment_protection:crm
- Route: open Admin (Administration index) -> click the 'Shipment Protection' link there. Never type its module=Administration URL directly; the save then fails with an XSRF error (2026-10-14, seeded)

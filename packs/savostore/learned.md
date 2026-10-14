# What the agent learned

Written by the agent after each run (routes it used, discovery notes).
Edit or delete freely; wrong lines are worse than none.

## parcel_value:portal
- Behaviour: Buy & Ship orders have no protection step. (2026-10-14, facts)
- Behaviour: Parcel Value drops decimals (200.50 -> 200, paste 25.50 -> 25), 1,000 -> 1000; 0 shows 'Enter a value greater than zero'; '-' shows the 'Whole dollars only' hint. (2026-10-14, facts)
- Route for typing/pasting checks on the Parcel Value field: open /shipping-only/create (same field, no existing order needed) (2026-10-14, seeded)
- Route to edit an existing order's Parcel Value: open an order from the orders list -> click 'Edit Order Details' (2026-10-14, TC1 reached)

## crm_settings_shipment_protection:crm
- Behaviour: A client exemption always overrides the threshold; declining protection means no charge anywhere. (2026-10-14, facts)
- Behaviour: Rate accepts decimals (2.5); minimum accepts 0. CRM protection cost shows whole dollars (4.02 as 4.00) by design. (2026-10-14, facts)
- Behaviour: Threshold Save with 12.5, 200.5, 0, -5, abc or blank shows no message and the form keeps the typed text; after a reload the old value is back (silent reject). Whole numbers like 200 save. Verify by reloading. (2026-10-14, facts)
- Route: open Admin (Administration index) -> click the 'Shipment Protection' link there. Never type its module=Administration URL directly; the save then fails with an XSRF error (2026-10-14, seeded)

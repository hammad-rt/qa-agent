# What the agent learned

Written by the agent after each run (routes it used, discovery notes).
Edit or delete freely; wrong lines are worse than none.

## parcel_value:portal
- Behaviour: Portal: Validation for zero/empty on blur: Validation messages 'Enter a value greater than zero' (for 0) and 'Parcel value is required' (for empty) were verified. (2026-10-15, TC4)
- Route: click 'radio "No"' -> click 'button "Create New Order"' -> click 'heading "Tracking Number 1"' -> click 'textbox "Enter total parcel value in USD"' -> click 'button "Continue"' -> click 'button "Create New Order"' -> click 'textbox "Enter total parcel value in USD"' -> click 'button "Continue"' (2026-10-15, TC4 reached)
- Route: click 'Continue button' -> click 'Air Shipping button' -> click 'Continue button' -> click 'No radio button' -> click 'Create New Order button' -> click 'heading \"Tracking Number 1\"' -> click 'textbox \"Enter total parcel value in USD\"' -> click 'Continue button' (2026-10-15, TC4 reached)
- Behaviour: Portal: Block signs, letters, and leading zeros: Typing '-5' and 'e5' both failed to enter the field and triggered the hint 'Whole dollars only, no signs or letters'; typing '007' resulted in the value '7'. (2026-10-15, TC3)
- Behaviour: Portal: Paste normalizer for decimals and separators: Pasting '25.50' resulted in '25' and pasting '1,000.50' resulted in '1000'. (2026-10-15, TC2)
- Behaviour: Portal: Block decimal and comma typing: When typing '12', then '.', then '34', the resulting value is '1234'. Similarly, typing '12', then ',', then '34' results in '1234'. (2026-10-15, TC1)
- Behaviour: Portal: Paste decimals and separators: Pasting '25.50' resulted in '25' and pasting '1,000.50' resulted in '1000'. (2026-10-14, TC2)
- Behaviour: Portal: Paste decimals and separators: The agent used `browser_type` instead of pasting, and the log fails to verify the value after the first input step (seq 24) before overwriting it with the second input (seq 26). (2026-10-14, TC2)
- Route: click 'button "U.S Dollars"' -> click 'button "Continue"' -> click 'button "California, USA"' -> click 'button "Continue"' -> click 'button "Air Shipping"' -> click 'button "Continue"' -> click 'radio "No"' -> click 'button "Create New Order"' (2026-10-14, TC2 reached)
- Route: click 'Continue button' -> click 'California, USA button' -> click 'Continue button' -> click 'Air Shipping button' -> click 'Continue button' -> click 'No radio button' -> click 'Create New Order button' -> click 'Continue button' (2026-10-14, TC4 reached)
- Route: click 'U.S Dollars button' -> click 'Continue button' -> click 'California, USA button' -> click 'Continue button' -> click 'Air Shipping button' -> click 'Continue button' -> click 'No radio button' -> click 'Create New Order button' (2026-10-14, TC1 reached)
- Behaviour: Buy & Ship orders have no protection step. (2026-10-14, facts)
- Behaviour: Parcel Value drops decimals (200.50 -> 200, paste 25.50 -> 25), 1,000 -> 1000; 0 shows 'Enter a value greater than zero'; '-' shows the 'Whole dollars only' hint. (2026-10-14, facts)
- Route for typing/pasting checks on the Parcel Value field: open /shipping-only/create (same field, no existing order needed) (2026-10-14, seeded)
- Route to edit an existing order's Parcel Value: open an order from the orders list -> click 'Edit Order Details' (2026-10-14, TC1 reached)

## crm_settings_shipment_protection:crm
- Behaviour: To check a threshold Save: leave the page, reopen Shipment Protection from the Administration index link, and read the threshold field before typing anything (it should still show the old value, e.g. 250). Reading it once is enough; then mark the step and move to the next value. (2026-10-14, facts)
- Behaviour: A client exemption always overrides the threshold; declining protection means no charge anywhere. (2026-10-14, facts)
- Behaviour: Rate accepts decimals (2.5); minimum accepts 0. CRM protection cost shows whole dollars (4.02 as 4.00) by design. (2026-10-14, facts)
- Behaviour: Threshold Save with 12.5, 200.5, 0, -5, abc or blank shows no message and the form keeps the typed text; after a reload the old value is back (silent reject). Whole numbers like 200 save. Verify by reloading. (2026-10-14, facts)
- Route: open Admin (Administration index) -> click the 'Shipment Protection' link there. Never type its module=Administration URL directly; the save then fails with an XSRF error (2026-10-14, seeded)

## shipping_only:portal
- Route: open / -> click 'Shipping Only Order button' -> click 'Shipping Only Order <a record> link' -> click 'Edit Order Details button' (2026-10-14, TC2 reached)
- Route: click 'Shipping Only Order button' -> click 'Shipping Only Order <a record> link' -> click 'Edit Order Details button' (2026-10-14, TC1 reached)

## crm_threshold:crm
- Behaviour: CRM: Threshold valid whole number: The value '200' was entered and verified as saved in the CRM threshold setting. (2026-10-14, TC6)

## admin_settings:crm
- Behaviour: CRM: Shipment Protection threshold validation: The system rejected decimal (12.5), zero (0), and negative (-5) values, displaying the error message 'Please enter a whole number (no decimals)' in each case. (2026-10-15, TC5)

## general:portal
- Savo Express /savo-express, cart /savo-express/cart.
- Buy & Ship list /buy-and-ship; open with /buy-and-ship?editedOrder=<B-number>; Express orders are BX numbers in this list.
- Shipping Only list /shipping-only; open an order with /shipping-only?editedOrder=<A-number>; pay at /shipping-only/invoice/<record id>.
- Savo Express: the order only exists after payment; other order types show an order ID before paying.
- Add to cart opens an 'Added to cart' prompt: Checkout or Continue shopping.
- Shipping Only has its own menu, separate from Buy & Ship; Stripe tests pay an existing unpaid Shipping Only order (4242 card), never a new order.

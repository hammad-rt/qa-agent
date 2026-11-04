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

## crm:crm
- Behaviour: Quick Repair: The agent logged in as admin, navigated to the Repair menu, and triggered 'Quick Repair and Rebuild'. The resulting page text confirms the process was executed, showing multiple 'Clearing...done' messages and 'Rebuilding...' logs. (2026-11-04, 184815.TC1)
- Route: open / (2026-11-04, 184815.TC1 reached)
- Behaviour: Line item save still works: The parcel status was changed to 'Not Yet Received', saved, and verified to persist after reload. The value was subsequently restored to 'Received'. (2026-10-28, TC4)
- Behaviour: Declined / no protection stays zero: The shipment protection was set to 'No'. After changing the parcel value to 5000.00, the protection cost fields remained at 0.00. (2026-10-28, TC3)
- Behaviour: Parcel drives value and cost: The agent updated the parcel value from 4000 to 5000; the shipment value and protection cost (80 to 100) updated live and persisted after saving and reloading the page. (2026-10-28, TC2)
- Behaviour: Shipment Value read-only: Both the 'Shipment Value' and 'Shipment Value(USD)' fields were confirmed as read-only. The agent attempted to type '5000.00' into both fields, and both attempts failed with a TimeoutError because the locators resolved to inputs with the 'readonly' attribute (e.g., <input r (2026-10-28, TC1)
- Route: click 'Orders' -> click 'Shipping Only Orders' -> click ' for order <a record>' (2026-10-28, TC1 reached)
- Behaviour: Line item save still works: The agent changed the parcel status of order <a record> to 'Received', saved the order, and verified the change persisted by attempting to set it back to 'Not Yet Received' in the subsequent step, confirming the save was successful. (2026-10-28, TC4)
- Behaviour: Declined / no protection stays zero: The agent updated the Parcel Value in USD to 3,000.00 and verified that the protection cost remained 0.00, as the Shipment Protection was set to 'No'. (2026-10-28, TC3)
- Behaviour: Parcel drives value and cost: The Shipment Value was increased from 3000.0 to 4000.0, which caused the protection cost to recalculate from 60.0 to 80.0. These values persisted after saving and reloading the page. (2026-10-28, TC2)
- Route: click 'Orders' -> click 'Shipping Only Orders' -> click '' -> click 'Yes' (2026-10-28, TC1 reached)
- Behaviour: Line item save still works: The agent changed the parcel status of order <a record> to 'Received', saved it, and verified it persisted upon reload. (2026-10-26, TC4)
- Behaviour: Declined / no protection stays zero: The agent changed the parcel value to 3000.00 and verified that the protection cost remained 0.00 (as the order had protection set to 'No'). (2026-10-26, TC3)
- Behaviour: Parcel drives value and cost: Initial values (Parcel Sum 1001.0, Protection Cost 20.02) were updated to (Parcel Sum 2000.0, Protection Cost 40.02). After save and reload, the final values persisted as Parcel Sum 2000.0 and Protection Cost 40.0 (rounded as per project facts). (2026-10-26, TC2)
- Behaviour: Shipment Value and Shipment Value USD are read-only after Quick Repair and Rebuild plus a hard reload; before the repair they looked editable from stale cache (2026-10-26, #183498 TC1).
- Route: click 'Orders' -> click 'Shipping Only Orders' -> click 'EditView for <a record>' -> click 'Edit link for order <a record>' -> click 'Shipment Value field' (2026-10-26, TC1 reached)

## admin:crm
- Behaviour: Run Quick Repair and Rebuild on the target environment: Quick Repair and Rebuild was executed successfully; database tables were synced with vardefs without errors. (2026-10-28, TC0)

## portal:portal
- Behaviour: Desktop field order: The field order matches the expectation: Recipient's Name -> Phone Number | Recipient's email -> Warning paragraph -> Street | Nearest Landmark -> City | County/State -> Postal Code | Country. (2026-11-04, 184815.TC2)
- Route: open / (2026-11-04, 184815.TC2 reached)

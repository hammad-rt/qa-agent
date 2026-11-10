# Savostore project pack: facts

Rewritten 2026-10-06 after the original file was lost. Sources: QA runs on #180966 (25 Sep), #165177 and #165348 (28-30 Sep), the user's confirmations, and the 28 Sep report email.

## Tracker
- Redmine: https://redmine.rolustech.com, project 1567.
- Test cases live in a child task named "Test Cases - ...".
- Staging addresses are on the wiki page "Staging_Cardentials". The wiki has no production app logins. The wiki holds plaintext secrets; never copy them into reports or memory.

## Environments
| | Staging | Production | Local (laptop) |
|---|---|---|---|
| Customer portal | https://staging5.rolustech.com:4225/ | https://app.savostore.com/ | http://localhost:3000/ |
| CRM (SuiteCRM) | https://staging5.rolustech.com:4220/ | https://savo.store/ | http://localhost/savostorea/ |

## Accounts (passwords never stored here)
- All Redmine posts (notes, time entries, bugs) go through Hammad Iqbal's account, never Sidney's.
- Staging CRM: `maria.tariq`. Was not an admin on 25 Sep, became admin by 28 Sep. Check the role each run.
- Staging portal: `sidney@savostore.com`. Check the account currency (USD or KES) each run.
- Production: no accounts known yet. Ask.

## Payments
- Stripe test card on staging and local: 4242 4242 4242 4242, expiry 12/34, CVC 123.
- Buy & Ship (B&S) orders on staging offer only M-Pesa, so card payment is blocked for them.
- Classic Buy & Ship is paid by bank transfer. Stripe (card) tests for non-Express orders use Shipping Only orders instead: pay an EXISTING unpaid Shipping Only order with the 4242 card; don't create new orders for this (confirmed by Hammad 2026-10-15).
- M-Pesa: skip unless the user says otherwise. Never pay with M-Pesa in rewards cases (B18 is out of scope).

## Local environment (laptop, set up 2026-11-04)
- Local data is its own database: order numbers and record ids (A1148 etc.) are not the staging ones. Find or create orders each run.
- Stripe works locally only while the Stripe CLI listener runs (it forwards checkout.session.completed to the CRM's
  entry_point_stripe_webhook.php). Paying on Stripe returns to the portal's /payment/success page, but the order is
  only marked paid when that callback arrives. If the invoice stays unpaid after a 4242 payment, the case is BLOCKED
  ("Stripe callback did not arrive"), not FAIL.
- The local portal login has no reCAPTCHA widget (no site key set), so email/password login submits without one.

## Modules (portal)
- Shipping Only is its own module with its own menu in the portal, separate from Buy & Ship (confirmed 2026-10-15). In the CRM, Shipping Only orders are `AOS_Invoices`; Buy & Ship are `AOS_Quotes`.
- Savo Express: the order is created only after payment. Other order types (Shipping Only, Buy & Ship) still get an order ID up front.
- Adding to the cart opens an "Added to cart" prompt with Checkout and Continue shopping.
- Savo Express prices are shown as they are, with no "est." or "estimated" label (#182698 won't do). Don't flag a missing "estimated" label.
- Intercom on the portal stays as it is. Don't flag it or suggest changing it.

## Routes (staging, verified 2026-10-16; order numbers and record ids below are placeholders, look them up each run)
Portal base https://staging5.rolustech.com:4225
- Home `/home`. Account: `/rewards`, `/profile`, `/change-password`, `/warehouse-addresses`, `/delivery-addresses`.
- Shipping Only: list `/shipping-only`, create `/shipping-only/create`, open an order `/shipping-only?editedOrder=<A-number>`, invoice/payment `/shipping-only/invoice/<crm record id>`.
- Buy & Ship: list `/buy-and-ship`, purchase-quote form `/buy-and-ship/create`, open an order `/buy-and-ship?editedOrder=<B-number>`, edit `/buy-and-ship/edit/<record id>?orderNumber=<B-number>`, invoice/payment `/buy-and-ship/invoice/<record id>`.
- Savo Express: `/savo-express`, cart `/savo-express/cart`. Express orders appear in the Buy & Ship list as BX numbers.
- Orders have no detail page: `editedOrder` opens the order's card expanded in the list.
- "Delivery Information" in a ticket means the delivery step inside a Buy & Ship or Shipping Only order (pick Next-Day Door Delivery or Pickup there). Its phone field is the combined box labelled "Phone Number" with the +254 flag and a green NEW tag. `/delivery-addresses/create` (Account > Delivery Addresses > Add) is a different form whose field is just "Phone" with no NEW tag; never use it for Delivery Information cases (#184815 TC3/TC6 false fails, 2026-11-04).

CRM base https://staging5.rolustech.com:4220/index.php
- Shipping Only orders `module=AOS_Invoices`; Buy & Ship (incl. BX) `module=AOS_Quotes`; Payments `module=sav_payments`; Clients `module=Contacts`; Rewards Redeemed `module=sav_rewards_redeemed`; Loyalty Rewards `module=sav_rewards`.
- Record page `?module=<M>&action=DetailView&record=<id>`. The CRM record id is the same id the portal invoice/edit URLs use.
- Admin settings (open from Administration `action=index`; a direct URL gives an XSRF error): `saveRewardsPanel` (Savo Rewards), `saveShipmentProtection`, `saveExpressCategoryWeightDefaults`, `saveExpressPricingSettings`.

## Business rules
- Rewards earn rate: 5% of shipping after rewards. Earned when the order is fully paid and its status changes to "Order Delivered" in the CRM.
- Redemption cap: rewards cover at most 10% of the shipping price, not the order total.
- Adding a parcel to an already-paid order: the rewards limit is based only on the new unpaid shipping (confirmed intended by the user; for example a $400 parcel on a previously paid $1,000 order gives a $40 limit).
- A rewards balance of at least $100 or 50,000 KES is needed for the redemption cases.
- Mandatory Shipment Protection (#180966) applies above a threshold set in the CRM. Production threshold: $150 (as of 30 Sep). Test at the threshold and just above it (150 and 151).

- Test data placeholders in cases (ORDER-MULTI, ORDER-EARN, ORDER-KES, ORDER-BAS, CUSTOMER, Wallet) are never real order numbers. They are resolved for THIS run from the pack placeholders table (find first, else create) and stored only in run state (`order_multi` / `order_earn` / … plus aliases like `test_order` / `test_order_record`). Never search the page for the literal token. On the portal open a shipping-only invoice at `<portal URL>/shipping-only/invoice/<record id>`; there is no `/payment/<number>` page (it is a 404). Buy & Ship: `order_bas` / `test_quote`.
- The wallet invariant (W = L - U, W >= 0, U <= L) is computed by code and given to the judge; a negative wallet with U > L is a FAIL even when W = L - U holds.

## Savo Rewards: CRM facts (verified 2026-10-12)
- Earn/redeem config: CRM `index.php?module=Administration&action=saveRewardsPanel`; fields "Rewards Earned (Percentage)" (normal 5) and "Rewards Redeemed (Percentage)" (normal 10). Changing them is a settings change: needs approval, record the original, restore after.
- Shipping-only orders are `AOS_Invoices` records; Buy & Ship orders are `AOS_Quotes`.
- Admin redemption: order DetailView > "Rewards Redeemed" subpanel > Create (name "Redeemed rewards by admin against <order>"; Redeemed Rewards editable, Applicable Rewards read-only).
- Client / CRM contact for a run: always the contact that matches the portal login/email picked for THIS run
  (look up by that email, or by the orders that login owns). Never name a fixed CRM contact here for any env —
  staging included. Contact fields: Total Rewards, Current Rewards, Rewards Used.
- Contact search ladder (SuiteCRM Clients/Contacts): basic name search is name-based only — for email use the
  list filter or Advanced Search, then global search. Confirm the record's email field matches the portal email
  before accepting the id. Open later steps by that CRM record id (run state), never by typing a person's name
  from facts. After two or three failed routes, ask which CRM contact is this portal email — do not finish stuck
  first. The resolved id stays in run state only (never learned.md).
- Earn happens only when the status CHANGES to Order Delivered; re-saving an order already at Order Delivered does
  nothing. To re-trigger, move it to another status, save, then set Order Delivered again. Shipping Only overwrites
  its single earn row on re-trigger; Buy & Ship earns once and never updates that row (code, 2026-11-04).
- Earned = earn % x (shipping - rewards redeemed on that order). The order's own earn % field overrides the Admin
  setting. Earning also needs Savo Rewards enabled on the CRM contact (`sav_rewards_enabled`); check it in E0.
- Earning needs the order FULLY paid: a linked payment and a balance of 0 or less. A Paid payment that leaves a
  balance earns nothing, and an unpaid order earns nothing. Any case that earns rewards must (1) pay the whole
  balance (4242 card on staging/local), (2) confirm the payment is recorded and the balance is 0 (portal and CRM)
  and only then (3) set Order Delivered. If the payment did not go through, do not move
  the status: the case is BLOCKED ("payment not recorded"), not FAIL. A wallet that stays empty after Order Delivered
  on an unpaid order is expected, not a bug.
- Redeem cap = 10% of unpaid shipping; 0 once paid >= shipping.
- Buy & Ship rewards box shows only at Awaiting Payment / Shipping Fee Invoiced; USD shows only Pay to Bank Account. Shipping ~$10/cu ft, $35 minimum.
- Order fit check before any rewards case: invoice exists, balance > 0, unpaid shipping > 0, client matches. A paid order (e.g. A1148) is unusable; prepare a fresh shipping-only order with an invoice and unpaid shipping (needs approval).
- Timing: apply/remove takes 3-5s; the rewards box shows a false "no rewards"/"up to $15.00" for 1-2s on load; CRM subpanels update ~8s late. Wait and recheck before any verdict.
- Stripe path: Pay By Card > /shipping-only/stripe/<id>?appliedRewards=N > second "Pay by Card" > Stripe; choose USD; test card from the Payments section.

## E0 and E1 (earn) — how to run them
- Soft dependency (agent code, 2026-11-05): if E0 fails only as a known open bug still present (e.g. bug 4 wallet
  drift), later cases including E1 still run. E1 is blocked only when E0 failed in a way that breaks E1's own
  preconditions (login, wrong contact, or no readable W0).
- Known bug 4 is pre-existing wallet drift: Total (L) and Used (U) disagree with Current (W), including U > L.
  Portal Current matching CRM Current (W0) can still be true. That match meets E0's baseline expected; the drift
  itself is PARTIAL / still-present bug 4, not a reason to skip earn cases.
- E1 must check its own preconditions before the earn steps (do not assume E0 left them true):
  1. Savo Rewards enabled on the CRM contact (`sav_rewards_enabled`).
  2. A Shipping Only order (`AOS_Invoices`) that is fully paid (linked payment, balance <= 0). Prefer Shipping Only
     over Buy & Ship: SO can re-earn on a later Order Delivered; B&S earns once and never again.
  3. The order is not already at Order Delivered (or move it away, save, then set Order Delivered again).
  If any of these is missing, finish with notes starting `BLOCKED:` and the missing precondition — do not call it FAIL.
- Assert the wallet delta, not a guessed absolute: read L0, W0, U0 on the contact before the status change; after
  Order Delivered, expect L1 = L0 + E and W1 = W0 + E where E = round_down_to_whole(earn% × (shipping − rewards
  redeemed on that order)). Earn % is the order's own field if set, else Admin Savo Rewards (normally 5). Portal
  Current must match CRM W1.
- CRM contact for E0/E1 (every env): the contact for the portal login/email chosen at run start. Look it up by
  that email or by the orders that login owns — never by a fixed name from KB (any environment).
- Payments for earn: Stripe test card 4242 only. Never M-Pesa.
- Admin Savo Rewards earn %: change only with approval; record the original; restore and re-read after the case.
- Admin pages (Repair, Savo Rewards): open from Administration index. A typed module URL gives an XSRF error.
- After a deploy on staging: Admin → Repair → Quick Repair and Rebuild, clear browser cache, hard-reload each
  EditView/DetailView before the first earn or rewards case. Stale JS has hidden new code (#184813).

## Savo Rewards: expected behaviour (#165177, #165348, #183737; from the rewards cases file, 2026-11-04)
Notation: E% earn %, R% redeem cap %, FX USD to KES rate, W / L / U = the contact's Current / Total (lifetime) / Used
rewards, cap(s) = R% x unpaid shipping s.
- Applicable (portal "You can use up to" and CRM Applicable) = min(W in the order's currency, cap(unpaid shipping) -
  rewards already redeemed on that tranche). Portal "up to" and CRM Applicable must be equal.
- Rewards count as Used only after the Stripe payment succeeds. Before that a portal apply is a pending "against"
  row with status Applied. Opening Stripe must not commit it; abandoning checkout leaves W and U unchanged, with no
  Used row and no portal "Rewards Used" entry. A successful payment commits exactly once (W - x, U + x, one Used row);
  reloads afterwards must not deduct again.
- The portal session: a hard reload or navigating away (Orders list, /rewards) clears pending rewards and deletes the
  CRM Applied row. Pressing browser Back from Stripe (soft back) keeps the pending amount, and replacing it is allowed
  up to the full cap. Apply then X, repeated, must leave at most one Applied row (no stacking). After X, "up to"
  returns to the cap and the "You have" line shows W, not W minus the removed amount.
- Admin redemptions (CRM order > Rewards Redeemed > Create, name "Redeemed rewards by <admin> against <order>") are
  "by" rows: on save the row is Used, W drops and U rises at once, the order's Savo Rewards shows the negative amount
  on the portal too, and paying later must not deduct again. A portal apply must not remove an admin "by" row.
- In that Create form, Total Shipping Cost is read-only and equals the order's shipping, not the Grand Total.
- Over-limit messages: portal "Rewards amount cannot exceed <cap>"; over the wallet on KES "cannot exceed KES <FX x W>";
  CRM admin "Redeemed rewards cannot exceed ...". A rejected amount creates no pending row and leaves "up to" as it was.
- An invalid discount code shows an error and does not change "up to".
- After a paid order gets a new package: balance = the new shipping only, "up to" = cap(new shipping) (not
  cap(total) - redeemed), and no customer credit.
- KES: the wallet is held in USD and shown as FX x W on KES orders, on both the portal and the CRM. The cap uses
  shipping only: non-shipping charges (shipment protection, next-day delivery, misc) are never included.
- Whole units in every currency: decimals are rejected (`rewards_not_whole_number`) and "up to" rounds down
  (35.19 shows as 35). Stripe charges are whole units too.
- Buy & Ship and Shipping Only must handle rewards identically; any difference is a bug. On Buy & Ship the rewards box
  shows only at Awaiting Payment, Shipping Fee Invoiced or Order Arrived, with a payable above 0. Card payment
  should be offered (never use M-Pesa).
- The portal rewards session lasts 30 minutes: don't judge pending-rewards behaviour on a page left idle longer.
- At balance 0, Pay By Card says "Order is already fully paid."; with under 1 unit left it shows the "below the minimum
  amount" message. Both are expected, not bugs.
- The Stripe webhook sets the payment to Paid first, then marks the rewards Used. Payment Paid but rewards still
  Applied = FAIL; payment never Paid = BLOCKED (payment not recorded).
- A refund reverts earned and redeemed rewards (hook exists in code; amounts not yet verified, so check, don't assume).
- Switching an order's currency must not change the wallet's Total, Current or Used.
- E0 must confirm the CRM contact whose wallet is read is the one for this run's portal login/email (match by
  that email or by the orders it owns — never by a hard-coded name).
- Invariants, always: W = L - U (gap 0), portal wallet = CRM Current, no order balance below 0, paid + rewards = total,
  and no portal "against" Applied rows left on test orders at the end.
- Build check: cases tied to the earlier report bugs (B1, B3, B6, B7, B8, B13, B15, B16, B19, A8, A14, C7) are expected
  to fail on builds without the #183737 rework. A fail there means the rework is missing or broken; say which.
- Order prerequisites: redemption cases need W >= cap of the order's shipping; if the wallet is short, run the earn
  case first (pay with 4242, confirm Paid, then Order Delivered). The extra-package cases need the order fully paid first.
- Not scriptable from the UI: an orphan Applied row (B17) and a Stripe webhook retry (A13). Report them as not verified.
- Restoring data after a mutating case: delete the Rewards Redeemed rows the run created, restore the contact's W, L
  and U to the baseline snapshot, restore or void the test orders, and take a screenshot. Editing wallet fields or
  running SQL needs the data owner's approval.

## CRM login (SuiteCRM)
- Login fields are `user_name` and `username_password`; the button is `#bigbutton` (Enter also submits).
- Data setup on staging needs the Administrator account (`admin`); `maria.tariq` is not an admin.
- If login_form fails, do not retry by clicking Log In by hand; finish with notes "BLOCKED: CRM login failed".

## Verified flows (from manual retest, 2026-10-12; follow these, check each step against the page)
**Open a CRM order directly:** browser_navigate to `<crm URL>/index.php?module=AOS_Invoices&action=DetailView&record=<record id>` (same id as the portal invoice, read in this run). Don't click the order-number link in lists; it often doesn't navigate.

**Paying a shipping-only invoice (portal, default when the case says pay on the portal):** use the **Pay By Card** button on the invoice page (opens `/shipping-only/stripe/<id>?appliedRewards=N`, then a second "Pay by Card" goes to Stripe; recipe R4). Do not click "Make a Payment" on these invoices; it does nothing useful.

**Record a payment in CRM (CRM alternative when portal card payment cannot be made):** on the order's DetailView open the **Payments** subpanel → **Create** → add a Paid payment for the full balance → confirm the subpanel shows the linked Paid payment and balance 0 → then move status on to completion (e.g. Order Delivered). Prefer this over looping on status saves. Note in the report that payment was made in CRM. Portal 4242 card payment stays the default when the case says pay on the portal.

**R1 Prepare a shipping-only order:** Customer creates the order on the portal; then in CRM open that AOS_Invoices record -> open its parcel, set Received, Save -> move status one step at a time, saving each: Shipped to Warehouse, On Warehouse floor, Ready to Ship, Shipped, Invoiced & Shipped (invoice the shipping in CRM). Portal: open `/shipping-only/invoice/<CRM record id>` directly (read the id from the CRM URL `record=` in the same run; never reuse an old id; "View Invoice" is not needed), wait ~4 s, check the balance, the Pay By Card button and rewards "up to" (= 10% of unpaid shipping).
**R2 Buy & Ship:** AOS_Quotes record; the rewards box shows only at Awaiting Payment or Shipping Fee Invoiced. In USD the only option is Pay to Bank Account; there is no currency switch, so KES/M-Pesa cases are BLOCKED.
**R3 Apply/remove rewards (portal):** type the amount, click Apply, wait 3-5 s before reading. Remove with the X, then wait again. An amount over the cap is rejected with a message that disappears after a few seconds (read it right away).
**R4 Stripe (staging only):** Pay By Card -> `/shipping-only/stripe/<id>?appliedRewards=N` -> click the second "Pay by Card" -> on Stripe choose USD -> card 4242 4242 4242 4242, expiry 12/34, CVC 123, cardholder name (required). For B6, leave Stripe without paying and check the wallet did not drop.
**R5 Wallet check:** portal wallet balance; CRM Contacts > <client> -> Total Rewards, Current Rewards, Rewards Used (wait ~10 s, reload if empty). Redemptions are in the order's Rewards Redeemed subpanel.
- In the CRM, an empty Rewards Redeemed subpanel or wallet total right after a change means it has not loaded yet: reload and read again before recording it.
**R6 Admin redemption:** CRM order -> Rewards Redeemed subpanel -> Create -> Redeemed Rewards <= Applicable Rewards -> Save. If Create returns HTTP 500: after two failures take a screenshot and record FAIL.
**R7 Earn rewards (staging only):** pay an order (R4 portal card, or CRM Payments subpanel when card payment cannot be made) -> CRM: set it to Order Delivered (earn = 5% of shipping after rewards). To earn faster raise the earn % under Administration > Rewards panel, then restore 5% earn / 10% redeem.
- Creating a brand-new shipping-only order: customer places it on the portal; CRM then receives the package, moves status, and invoices shipping (see pack placeholders / data_prep create parts).

## UI recipes
- Build a rewards balance: create a test order, pay it with the 4242 card, move it to Order Delivered in the CRM, then check the wallet.
- After applying rewards on the portal, reload twice before reading values (first reload can be stale).

## Known open bugs (drafts from the 28 Sep report, not posted; report as "still present: bug N")
1. [Critical] Portal rewards are committed (Used, wallet deducted) when Stripe checkout opens, not after payment. Abandoning checkout burns rewards.
2. [High] "You can use up to" and the replacement limit are computed on the rewards-reduced amount: $60 applied on $1,000 shows $34 (expected $40); $15 on $400 unpaid shows $23.50 (expected $25). Confirmed real by the user.
3. [High] CRM admin Rewards Redeemed saves as Applied; wallet not deducted on save; Used never incremented; status never becomes Used.
4. [High] Pre-existing wallet drift: Current (W) disagrees with Total minus Used (L − U), including cases where Used > Total (e.g. L=1.25, U=1.47, W=0). Portal Current may still match CRM Current.
5. [Medium] Portal apply does not persist when an admin redemption already exists on the order.
6. [Medium] Stale values on the first reload after apply.
7. [Medium] Buy & Ship: only M-Pesa offered, the exact decimal maximum is rejected, the total label does not update.
8. [Low] Wallet line still shown after removing rewards with the X button.
9. [Low] Invalid discount code gives no feedback.

## Not reproducible from the UI
- Webhook retry and orphan pending redemptions (#165348 A13, C11; #165177 B17).

## Test history
- #165177 / #165348 on staging: #165348 13 of 17 pass; #165177 5 pass, 9 fail, 3 partial, 1 not verified, 1 skipped. Report PDF: `qa/rewards/report/savo-rewards-qa-report-2026-09-28.pdf` (original; the later 97-page version with notes is in the user's 1:1 chat).
- #180966 on staging (25 Sep): portal cases run against the existing threshold; admin threshold cases blocked because the CRM account was not admin then. Production testing waits on accounts and approvals.

## Shipment Protection behaviour (confirmed on staging 2026-10-14)
- CRM Shipment Protection threshold rejects 12.5, 200.5, 0, -5, abc and blank silently: the form shows the typed value again with no message, and reopening the page shows the old value. Whole numbers like 200 save. Check the result by reloading, not by looking for a message.
- The rate accepts decimals (e.g. 2.5); the minimum accepts 0.
- Rounding is intended: the CRM protection cost shows whole dollars (4.02 shows as 4.00) while the USD field keeps cents.
- A client exemption always overrides the threshold. Declining protection means no charge anywhere.
- Portal Parcel Value drops decimals (200.50 becomes 200, pasting 25.50 gives 25) and 1,000 becomes 1000. 0 shows "Enter a value greater than zero"; typing "-" shows the "Whole dollars only" hint.
- Buy & Ship orders have no protection step.
- Fixing old orders (TC7 of #182941) is a repair script run on production; it can't be tested on staging.

## CRM repair before testing (verified on #183498, 2026-10-26)
- After a deploy, run Admin → Repair → Quick Repair and Rebuild before the first test, then hard-reload each EditView/DetailView. Clearing browser cache and cookies alone did not apply the change (Shipment Value only became read-only after QRR plus a hard reload).
- If Quick Repair and Rebuild ends with "Differences found between database and vardefs" and SQL with an Execute button, that SQL adds the new fields (columns, tables, indexes) the deploy needs, and the cases that test those fields fail until it runs. Read the full SQL from the page, pass it to check_repair_sql, ask with request_approval using the action_type it returns, click Execute only when approved, then run Quick Repair and Rebuild again and confirm no differences are listed. Additive SQL (execute_repair_sql) can be pre-approved for the run on staging; SQL that drops or changes anything (repair_sql_destructive) always asks. On production it always asks.

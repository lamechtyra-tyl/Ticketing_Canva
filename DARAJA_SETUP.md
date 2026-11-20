# Payment provider setup

## PayPal

Create a PayPal REST app and configure these environment variables on the Django
web process:

- `PAYPAL_ENV`: `sandbox` (default) or `production`.
- `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`: credentials for that PayPal app.
- `PAYPAL_KES_PER_USD`: KES exchanged for one USD. Defaults to `129.82`; this rate
  is configurable and should be kept current by the operator.

Ticket prices are fixed in KES (Regular 2,000, VIP 4,500, VVIP 6,000). PayPal
checkout uses USD and rounds the converted amount to two decimals using the
configured KES-per-USD rate. The server creates the PayPal order and captures it
after the customer returns from approval. A ticket is marked paid only after the
server verifies PayPal's completed capture, order ID, ticket reference, currency,
and amount. Sandbox credentials should be used for testing before production.

## M-Pesa Daraja

The app starts in sandbox mode and never simulates a successful payment. Set these
environment variables on the Django web process before starting it:

- `DARAJA_ENV`: `sandbox` (default) or `production`.
- `DARAJA_CONSUMER_KEY` and `DARAJA_CONSUMER_SECRET`: credentials for the selected Daraja app.
- `DARAJA_SHORTCODE` and `DARAJA_PASSKEY`: the shortcode and matching STK passkey.
- `DARAJA_CALLBACK_URL`: a public HTTPS URL ending in `/api/payments/callback/`.
- `DARAJA_TRANSACTION_TYPE`: `CustomerPayBillOnline` (default) or
  `CustomerBuyGoodsOnline`, matching the shortcode.

For local sandbox testing, expose the Django callback endpoint through a public
HTTPS tunnel and register the same URL with Daraja. Safaricom must be able to
reach it. Do not use a localhost URL or commit credentials to source control.
For example, set `DARAJA_CALLBACK_URL` to
`https://<active-public-host>/api/payments/callback/` in the same environment as
the Django server. The URL must use the current tunnel hostname and the exact
callback path; restart Django after changing it.

After configuration, apply the database changes with `python manage.py migrate`.
The booking UI allows one ticket category and one payment method per booking.
M-Pesa uses STK Push and waits for the callback to match the checkout ID, amount,
and phone number. PayPal redirects to its approval page and verifies the server-side
capture. Neither provider's checkout-start response alone marks a ticket paid.

Run the focused tests with:

```powershell
.\myvenv\Scripts\python.exe manage.py test canvaapp.tests --settings=canvaapp.test_settings
```

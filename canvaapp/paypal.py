from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlparse

import requests
from django.conf import settings


class PayPalError(Exception):
    pass


class PayPalConfigurationError(PayPalError):
    pass


class PayPalServiceError(PayPalError):
    pass


class PayPalRejectedError(PayPalError):
    pass


class PayPalAmbiguousError(PayPalError):
    pass


def _paypal_settings():
    base_url = settings.PAYPAL_BASE_URL.rstrip('/')
    if not base_url:
        raise PayPalConfigurationError(
            'PAYPAL_ENV must be sandbox or production.'
        )
    if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
        raise PayPalConfigurationError(
            'PayPal payments are not configured: client ID and client secret are required.'
        )
    if settings.PAYPAL_CURRENCY != 'USD':
        raise PayPalConfigurationError('PAYPAL_CURRENCY must be USD.')
    try:
        kes_per_usd = Decimal(str(settings.PAYPAL_KES_PER_USD))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PayPalConfigurationError(
            'PAYPAL_KES_PER_USD must be a positive decimal number.'
        ) from exc
    if not kes_per_usd.is_finite() or kes_per_usd <= 0:
        raise PayPalConfigurationError(
            'PAYPAL_KES_PER_USD must be a positive decimal number.'
        )
    return base_url, kes_per_usd


def paypal_amount(kes_amount):
    _, kes_per_usd = _paypal_settings()
    try:
        amount = (
            Decimal(str(kes_amount)) / kes_per_usd
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError('The ticket price cannot be converted to USD.') from exc
    if amount <= 0:
        raise ValueError('The converted PayPal amount must be at least USD 0.01.')
    return amount


def get_access_token():
    base_url, _ = _paypal_settings()
    try:
        response = requests.post(
            f'{base_url}/v1/oauth2/token',
            auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET),
            data={'grant_type': 'client_credentials'},
            headers={'Accept': 'application/json', 'Accept-Language': 'en_US'},
            timeout=(5, 20),
        )
    except requests.RequestException as exc:
        raise PayPalServiceError('Could not connect to PayPal.') from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise PayPalServiceError(
            'PayPal returned an invalid access-token response.'
        ) from exc
    if not response.ok or not isinstance(data, dict) or not data.get('access_token'):
        raise PayPalServiceError('PayPal did not provide an access token.')
    return data['access_token'], base_url


def create_order(ticket, amount, return_url, cancel_url, access_token, base_url):
    payload = {
        'intent': 'CAPTURE',
        'purchase_units': [
            {
                'reference_id': ticket.code,
                'custom_id': ticket.code,
                'description': f'{ticket.get_ticket_type_display()} - {ticket.event.title}',
                'amount': {
                    'currency_code': settings.PAYPAL_CURRENCY,
                    'value': f'{amount:.2f}',
                },
            }
        ],
        'application_context': {
            'return_url': return_url,
            'cancel_url': cancel_url,
            'user_action': 'PAY_NOW',
            'shipping_preference': 'NO_SHIPPING',
        },
    }
    try:
        response = requests.post(
            f'{base_url}/v2/checkout/orders',
            json=payload,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
                'PayPal-Request-Id': f'create-{ticket.code}',
            },
            timeout=(5, 30),
        )
    except requests.RequestException as exc:
        raise PayPalAmbiguousError(
            'PayPal did not confirm whether checkout was created. Check payment '
            'status before trying again.'
        ) from exc

    if response.status_code >= 500:
        raise PayPalAmbiguousError(
            'PayPal did not confirm whether checkout was created. Check payment '
            'status before trying again.'
        )
    try:
        data = response.json()
    except ValueError as exc:
        if response.status_code >= 400:
            raise PayPalRejectedError('PayPal rejected the checkout request.') from exc
        raise PayPalAmbiguousError(
            'PayPal returned an unreadable checkout response. Check payment status '
            'before trying again.'
        ) from exc

    if not response.ok or not isinstance(data, dict):
        raise PayPalRejectedError('PayPal rejected the checkout request.')

    order_id = data.get('id')
    approval_url = next(
        (
            link.get('href')
            for link in data.get('links', [])
            if isinstance(link, dict) and link.get('rel') == 'approve'
        ),
        None,
    )
    parsed_approval_url = urlparse(approval_url or '')
    if (
        not isinstance(order_id, str)
        or not order_id
        or parsed_approval_url.scheme != 'https'
        or parsed_approval_url.hostname
        not in ('www.paypal.com', 'www.sandbox.paypal.com')
    ):
        raise PayPalAmbiguousError(
            'PayPal did not return valid checkout details. Check payment status '
            'before trying again.'
        )
    return order_id, approval_url


def get_order(order_id, access_token, base_url):
    try:
        response = requests.get(
            f'{base_url}/v2/checkout/orders/{order_id}',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=(5, 20),
        )
    except requests.RequestException as exc:
        raise PayPalAmbiguousError(
            'Could not verify the PayPal order. The ticket is still pending.'
        ) from exc

    if response.status_code >= 500:
        raise PayPalAmbiguousError(
            'Could not verify the PayPal order. The ticket is still pending.'
        )
    try:
        data = response.json()
    except ValueError as exc:
        if response.status_code >= 400:
            raise PayPalRejectedError('PayPal could not find this order.') from exc
        raise PayPalAmbiguousError(
            'PayPal returned an unreadable order response.'
        ) from exc

    if not response.ok or not isinstance(data, dict):
        raise PayPalRejectedError('PayPal could not verify this order.')
    return data


def capture_order(order_id, access_token, base_url):
    try:
        response = requests.post(
            f'{base_url}/v2/checkout/orders/{order_id}/capture',
            json={},
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
                'PayPal-Request-Id': f'capture-{order_id}',
            },
            timeout=(5, 30),
        )
    except requests.RequestException as exc:
        raise PayPalAmbiguousError(
            'PayPal did not confirm the capture. The ticket is still pending; '
            'check payment status before retrying.'
        ) from exc

    if response.status_code >= 500:
        raise PayPalAmbiguousError(
            'PayPal did not confirm the capture. The ticket is still pending; '
            'check payment status before retrying.'
        )
    try:
        data = response.json()
    except ValueError as exc:
        if response.status_code >= 400:
            raise PayPalRejectedError('PayPal rejected the payment capture.') from exc
        raise PayPalAmbiguousError(
            'PayPal returned an unreadable capture response. The ticket is still pending.'
        ) from exc

    if not response.ok or not isinstance(data, dict):
        raise PayPalRejectedError('PayPal rejected the payment capture.')
    return data

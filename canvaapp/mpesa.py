import base64
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.urls import reverse


class DarajaError(Exception):
    pass


class DarajaConfigurationError(DarajaError):
    pass


class DarajaServiceError(DarajaError):
    pass


class DarajaRejectedError(DarajaError):
    pass


class DarajaAmbiguousError(DarajaError):
    pass


def normalize_ke_phone(value):
    if not isinstance(value, str):
        raise ValueError('Enter a valid Kenyan mobile number.')

    phone = re.sub(r'[\s()-]', '', value)
    if phone.startswith('+'):
        phone = phone[1:]

    if re.fullmatch(r'254[17]\d{8}', phone):
        return phone
    if re.fullmatch(r'0[17]\d{8}', phone):
        return '254' + phone[1:]
    if re.fullmatch(r'[17]\d{8}', phone):
        return '254' + phone
    raise ValueError('Use a Kenyan mobile number, e.g. 0712345678 or 254712345678.')


def stk_amount(event_price):
    amount = Decimal(event_price).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    if amount < 1:
        raise ValueError('The event price must be at least KES 1.')
    return amount


def _daraja_settings():
    base_url = settings.DARAJA_BASE_URL.rstrip('/')
    callback = urlparse(settings.DARAJA_CALLBACK_URL)
    required = {
        'consumer key': settings.DARAJA_CONSUMER_KEY,
        'consumer secret': settings.DARAJA_CONSUMER_SECRET,
        'shortcode': settings.DARAJA_SHORTCODE,
        'passkey': settings.DARAJA_PASSKEY,
    }
    missing = [label for label, value in required.items() if not value]
    if missing:
        raise DarajaConfigurationError(
            'M-Pesa payments are not configured: ' + ', '.join(missing) + '.'
        )
    if not settings.DARAJA_SHORTCODE.isdigit():
        raise DarajaConfigurationError('DARAJA_SHORTCODE must contain digits only.')
    if not base_url:
        raise DarajaConfigurationError('DARAJA_ENV must be sandbox or production.')
    if callback.scheme != 'https' or not callback.netloc:
        raise DarajaConfigurationError(
            'Set DARAJA_CALLBACK_URL to a publicly reachable HTTPS callback URL.'
        )
    callback_path = reverse('mpesa-callback')
    if callback.path.rstrip('/') != callback_path.rstrip('/'):
        raise DarajaConfigurationError(
            f'DARAJA_CALLBACK_URL must point to the {callback_path} endpoint.'
        )
    if settings.DARAJA_TRANSACTION_TYPE not in (
        'CustomerPayBillOnline',
        'CustomerBuyGoodsOnline',
    ):
        raise DarajaConfigurationError('DARAJA_TRANSACTION_TYPE is invalid.')
    return base_url


def get_access_token():
    base_url = _daraja_settings()
    try:
        response = requests.get(
            f'{base_url}/oauth/v1/generate?grant_type=client_credentials',
            auth=(settings.DARAJA_CONSUMER_KEY, settings.DARAJA_CONSUMER_SECRET),
            timeout=(5, 20),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DarajaServiceError('Could not connect to Safaricom M-Pesa.') from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise DarajaServiceError('Safaricom returned an invalid access-token response.') from exc

    if not isinstance(data, dict):
        raise DarajaServiceError('Safaricom returned an invalid access-token response.')
    access_token = data.get('access_token')
    if not access_token:
        raise DarajaServiceError('Safaricom did not return an access token.')
    return access_token, base_url


def send_stk_push(ticket, phone, amount, access_token, base_url):
    local_time = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=3))
    )
    timestamp = local_time.strftime('%Y%m%d%H%M%S')
    password = base64.b64encode(
        (
            f'{settings.DARAJA_SHORTCODE}'
            f'{settings.DARAJA_PASSKEY}'
            f'{timestamp}'
        ).encode('utf-8')
    ).decode('ascii')
    payload = {
        'BusinessShortCode': settings.DARAJA_SHORTCODE,
        'Password': password,
        'Timestamp': timestamp,
        'TransactionType': settings.DARAJA_TRANSACTION_TYPE,
        'Amount': int(amount),
        'PartyA': phone,
        'PartyB': settings.DARAJA_SHORTCODE,
        'PhoneNumber': phone,
        'CallBackURL': settings.DARAJA_CALLBACK_URL,
        'AccountReference': ticket.code,
        'TransactionDesc': 'Event ticket',
    }

    try:
        response = requests.post(
            f'{base_url}/mpesa/stkpush/v1/processrequest',
            json=payload,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=(5, 30),
        )
    except requests.RequestException as exc:
        raise DarajaAmbiguousError(
            'Safaricom did not confirm whether the STK prompt was sent. '
            'Do not retry yet; check the payment status or contact the organizer.'
        ) from exc

    if response.status_code >= 500:
        raise DarajaAmbiguousError(
            'Safaricom did not confirm whether the STK prompt was sent. '
            'Do not retry yet; check the payment status or contact the organizer.'
        )

    try:
        data = response.json()
    except ValueError as exc:
        if response.status_code >= 400:
            raise DarajaRejectedError('Safaricom rejected the STK request.') from exc
        raise DarajaAmbiguousError(
            'Safaricom returned an unreadable STK response. Check the payment status '
            'before retrying.'
        ) from exc

    if not isinstance(data, dict):
        if response.status_code >= 400:
            raise DarajaRejectedError('Safaricom rejected the STK request.')
        raise DarajaAmbiguousError(
            'Safaricom returned an unreadable STK response. Check the payment status '
            'before retrying.'
        )

    if not response.ok or str(data.get('ResponseCode')) != '0':
        raise DarajaRejectedError(
            data.get('ResponseDescription')
            or data.get('errorMessage')
            or 'Safaricom rejected the STK request.'
        )
    if not data.get('CheckoutRequestID') or not data.get('MerchantRequestID'):
        raise DarajaAmbiguousError(
            'Safaricom accepted the request but did not return payment identifiers. '
            'Check the payment status before retrying.'
        )
    return data

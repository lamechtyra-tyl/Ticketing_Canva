from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Event, Payment, TICKET_PRICES, Ticket
from .mpesa import normalize_ke_phone
from .paypal import paypal_amount


class KenyanPhoneNumberTests(TestCase):
    def test_normalizes_common_kenyan_mobile_formats(self):
        self.assertEqual(normalize_ke_phone('0712 345 678'), '254712345678')
        self.assertEqual(normalize_ke_phone('+254 (712) 345-678'), '254712345678')
        self.assertEqual(normalize_ke_phone('112345678'), '254112345678')

    def test_rejects_non_kenyan_mobile_numbers(self):
        with self.assertRaises(ValueError):
            normalize_ke_phone('0201234567')


class TicketPaymentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.event = Event.objects.create(
            title='Canva World Tour Nairobi',
            description='Creative technology event',
            venue='Nairobi',
            date=timezone.now() + timedelta(days=30),
            price=Decimal('2000.00'),
            capacity=2,
        )

    def create_ticket(self, phone='0712345678', ticket_type='regular'):
        return Ticket.objects.create(
            event=self.event,
            buyer_name='Test Buyer',
            phone=normalize_ke_phone(phone) if phone else '',
            ticket_type=ticket_type,
            price=TICKET_PRICES[ticket_type],
        )

    def callback_data(
        self, checkout_id, amount=2000, phone=254712345678, merchant_id='merchant-1'
    ):
        return {
            'Body': {
                'stkCallback': {
                    'MerchantRequestID': merchant_id,
                    'CheckoutRequestID': checkout_id,
                    'ResultCode': 0,
                    'ResultDesc': 'The service request is processed successfully.',
                    'CallbackMetadata': {
                        'Item': [
                            {'Name': 'Amount', 'Value': amount},
                            {'Name': 'MpesaReceiptNumber', 'Value': 'QWE123ABC'},
                            {'Name': 'PhoneNumber', 'Value': phone},
                        ]
                    },
                }
            }
        }

    def test_ticket_creation_normalizes_phone(self):
        response = self.client.post(
            '/api/tickets/',
            {
                'event': self.event.pk,
                'buyer_name': 'Test Buyer',
                'phone': '+254 712 345 678',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        ticket = Ticket.objects.get(code=response.data['code'])
        self.assertEqual(ticket.phone, '254712345678')
        self.assertEqual(ticket.status, 'pending')

    def test_ticket_creation_rejects_invalid_phone(self):
        response = self.client.post(
            '/api/tickets/',
            {
                'event': self.event.pk,
                'buyer_name': 'Test Buyer',
                'phone': '0201234567',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Ticket.objects.exists())

    def test_homepage_renders_the_styled_m_pesa_booking_form(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="booking-form"')
        self.assertContains(response, 'Get Ticket with M-Pesa')
        self.assertContains(response, 'Regular Ticket')
        self.assertContains(response, 'KES 4,500')
        self.assertContains(response, 'value="paypal"')
        self.assertContains(response, 'data-kes-per-usd="129.82"')
        self.assertContains(response, 'assets/css/booking-theme.css')

    def test_ticket_creation_stores_selected_category_and_price_without_phone(self):
        response = self.client.post(
            '/api/tickets/',
            {
                'event': self.event.pk,
                'buyer_name': 'Test Buyer',
                'ticket_type': 'vvip',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        ticket = Ticket.objects.get(code=response.data['code'])
        self.assertEqual(ticket.ticket_type, 'vvip')
        self.assertEqual(ticket.price, Decimal('6000.00'))
        self.assertEqual(ticket.phone, '')

    @override_settings(
        PAYPAL_BASE_URL='https://api-m.sandbox.paypal.com',
        PAYPAL_CLIENT_ID='test-client',
        PAYPAL_CLIENT_SECRET='test-secret',
        PAYPAL_CURRENCY='USD',
        PAYPAL_KES_PER_USD='129.82',
    )
    def test_paypal_amount_uses_configured_kes_exchange_rate(self):
        self.assertEqual(paypal_amount(Decimal('2000.00')), Decimal('15.41'))
        self.assertEqual(paypal_amount(Decimal('4500.00')), Decimal('34.66'))
        self.assertEqual(paypal_amount(Decimal('6000.00')), Decimal('46.22'))

    @override_settings(
        DARAJA_BASE_URL='https://sandbox.safaricom.co.ke',
        DARAJA_CALLBACK_URL='https://example.test/api/mpesa/callback/',
        DARAJA_CONSUMER_KEY='test-key',
        DARAJA_CONSUMER_SECRET='test-secret',
        DARAJA_SHORTCODE='174379',
        DARAJA_PASSKEY='test-passkey',
        DARAJA_TRANSACTION_TYPE='CustomerPayBillOnline',
    )
    @patch('canvaapp.mpesa.requests.post')
    @patch('canvaapp.mpesa.requests.get')
    def test_stk_push_keeps_ticket_pending_until_callback(
        self, mock_get, mock_post
    ):
        ticket = self.create_ticket(ticket_type='vip')
        token_response = Mock()
        token_response.json.return_value = {'access_token': 'test-access-token'}
        mock_get.return_value = token_response
        stk_response = Mock()
        stk_response.status_code = 200
        stk_response.ok = True
        stk_response.json.return_value = {
            'ResponseCode': '0',
            'MerchantRequestID': 'merchant-1',
            'CheckoutRequestID': 'checkout-1',
            'CustomerMessage': 'Success. Request accepted for processing',
        }
        mock_post.return_value = stk_response

        response = self.client.post(
            '/api/mpesa/stkpush/',
            {'code': ticket.code},
            format='json',
        )

        self.assertEqual(response.status_code, 202)
        ticket.refresh_from_db()
        payment = Payment.objects.get(ticket=ticket)
        self.assertEqual(ticket.status, 'pending')
        self.assertEqual(payment.status, 'pending')
        self.assertEqual(payment.amount, Decimal('4500'))
        self.assertEqual(payment.currency, 'KES')
        self.assertEqual(payment.checkout_request_id, 'checkout-1')
        self.assertEqual(mock_post.call_args.kwargs['json']['PhoneNumber'], '254712345678')
        self.assertEqual(mock_post.call_args.kwargs['json']['Amount'], 4500)

    @override_settings(
        PAYPAL_BASE_URL='https://api-m.sandbox.paypal.com',
        PAYPAL_CLIENT_ID='test-client',
        PAYPAL_CLIENT_SECRET='test-secret',
        PAYPAL_CURRENCY='USD',
        PAYPAL_KES_PER_USD='129.82',
    )
    @patch('canvaapp.paypal.requests.post')
    def test_paypal_checkout_uses_ticket_price_and_redirects_to_approval(
        self, mock_post
    ):
        ticket = self.create_ticket(phone='', ticket_type='vvip')
        token_response = Mock()
        token_response.ok = True
        token_response.json.return_value = {'access_token': 'paypal-token'}
        order_response = Mock()
        order_response.status_code = 201
        order_response.ok = True
        order_response.json.return_value = {
            'id': 'PAYPAL-ORDER-123',
            'links': [
                {
                    'rel': 'approve',
                    'href': 'https://www.sandbox.paypal.com/checkoutnow?token=PAYPAL-ORDER-123',
                }
            ],
        }
        mock_post.side_effect = [token_response, order_response]

        response = self.client.post(
            '/api/paypal/create-order/',
            {'code': ticket.code},
            format='json',
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['amount'], '46.22')
        self.assertEqual(response.data['ticket_amount'], '6000.00')
        self.assertEqual(response.data['currency'], 'USD')
        self.assertEqual(
            response.data['approval_url'],
            'https://www.sandbox.paypal.com/checkoutnow?token=PAYPAL-ORDER-123',
        )
        payment = Payment.objects.get(ticket=ticket)
        self.assertEqual(payment.method, 'paypal')
        self.assertEqual(payment.amount, Decimal('46.22'))
        self.assertEqual(payment.paypal_order_id, 'PAYPAL-ORDER-123')
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'pending')

    @override_settings(
        PAYPAL_BASE_URL='https://api-m.sandbox.paypal.com',
        PAYPAL_CLIENT_ID='test-client',
        PAYPAL_CLIENT_SECRET='test-secret',
        PAYPAL_CURRENCY='USD',
        PAYPAL_KES_PER_USD='129.82',
    )
    @patch('canvaapp.paypal.requests.post')
    @patch('canvaapp.paypal.requests.get')
    def test_paypal_ticket_is_paid_only_after_matching_capture(
        self, mock_get, mock_post
    ):
        ticket = self.create_ticket(phone='', ticket_type='vip')
        payment = Payment.objects.create(
            ticket=ticket,
            method='paypal',
            phone='',
            amount=Decimal('34.66'),
            currency='USD',
            paypal_order_id='PAYPAL-ORDER-CAPTURE',
        )
        order_response = Mock()
        order_response.status_code = 200
        order_response.ok = True
        order_response.json.return_value = {
            'id': 'PAYPAL-ORDER-CAPTURE',
            'status': 'APPROVED',
        }
        mock_get.return_value = order_response
        token_response = Mock()
        token_response.ok = True
        token_response.json.return_value = {'access_token': 'paypal-token'}
        capture_response = Mock()
        capture_response.status_code = 201
        capture_response.ok = True
        capture_response.json.return_value = {
            'id': 'PAYPAL-ORDER-CAPTURE',
            'status': 'COMPLETED',
            'purchase_units': [
                {
                    'custom_id': ticket.code,
                    'payments': {
                        'captures': [
                            {
                                'id': 'PAYPAL-CAPTURE-456',
                                'status': 'COMPLETED',
                                'amount': {
                                    'currency_code': 'USD',
                                    'value': '34.66',
                                },
                            }
                        ]
                    },
                }
            ],
        }
        mock_post.side_effect = [token_response, capture_response]

        response = self.client.post(
            '/api/paypal/capture/',
            {'code': ticket.code},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(ticket.status, 'paid')
        self.assertEqual(payment.status, 'success')
        self.assertEqual(payment.paypal_capture_id, 'PAYPAL-CAPTURE-456')

    @override_settings(
        PAYPAL_BASE_URL='https://api-m.sandbox.paypal.com',
        PAYPAL_CLIENT_ID='test-client',
        PAYPAL_CLIENT_SECRET='test-secret',
        PAYPAL_CURRENCY='USD',
        PAYPAL_KES_PER_USD='129.82',
    )
    @patch('canvaapp.paypal.requests.post')
    @patch('canvaapp.paypal.requests.get')
    def test_paypal_mismatched_capture_does_not_confirm_ticket(
        self, mock_get, mock_post
    ):
        ticket = self.create_ticket(phone='', ticket_type='vip')
        Payment.objects.create(
            ticket=ticket,
            method='paypal',
            phone='',
            amount=Decimal('34.66'),
            currency='USD',
            paypal_order_id='PAYPAL-ORDER-MISMATCH',
        )
        order_response = Mock()
        order_response.status_code = 200
        order_response.ok = True
        order_response.json.return_value = {
            'id': 'PAYPAL-ORDER-MISMATCH',
            'status': 'APPROVED',
        }
        mock_get.return_value = order_response
        token_response = Mock()
        token_response.ok = True
        token_response.json.return_value = {'access_token': 'paypal-token'}
        capture_response = Mock()
        capture_response.status_code = 201
        capture_response.ok = True
        capture_response.json.return_value = {
            'id': 'PAYPAL-ORDER-MISMATCH',
            'status': 'COMPLETED',
            'purchase_units': [
                {
                    'custom_id': ticket.code,
                    'payments': {
                        'captures': [
                            {
                                'id': 'PAYPAL-CAPTURE-WRONG-AMOUNT',
                                'status': 'COMPLETED',
                                'amount': {
                                    'currency_code': 'USD',
                                    'value': '30.00',
                                },
                            }
                        ]
                    },
                }
            ],
        }
        mock_post.side_effect = [token_response, capture_response]

        response = self.client.post(
            '/api/paypal/capture/',
            {'code': ticket.code},
            format='json',
        )

        self.assertEqual(response.status_code, 202)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'pending')
        self.assertEqual(
            Payment.objects.get(ticket=ticket).status,
            'pending',
        )

    @override_settings(
        PAYPAL_BASE_URL='https://api-m.sandbox.paypal.com',
        PAYPAL_CLIENT_ID='test-client',
        PAYPAL_CLIENT_SECRET='test-secret',
        PAYPAL_CURRENCY='USD',
        PAYPAL_KES_PER_USD='129.82',
    )
    @patch('canvaapp.paypal.requests.post')
    @patch('canvaapp.paypal.requests.get')
    def test_paypal_order_is_not_captured_before_approval(
        self, mock_get, mock_post
    ):
        ticket = self.create_ticket(phone='')
        Payment.objects.create(
            ticket=ticket,
            method='paypal',
            phone='',
            amount=Decimal('15.41'),
            currency='USD',
            paypal_order_id='PAYPAL-ORDER-NOT-APPROVED',
        )
        order_response = Mock()
        order_response.status_code = 200
        order_response.ok = True
        order_response.json.return_value = {
            'id': 'PAYPAL-ORDER-NOT-APPROVED',
            'status': 'CREATED',
        }
        mock_get.return_value = order_response
        token_response = Mock()
        token_response.ok = True
        token_response.json.return_value = {'access_token': 'paypal-token'}
        mock_post.return_value = token_response

        response = self.client.post(
            '/api/paypal/capture/',
            {'code': ticket.code},
            format='json',
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(mock_post.call_count, 1)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'pending')
        self.assertEqual(
            Payment.objects.get(ticket=ticket).status,
            'pending',
        )

    def test_successful_callback_confirms_matching_payment(self):
        ticket = self.create_ticket()
        Payment.objects.create(
            ticket=ticket,
            phone=ticket.phone,
            amount=Decimal('2000'),
            merchant_request_id='merchant-1',
            checkout_request_id='checkout-success',
        )

        response = self.client.post(
            '/api/mpesa/callback/',
            self.callback_data('checkout-success'),
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        payment = Payment.objects.get(checkout_request_id='checkout-success')
        self.assertEqual(ticket.status, 'paid')
        self.assertEqual(payment.status, 'success')
        self.assertEqual(payment.mpesa_receipt, 'QWE123ABC')
        status_response = self.client.get(
            f'/api/tickets/{ticket.code}/payment-status/'
        )
        self.assertEqual(status_response.data['status'], 'paid')
        self.assertEqual(status_response.data['receipt'], 'QWE123ABC')

    def test_mismatched_merchant_request_does_not_confirm_ticket(self):
        ticket = self.create_ticket()
        Payment.objects.create(
            ticket=ticket,
            phone=ticket.phone,
            amount=Decimal('2000'),
            merchant_request_id='merchant-expected',
            checkout_request_id='checkout-merchant-mismatch',
        )

        response = self.client.post(
            '/api/mpesa/callback/',
            self.callback_data(
                'checkout-merchant-mismatch',
                merchant_id='merchant-other',
            ),
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'pending')

    def test_mismatched_callback_does_not_confirm_ticket(self):
        ticket = self.create_ticket()
        Payment.objects.create(
            ticket=ticket,
            phone=ticket.phone,
            amount=Decimal('2000'),
            checkout_request_id='checkout-mismatch',
        )

        response = self.client.post(
            '/api/mpesa/callback/',
            self.callback_data('checkout-mismatch', amount=1000),
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'pending')

    def test_failed_callback_marks_ticket_failed(self):
        ticket = self.create_ticket()
        Payment.objects.create(
            ticket=ticket,
            phone=ticket.phone,
            amount=Decimal('2000'),
            checkout_request_id='checkout-failed',
        )

        response = self.client.post(
            '/api/mpesa/callback/',
            {
                'Body': {
                    'stkCallback': {
                        'CheckoutRequestID': 'checkout-failed',
                        'ResultCode': 1032,
                        'ResultDesc': 'Request cancelled by user.',
                    }
                }
            },
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'failed')
        self.assertEqual(
            Payment.objects.get(checkout_request_id='checkout-failed').status,
            'failed',
        )

    def test_missing_daraja_configuration_never_marks_ticket_paid(self):
        ticket = self.create_ticket()
        with override_settings(
            DARAJA_BASE_URL='https://sandbox.safaricom.co.ke',
            DARAJA_CALLBACK_URL='',
            DARAJA_CONSUMER_KEY='',
            DARAJA_CONSUMER_SECRET='',
            DARAJA_SHORTCODE='',
            DARAJA_PASSKEY='',
            DARAJA_TRANSACTION_TYPE='CustomerPayBillOnline',
        ):
            response = self.client.post(
                '/api/mpesa/stkpush/',
                {'code': ticket.code},
                format='json',
            )

        self.assertEqual(response.status_code, 503)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'failed')
        self.assertFalse(Payment.objects.filter(ticket=ticket).exists())

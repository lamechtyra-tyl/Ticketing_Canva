from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Event, Payment, Ticket
from .mpesa import (
    DarajaAmbiguousError,
    DarajaConfigurationError,
    DarajaRejectedError,
    DarajaServiceError,
    get_access_token,
    normalize_ke_phone,
    send_stk_push,
    stk_amount,
)
from .paypal import (
    PayPalAmbiguousError,
    PayPalConfigurationError,
    PayPalRejectedError,
    PayPalServiceError,
    capture_order,
    create_order,
    get_access_token as get_paypal_access_token,
    get_order,
    paypal_amount,
)
from .serializers import EventSerializer, TicketSerializer


def home(request):
    events = Event.objects.all().order_by('date')
    return render(
        request,
        'base.html',
        {
            'events': events,
            'paypal_kes_per_usd': settings.PAYPAL_KES_PER_USD,
        },
    )


class EventViewSet(viewsets.ModelViewSet):
    queryset = Event.objects.all()
    serializer_class = EventSerializer

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [permissions.AllowAny()]
        return [permissions.IsAdminUser()]


class TicketViewSet(mixins.CreateModelMixin,
                    mixins.RetrieveModelMixin,
                    viewsets.GenericViewSet):
    queryset = Ticket.objects.select_related('event')
    serializer_class = TicketSerializer
    lookup_field = 'code'
    authentication_classes = []
    permission_classes = [permissions.AllowAny]


class StkPush(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        code = request.data.get('code') if isinstance(request.data, dict) else None
        if not isinstance(code, str) or not code.strip():
            return Response(
                {'error': 'A ticket code is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            ticket = Ticket.objects.select_related('event').get(code=code.strip())
        except Ticket.DoesNotExist:
            return Response(
                {'error': 'Ticket not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if ticket.status == 'paid':
            return Response(
                {'error': 'This ticket has already been paid.'},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            phone = normalize_ke_phone(ticket.phone)
            amount = stk_amount(ticket.price)
            access_token, base_url = get_access_token()
        except (ValueError, InvalidOperation) as exc:
            _fail_ticket(ticket)
            return Response(
                {'error': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except (DarajaConfigurationError, DarajaServiceError) as exc:
            _fail_ticket(ticket)
            return Response(
                {'error': str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        with transaction.atomic():
            ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
            if ticket.status == 'paid':
                return Response(
                    {'error': 'This ticket has already been paid.'},
                    status=status.HTTP_409_CONFLICT,
                )
            if ticket.payments.filter(status='pending').exists():
                return Response(
                    {
                        'error': (
                            'A payment request is already in progress for this ticket.'
                        ),
                        'code': ticket.code,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            payment = Payment.objects.create(
                ticket=ticket,
                method='mpesa',
                phone=phone,
                amount=amount,
                currency='KES',
            )
            ticket.phone = phone
            ticket.status = 'pending'
            ticket.save(update_fields=['phone', 'status'])

        try:
            data = send_stk_push(
                ticket=ticket,
                phone=phone,
                amount=amount,
                access_token=access_token,
                base_url=base_url,
            )
        except DarajaRejectedError as exc:
            _fail_payment(payment.pk, str(exc))
            return Response(
                {'error': str(exc), 'code': ticket.code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except DarajaAmbiguousError as exc:
            Payment.objects.filter(pk=payment.pk, status='pending').update(
                result_description=str(exc)[:255],
            )
            return Response(
                {
                    'message': str(exc),
                    'code': ticket.code,
                    'status': 'pending',
                },
                status=status.HTTP_202_ACCEPTED,
            )

        Payment.objects.filter(pk=payment.pk).update(
            checkout_request_id=data['CheckoutRequestID'],
            merchant_request_id=data['MerchantRequestID'],
        )
        return Response(
            {
                'message': (
                    data.get('CustomerMessage')
                    or 'M-Pesa prompt sent. Enter your PIN on your phone to continue.'
                ),
                'code': ticket.code,
                'status': 'pending',
                'amount': str(amount),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class PayPalOrder(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        code = request.data.get('code') if isinstance(request.data, dict) else None
        if not isinstance(code, str) or not code.strip():
            return Response(
                {'error': 'A ticket code is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            ticket = Ticket.objects.select_related('event').get(code=code.strip())
        except Ticket.DoesNotExist:
            return Response(
                {'error': 'Ticket not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if ticket.status == 'paid':
            return Response(
                {'error': 'This ticket has already been paid.'},
                status=status.HTTP_409_CONFLICT,
            )

        try:
            amount = paypal_amount(ticket.price)
            access_token, base_url = get_paypal_access_token()
        except ValueError as exc:
            _fail_ticket(ticket)
            return Response(
                {'error': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except (PayPalConfigurationError, PayPalServiceError) as exc:
            _fail_ticket(ticket)
            return Response(
                {'error': str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        with transaction.atomic():
            ticket = Ticket.objects.select_for_update().select_related('event').get(
                pk=ticket.pk
            )
            if ticket.status == 'paid':
                return Response(
                    {'error': 'This ticket has already been paid.'},
                    status=status.HTTP_409_CONFLICT,
                )
            if ticket.payments.filter(status='pending').exists():
                return Response(
                    {
                        'error': (
                            'A payment request is already in progress for this ticket.'
                        ),
                        'code': ticket.code,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            payment = Payment.objects.create(
                ticket=ticket,
                method='paypal',
                phone=ticket.phone,
                amount=amount,
                currency='USD',
            )
            ticket.status = 'pending'
            ticket.save(update_fields=['status'])

        return_url = request.build_absolute_uri(reverse('paypal-return'))
        cancel_url = request.build_absolute_uri(
            reverse('paypal-cancel')
            + '?'
            + urlencode({'ticket': ticket.code})
        )
        try:
            order_id, approval_url = create_order(
                ticket=ticket,
                amount=amount,
                return_url=return_url,
                cancel_url=cancel_url,
                access_token=access_token,
                base_url=base_url,
            )
        except PayPalRejectedError as exc:
            _fail_payment(payment.pk, str(exc))
            return Response(
                {'error': str(exc), 'code': ticket.code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except PayPalAmbiguousError as exc:
            Payment.objects.filter(pk=payment.pk, status='pending').update(
                result_description=str(exc)[:255],
            )
            return Response(
                {
                    'message': str(exc),
                    'code': ticket.code,
                    'status': 'pending',
                },
                status=status.HTTP_202_ACCEPTED,
            )

        Payment.objects.filter(pk=payment.pk).update(
            paypal_order_id=order_id,
            paypal_approval_url=approval_url,
        )
        return Response(
            {
                'message': 'PayPal checkout is ready.',
                'code': ticket.code,
                'status': 'pending',
                'amount': str(amount),
                'currency': 'USD',
                'ticket_amount': str(ticket.price),
                'ticket_currency': 'KES',
                'approval_url': approval_url,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class PayPalCapture(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        code = request.data.get('code') if isinstance(request.data, dict) else None
        if not isinstance(code, str) or not code.strip():
            return Response(
                {'error': 'A ticket code is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ticket = Ticket.objects.filter(code=code.strip()).first()
        if ticket is None:
            return Response(
                {'error': 'Ticket not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        payment = ticket.payments.filter(
            method='paypal',
            status='pending',
            paypal_order_id__isnull=False,
        ).order_by('-created_at', '-pk').first()
        if payment is None:
            if ticket.status == 'paid':
                return Response({'status': 'paid', 'code': ticket.code})
            return Response(
                {'error': 'There is no pending PayPal checkout for this ticket.'},
                status=status.HTTP_409_CONFLICT,
            )

        result = _capture_paypal_payment(payment)
        ticket.refresh_from_db()
        payment.refresh_from_db()
        return Response(
            {
                'code': ticket.code,
                'status': ticket.status,
                'payment_status': payment.status,
                'message': (
                    'PayPal payment confirmed.'
                    if payment.status == 'success'
                    else payment.result_description
                    or 'Waiting for PayPal payment confirmation.'
                ),
            },
            status=(
                status.HTTP_200_OK
                if result == 'paid'
                else status.HTTP_202_ACCEPTED
            ),
        )


class PayPalReturn(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        order_id = request.query_params.get('token')
        payment = (
            Payment.objects.select_related('ticket')
            .filter(method='paypal', paypal_order_id=order_id)
            .first()
            if order_id
            else None
        )
        if payment is None:
            return _paypal_result_redirect('error')

        result = (
            'paid'
            if payment.status == 'success'
            else _capture_paypal_payment(payment)
        )
        if result == 'paid':
            return _paypal_result_redirect('complete', payment.ticket.code)
        if result == 'failed':
            return _paypal_result_redirect('failed', payment.ticket.code)
        return _paypal_result_redirect('pending', payment.ticket.code)


class PayPalCancel(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        code = request.query_params.get('ticket')
        return _paypal_result_redirect('cancelled', code)


class TicketPaymentStatus(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, code):
        try:
            ticket = Ticket.objects.get(code=code)
        except Ticket.DoesNotExist:
            return Response(
                {'error': 'Ticket not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        payment = ticket.payments.order_by('-created_at', '-pk').first()
        payment_status = ticket.status
        payment_method = payment.method if payment else None
        if payment_status == 'paid':
            message = (
                'PayPal payment confirmed.'
                if payment_method == 'paypal'
                else 'M-Pesa payment confirmed.'
            )
        elif payment_status == 'failed':
            message = 'Payment was not completed.'
        else:
            message = (
                payment.result_description
                if payment and payment.result_description
                else (
                    'Waiting for PayPal payment confirmation.'
                    if payment_method == 'paypal'
                    else 'Waiting for Safaricom payment confirmation.'
                )
            )

        return Response(
            {
                'code': ticket.code,
                'status': payment_status,
                'message': message,
                'receipt': (
                    payment.mpesa_receipt
                    if payment_status == 'paid' and payment
                    else None
                ),
                'capture_id': (
                    payment.paypal_capture_id
                    if payment_status == 'paid'
                    and payment
                    and payment.method == 'paypal'
                    else None
                ),
                'payment_method': payment_method,
                'amount': str(payment.amount) if payment else None,
                'currency': payment.currency if payment else None,
                'approval_url': (
                    payment.paypal_approval_url
                    if payment
                    and payment.method == 'paypal'
                    and payment.status == 'pending'
                    else None
                ),
            }
        )


class MpesaCallback(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        body = request.data.get('Body') if isinstance(request.data, dict) else None
        callback = body.get('stkCallback') if isinstance(body, dict) else None
        if not isinstance(callback, dict):
            return Response(
                {'ResultCode': 1, 'ResultDesc': 'Invalid callback body.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        checkout_id = callback.get('CheckoutRequestID')
        try:
            result_code = int(callback.get('ResultCode'))
        except (TypeError, ValueError):
            return Response(
                {'ResultCode': 1, 'ResultDesc': 'Invalid callback result code.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(checkout_id, str) or not checkout_id:
            return Response(
                {'ResultCode': 1, 'ResultDesc': 'Checkout request ID is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            payment = (
                Payment.objects.select_for_update()
                .select_related('ticket')
                .filter(checkout_request_id=checkout_id)
                .first()
            )
            if payment is None:
                return Response(
                    {'ResultCode': 1, 'ResultDesc': 'Payment request not found.'},
                    status=status.HTTP_404_NOT_FOUND,
                )

            if payment.method != 'mpesa':
                return Response(
                    {
                        'ResultCode': 1,
                        'ResultDesc': 'Payment method does not match the callback.',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if (
                payment.merchant_request_id
                and callback.get('MerchantRequestID') != payment.merchant_request_id
            ):
                return Response(
                    {
                        'ResultCode': 1,
                        'ResultDesc': 'Merchant request ID does not match the payment.',
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if payment.status in ('success', 'failed'):
                return Response({'ResultCode': 0, 'ResultDesc': 'Accepted.'})

            if result_code == 0:
                metadata = callback.get('CallbackMetadata', {})
                items = metadata.get('Item', []) if isinstance(metadata, dict) else []
                values = {
                    item.get('Name'): item.get('Value')
                    for item in items
                    if isinstance(item, dict) and item.get('Name')
                } if isinstance(items, list) else {}
                try:
                    callback_amount = Decimal(str(values['Amount']))
                    callback_phone = normalize_ke_phone(str(values['PhoneNumber']))
                    receipt_value = values['MpesaReceiptNumber']
                except (KeyError, ValueError, InvalidOperation):
                    return Response(
                        {
                            'ResultCode': 1,
                            'ResultDesc': 'Successful callback metadata is incomplete.',
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if not isinstance(receipt_value, str) or not receipt_value.strip():
                    return Response(
                        {
                            'ResultCode': 1,
                            'ResultDesc': 'Successful callback metadata is incomplete.',
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                receipt = receipt_value.strip()

                if (
                    callback_amount != payment.amount
                    or callback_phone != payment.phone
                    or not receipt
                ):
                    return Response(
                        {
                            'ResultCode': 1,
                            'ResultDesc': 'Callback details do not match the payment.',
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                payment.status = 'success'
                payment.mpesa_receipt = receipt[:50]
                payment.result_code = result_code
                payment.result_description = str(callback.get('ResultDesc', ''))[:255]
                payment.save(
                    update_fields=[
                        'status',
                        'mpesa_receipt',
                        'result_code',
                        'result_description',
                    ]
                )
                Ticket.objects.filter(pk=payment.ticket_id).update(status='paid')
            else:
                description = str(
                    callback.get('ResultDesc') or 'M-Pesa payment was not completed.'
                )[:255]
                payment.status = 'failed'
                payment.result_code = result_code
                payment.result_description = description
                payment.save(
                    update_fields=['status', 'result_code', 'result_description']
                )
                Ticket.objects.filter(
                    pk=payment.ticket_id
                ).exclude(status='paid').update(status='failed')

        return Response({'ResultCode': 0, 'ResultDesc': 'Accepted.'})


def _paypal_result_redirect(result, code=None):
    params = {'paypal_result': result}
    if isinstance(code, str) and code:
        params['code'] = code
    return redirect('/?' + urlencode(params) + '#registration')


def _capture_paypal_payment(payment):
    if payment.status == 'success':
        return 'paid'
    if payment.status != 'pending' or not payment.paypal_order_id:
        return payment.status

    try:
        access_token, base_url = get_paypal_access_token()
        order = get_order(payment.paypal_order_id, access_token, base_url)
        if order.get('id') != payment.paypal_order_id:
            raise PayPalAmbiguousError('PayPal returned a different order.')
        if order.get('status') == 'APPROVED':
            order = capture_order(
                payment.paypal_order_id,
                access_token,
                base_url,
            )
    except PayPalRejectedError as exc:
        _set_pending_payment_description(payment.pk, str(exc))
        return 'pending'
    except (PayPalConfigurationError, PayPalServiceError, PayPalAmbiguousError) as exc:
        _set_pending_payment_description(payment.pk, str(exc))
        return 'pending'

    if order.get('status') != 'COMPLETED':
        _set_pending_payment_description(
            payment.pk, 'Waiting for PayPal payment approval and capture.'
        )
        return 'pending'

    purchase_units = order.get('purchase_units')
    if not isinstance(purchase_units, list) or len(purchase_units) != 1:
        _set_pending_payment_description(
            payment.pk, 'PayPal order details could not be verified.'
        )
        return 'pending'
    purchase_unit = purchase_units[0]
    captures = (
        purchase_unit.get('payments', {}).get('captures', [])
        if isinstance(purchase_unit, dict)
        and isinstance(purchase_unit.get('payments'), dict)
        else []
    )
    if not isinstance(captures, list) or len(captures) != 1:
        _set_pending_payment_description(
            payment.pk, 'PayPal capture details could not be verified.'
        )
        return 'pending'
    capture = captures[0]
    capture_amount = capture.get('amount') if isinstance(capture, dict) else None
    try:
        captured_value = Decimal(str(capture_amount['value']))
    except (KeyError, TypeError, InvalidOperation, ValueError):
        _set_pending_payment_description(
            payment.pk, 'PayPal capture amount could not be verified.'
        )
        return 'pending'

    if (
        order.get('id') != payment.paypal_order_id
        or purchase_unit.get('custom_id') != payment.ticket.code
        or capture.get('status') != 'COMPLETED'
        or not capture.get('id')
        or not isinstance(capture_amount, dict)
        or capture_amount.get('currency_code') != payment.currency
        or captured_value != payment.amount
    ):
        _set_pending_payment_description(
            payment.pk, 'PayPal capture details do not match this ticket.'
        )
        return 'pending'

    with transaction.atomic():
        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked_payment.status == 'success':
            return 'paid'
        if locked_payment.status != 'pending':
            return locked_payment.status
        locked_payment.status = 'success'
        locked_payment.paypal_capture_id = capture['id'][:100]
        locked_payment.result_description = 'PayPal capture confirmed.'
        locked_payment.save(
            update_fields=[
                'status',
                'paypal_capture_id',
                'result_description',
            ]
        )
        Ticket.objects.filter(pk=locked_payment.ticket_id).update(status='paid')
    return 'paid'


def _set_pending_payment_description(payment_id, description):
    Payment.objects.filter(pk=payment_id, status='pending').update(
        result_description=description[:255],
    )


def _fail_ticket(ticket):
    if ticket.status != 'paid':
        ticket.status = 'failed'
        ticket.save(update_fields=['status'])


def _fail_payment(payment_id, description):
    with transaction.atomic():
        payment = Payment.objects.select_for_update().select_related('ticket').get(
            pk=payment_id
        )
        if payment.status == 'success':
            return
        payment.status = 'failed'
        payment.result_description = description[:255]
        payment.save(update_fields=['status', 'result_description'])
        Ticket.objects.filter(pk=payment.ticket_id).exclude(status='paid').update(
            status='failed'
        )

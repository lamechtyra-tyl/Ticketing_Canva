import uuid
from decimal import Decimal

from django.db import models


TICKET_PRICES = {
    'regular': Decimal('2000.00'),
    'vip': Decimal('4500.00'),
    'vvip': Decimal('6000.00'),
}


class Event(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    venue = models.CharField(max_length=200)
    date = models.DateTimeField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    capacity = models.PositiveIntegerField(default=100)

    def __str__(self):
        return self.title

    @property
    def tickets_available(self):
        return self.capacity - self.tickets.exclude(status='failed').count()


class Ticket(models.Model):
    TICKET_TYPE_CHOICES = [
        ('regular', 'Regular Ticket'),
        ('vip', 'VIP Ticket'),
        ('vvip', 'VVIP Ticket'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
    ]

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='tickets')
    buyer_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=15, blank=True)
    ticket_type = models.CharField(
        max_length=10, choices=TICKET_TYPE_CHOICES, default='regular'
    )
    price = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal('2000.00')
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    code = models.CharField(max_length=12, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = uuid.uuid4().hex[:10].upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} - {self.buyer_name}"


class Payment(models.Model):
    STATUS_CHOICES = [('pending', 'Pending'), ('success', 'Success'), ('failed', 'Failed')]
    METHOD_CHOICES = [('mpesa', 'M-Pesa'), ('paypal', 'PayPal')]

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name='payments')
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default='mpesa')
    phone = models.CharField(max_length=15, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='KES')
    checkout_request_id = models.CharField(
        max_length=100, blank=True, null=True, unique=True
    )
    paypal_order_id = models.CharField(
        max_length=100, blank=True, null=True, unique=True
    )
    paypal_approval_url = models.URLField(max_length=500, blank=True)
    paypal_capture_id = models.CharField(max_length=100, blank=True)
    merchant_request_id = models.CharField(max_length=100, blank=True)
    mpesa_receipt = models.CharField(max_length=50, blank=True)
    result_code = models.IntegerField(blank=True, null=True)
    result_description = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.ticket.code} - {self.status}"
from rest_framework import serializers
from .models import Event, TICKET_PRICES, Ticket
from .mpesa import normalize_ke_phone


class EventSerializer(serializers.ModelSerializer):
    tickets_available = serializers.ReadOnlyField()

    class Meta:
        model = Event
        fields = ['id', 'title', 'description', 'venue', 'date',
                  'price', 'capacity', 'tickets_available']


class TicketSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    ticket_type = serializers.ChoiceField(
        choices=Ticket.TICKET_TYPE_CHOICES, default='regular'
    )
    price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Ticket
        fields = [
            'id',
            'event',
            'buyer_name',
            'phone',
            'ticket_type',
            'price',
            'status',
            'code',
            'created_at',
        ]
        read_only_fields = ['status', 'code', 'created_at']

    def validate(self, data):
        if data['event'].tickets_available <= 0:
            raise serializers.ValidationError("This event is sold out.")
        return data

    def validate_phone(self, value):
        if not value:
            return value
        try:
            return normalize_ke_phone(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def create(self, validated_data):
        validated_data['price'] = TICKET_PRICES[validated_data['ticket_type']]
        return super().create(validated_data)
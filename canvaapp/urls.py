from django.urls import path, include
from rest_framework.routers import DefaultRouter 
from .views import (
    MpesaCallback,
    PayPalCancel,
    PayPalCapture,
    PayPalOrder,
    PayPalReturn,
    StkPush,
    TicketPaymentStatus,
)
from . import views

router = DefaultRouter()
router.register('events', views.EventViewSet)
router.register('tickets', views.TicketViewSet)
urlpatterns = [
    path('', views.home, name='home'),
    path(
        'api/tickets/<str:code>/payment-status/',
        TicketPaymentStatus.as_view(),
        name='ticket-payment-status',
    ),
    path('api/', include(router.urls)),
    path('api/mpesa/stkpush/', StkPush.as_view()),
    path('api/mpesa/callback/', MpesaCallback.as_view(), name='mpesa-callback'),
    path('api/paypal/create-order/', PayPalOrder.as_view(), name='paypal-create-order'),
    path('api/paypal/capture/', PayPalCapture.as_view(), name='paypal-capture'),
    path('paypal/return/', PayPalReturn.as_view(), name='paypal-return'),
    path('paypal/cancel/', PayPalCancel.as_view(), name='paypal-cancel'),
]
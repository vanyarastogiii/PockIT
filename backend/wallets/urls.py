from django.urls import path

from .views import (
    CancelRequestView,
    DeclineRequestView,
    PaymentRequestListCreateView,
    PayRequestView,
    TopUpView,
    TransactionListView,
    TransferView,
    WalletView,
)

urlpatterns = [
    path("wallet", WalletView.as_view(), name="wallet"),
    path("wallet/topup", TopUpView.as_view(), name="topup"),
    path("transfers", TransferView.as_view(), name="transfers"),
    path("transactions", TransactionListView.as_view(), name="transactions"),
    path("requests", PaymentRequestListCreateView.as_view(), name="requests"),
    path("requests/<int:pk>/pay", PayRequestView.as_view(), name="request-pay"),
    path("requests/<int:pk>/decline", DeclineRequestView.as_view(), name="request-decline"),
    path("requests/<int:pk>/cancel", CancelRequestView.as_view(), name="request-cancel"),
]

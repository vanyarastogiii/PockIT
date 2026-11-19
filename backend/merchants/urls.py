from django.urls import path

from . import views

urlpatterns = [
    # Merchant API, called by the shop's server with its secret key
    path("v1/orders", views.ApiOrderCreateView.as_view(), name="api-order-create"),
    path("v1/orders/<uuid:order_id>", views.ApiOrderDetailView.as_view(), name="api-order-detail"),
    path("v1/orders/<uuid:order_id>/refunds", views.ApiRefundView.as_view(), name="api-order-refund"),
    # Business page (logged-in merchant owner)
    path("merchant", views.MyMerchantView.as_view(), name="my-merchant"),
    path("merchant/rotate-key", views.RotateKeyView.as_view(), name="my-merchant-rotate-key"),
    path("merchant/orders", views.MyOrdersView.as_view(), name="my-merchant-orders"),
    path("merchant/orders/<uuid:order_id>/refunds", views.MyRefundView.as_view(), name="my-merchant-refund"),
    path("merchant/webhooks", views.MyWebhookEventsView.as_view(), name="my-merchant-webhooks"),
    path("merchant/webhooks/<uuid:event_id>/retry", views.RetryWebhookView.as_view(), name="my-merchant-webhook-retry"),
    # Checkout (the customer)
    path("checkout/<uuid:order_id>", views.CheckoutOrderView.as_view(), name="checkout-order"),
    path("checkout/<uuid:order_id>/pay", views.CheckoutPayView.as_view(), name="checkout-pay"),
]

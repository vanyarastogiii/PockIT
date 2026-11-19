from django.urls import path

from . import views

urlpatterns = [
    path("api/buy", views.BuyView.as_view(), name="shop-buy"),
    path("api/orders/<int:order_id>", views.ShopOrderView.as_view(), name="shop-order"),
    path("webhooks/pockit", views.pockit_webhook, name="shop-webhook"),
]

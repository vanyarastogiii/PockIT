from django.apps import AppConfig


class MerchantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "merchants"

    def ready(self):
        from . import auth  # noqa: F401  (registers the API-key scheme with the API docs)

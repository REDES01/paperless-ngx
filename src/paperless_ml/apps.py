from django.apps import AppConfig


class PaperlessMlConfig(AppConfig):
    name = "paperless_ml"
    verbose_name = "Paperless ML integration"

    def ready(self) -> None:
        # Importing the module registers the @receiver-decorated handlers.
        from paperless_ml import signals  # noqa: F401

from django.urls import path

from paperless_ml.views import htr_corrections, htr_queue, search_feedback

urlpatterns = [
    path("htr/queue/", htr_queue, name="ml_htr_queue"),
    path("htr/corrections/", htr_corrections, name="ml_htr_corrections"),
    path("search/feedback/", search_feedback, name="ml_search_feedback"),
]

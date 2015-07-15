# http://docs.celeryproject.org/en/latest/django/first-steps-with-django.html
from __future__ import absolute_import

from celery import Celery
from django.conf import settings

app = Celery('celery_app')
app.config_from_object('django.conf:settings')
app.autodiscover_tasks(lambda: settings.INSTALLED_APPS)

from src.workers.celery_app import celery_app


def test_celery_app_configured():
    assert celery_app.main == "mind-center"

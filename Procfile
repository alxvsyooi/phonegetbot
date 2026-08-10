worker: python main.py
celeryworker: celery -A celery_app worker --loglevel=info --concurrency=2
celerybeat: celery -A celery_app beat --loglevel=info

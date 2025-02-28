# Gunicorn configuration
bind = "0.0.0.0:5000"
workers = 2
threads = 4
timeout = 60  # Increase timeout to 60 seconds
worker_class = "gthread" 
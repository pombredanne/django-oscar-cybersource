import django.dispatch

pre_calculate_auth_total = django.dispatch.Signal(providing_args=["basket"])
pre_build_auth_request = django.dispatch.Signal(providing_args=["extra_fields", "request"])
order_placed = django.dispatch.Signal(providing_args=["order"])

from django.urls import path
from planner import views

urlpatterns = [
    path('', views.index, name='index'),
    path('api/state/', views.state),
    path('api/conversations/', views.new_conversation),
    path('api/chat/', views.chat),
    path('api/runs/<uuid:run_id>/', views.run_status),
    path('api/debug/', views.debug_mode),
    path('api/runs/<uuid:run_id>/debug/', views.debug_logs),
    path('api/xhs/login/', views.xhs_login),
]

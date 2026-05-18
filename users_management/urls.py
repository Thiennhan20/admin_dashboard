from django.urls import path
from . import views

app_name = 'admin_dashboard'

urlpatterns = [
    # Authentication
    path('login/', views.admin_login, name='login'),
    path('logout/', views.admin_logout, name='logout'),
    path('register/', views.admin_register, name='register'),
    
    # Dashboard chính
    path('', views.operations_dashboard, name='dashboard'),
    
    # Quản lý users
    path('users/', views.users_list, name='users_list'),
    path('users/<str:user_id>/', views.user_detail, name='user_detail'),
    path('users/<str:user_id>/edit/', views.user_edit, name='user_edit'),
    path('users/<str:user_id>/delete/', views.user_delete, name='user_delete'),
    
    # Quản lý watchlist
    path('watchlist/', views.watchlist_list, name='watchlist_list'),
    path('watchlist/<str:item_id>/', views.watchlist_detail, name='watchlist_detail'),
    path('watchlist/<str:item_id>/edit/', views.watchlist_edit, name='watchlist_edit'),
    path('watchlist/<str:item_id>/delete/', views.watchlist_delete, name='watchlist_delete'),
    path('watchlist/cleanup/', views.cleanup_watchlist, name='cleanup_watchlist'),
    
    # Quản lý admin
    path('admins/', views.admin_list, name='admin_list'),
    path('admins/create/', views.admin_create, name='admin_create'),
    path('admins/<int:admin_id>/', views.admin_detail, name='admin_detail'),
    path('admins/<int:admin_id>/edit/', views.admin_edit, name='admin_edit'),
    path('admins/<int:admin_id>/delete/', views.admin_delete, name='admin_delete'),
    
    # Media Management
    path('movies/', views.placeholder_movies, name='movies'),
    path('tv-shows/', views.placeholder_tv, name='tv_shows'),
    path('comments/', views.placeholder_comments, name='comments'),
    path('rooms/', views.placeholder_rooms, name='rooms'),
    path('notifications/', views.placeholder_notifications, name='notifications'),
    path('cache/', views.cache_management, name='cache'),
    path('api/cache/', views.api_cache_action, name='api_cache_action'),
    path('api/cache/list/', views.api_cache_list, name='api_cache_list'),
    
    # API endpoints cho AJAX
    path('api/refresh/', views.api_sync_from_api, name='api_refresh'),
    path('api/overview/', views.api_dashboard_overview, name='api_overview'),
    path('api/mongo/<str:collection>/', views.api_mongo_collection, name='api_mongo_collection'),
    path(
        'api/mongo/<str:collection>/<str:document_id>/<str:action>/',
        views.api_mongo_action,
        name='api_mongo_action'
    ),

]

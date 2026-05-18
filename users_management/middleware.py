from django.shortcuts import redirect
from django.urls import reverse
from django.contrib import messages

class AdminAccessMiddleware:
    """
    Middleware để kiểm tra quyền truy cập admin dashboard
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
        
    def __call__(self, request):
        # Danh sách các URL được phép truy cập mà không cần đăng nhập
        public_urls = [
            '/dashboard/login/',
            '/dashboard/register/',
            '/admin/',
            '/admin/login/',
            '/admin/logout/',
        ]
        
        # Kiểm tra nếu đang truy cập dashboard
        if request.path.startswith('/dashboard/'):
            # Nếu là URL công khai, cho phép truy cập
            if any(request.path.startswith(url) for url in public_urls):
                response = self.get_response(request)
                return response
            
            # Kiểm tra đăng nhập
            if not request.user.is_authenticated:
                messages.error(request, 'Bạn cần đăng nhập để truy cập admin dashboard!')
                return redirect('admin_dashboard:login')
            
            # Kiểm tra quyền admin
            if not (request.user.is_staff or request.user.is_superuser):
                messages.error(request, 'Bạn không có quyền truy cập admin dashboard!')
                return redirect('admin_dashboard:login')
        
        response = self.get_response(request)
        return response
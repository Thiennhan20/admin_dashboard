from django.db import models
from django.contrib.auth.models import User

# Chỉ giữ lại models cần thiết cho admin authentication
# Không lưu trữ dữ liệu API để tiết kiệm tài nguyên

class AdminProfile(models.Model):
    """
    Model để lưu thông tin bổ sung cho admin user (nếu cần)
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='admin_profile')
    role = models.CharField(max_length=50, default='admin')
    last_api_sync = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Admin Profile"
        verbose_name_plural = "Admin Profiles"
    
    def __str__(self):
        return f"Admin: {self.user.username} ({self.role})"

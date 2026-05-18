# Hướng dẫn thiết lập Django Admin

## Vấn đề đã được sửa

Trước đây, hệ thống tạo admin không đúng chuẩn Django, gây ra các vấn đề về quyền hạn. Đã được sửa lại để sử dụng Django admin mặc định.

## Cách tạo Superuser đầu tiên

### Phương pháp 1: Sử dụng Management Command (Khuyến nghị)

```bash
cd admin_pannel
python manage.py create_superuser --username admin --email admin@example.com --password your_password --first-name Admin --last-name User
```

### Phương pháp 2: Sử dụng Django Shell

```bash
cd admin_pannel
python manage.py shell
```

```python
from django.contrib.auth.models import User
from users_management.models import AdminProfile

# Tạo superuser
user = User.objects.create_superuser(
    username='admin',
    email='admin@example.com',
    password='your_password',
    first_name='Admin',
    last_name='User'
)

# Tạo admin profile
AdminProfile.objects.create(user=user, role='super_admin')
```

### Phương pháp 3: Sử dụng Django Admin Interface

1. Tạo superuser bằng lệnh Django mặc định:
```bash
python manage.py createsuperuser
```

2. Sau đó tạo AdminProfile:
```python
python manage.py shell
```

```python
from django.contrib.auth.models import User
from users_management.models import AdminProfile

user = User.objects.get(username='admin')  # Thay 'admin' bằng username bạn vừa tạo
AdminProfile.objects.create(user=user, role='super_admin')
```

## Các loại quyền hạn

### 1. Superuser (`is_superuser=True`)
- Có toàn quyền trong Django Admin (`/admin/`)
- Có thể quản lý tất cả models, users, permissions
- Có thể tạo admin mới
- Có thể truy cập dashboard (`/dashboard/`)

### 2. Staff User (`is_staff=True, is_superuser=False`)
- Có thể truy cập Django Admin (`/admin/`) nhưng với quyền hạn hạn chế
- Có thể truy cập dashboard (`/dashboard/`)
- Không thể quản lý admin khác

### 3. Regular User (`is_staff=False, is_superuser=False`)
- Không thể truy cập Django Admin
- Không thể truy cập dashboard

## Cách sử dụng

### 1. Đăng nhập Django Admin
- URL: `http://localhost:8000/admin/`
- Sử dụng tài khoản superuser hoặc staff user

### 2. Đăng nhập Dashboard
- URL: `http://localhost:8000/dashboard/`
- Sử dụng tài khoản có `is_staff=True`

### 3. Tạo admin mới
- Chỉ superuser mới có thể tạo admin mới
- Truy cập: `/dashboard/admins/create/`
- Có thể chọn tạo staff user hoặc superuser

## Lưu ý quan trọng

1. **Luôn tạo superuser đầu tiên** trước khi sử dụng hệ thống
2. **Không xóa superuser cuối cùng** - hệ thống sẽ ngăn chặn điều này
3. **Sử dụng Django Admin** (`/admin/`) để quản lý users và permissions
4. **Sử dụng Dashboard** (`/dashboard/`) để quản lý dữ liệu ứng dụng

## Troubleshooting

### Lỗi: "Chỉ có Superuser mới có thể tạo admin mới"
- Giải pháp: Đăng nhập bằng tài khoản superuser hoặc tạo superuser mới

### Lỗi: "Không thể truy cập Django Admin"
- Kiểm tra user có `is_staff=True` không
- Kiểm tra user có `is_active=True` không

### Lỗi: "Permission denied"
- Kiểm tra user có đúng quyền hạn không
- Sử dụng superuser để cấp quyền

## Migration

Nếu bạn đã có dữ liệu cũ, chạy migration:

```bash
python manage.py makemigrations
python manage.py migrate
```

Sau đó cập nhật các user hiện có:

```python
python manage.py shell
```

```python
from django.contrib.auth.models import User
from users_management.models import AdminProfile

# Cập nhật tất cả admin hiện có
for user in User.objects.filter(is_staff=True):
    if not hasattr(user, 'admin_profile'):
        AdminProfile.objects.create(user=user, role='admin')
```

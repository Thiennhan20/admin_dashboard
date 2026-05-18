# 📁 Admin Dashboard Template Structure

## 🎯 Tổng quan
Cấu trúc template đã được tổ chức lại để dễ quản lý và bảo trì hơn.

## 📂 Cấu trúc thư mục

```
templates/admin_dashboard/
├── base.html                    # Base template cho toàn bộ admin panel
├── auth/                        # Authentication pages
│   ├── login.html              # Trang đăng nhập
│   └── register.html           # Trang đăng ký
├── dashboard/                   # Dashboard & Overview
│   └── dashboard.html          # Trang dashboard chính
├── users/                       # User Management
│   ├── users_list.html         # Danh sách users
│   ├── user_detail.html        # Chi tiết user
│   └── user_edit.html          # Chỉnh sửa user
├── watchlist/                   # Watchlist Management
│   ├── watchlist_list.html     # Danh sách watchlist
│   ├── watchlist_detail.html   # Chi tiết watchlist item
│   └── watchlist_edit.html     # Chỉnh sửa watchlist item
└── admin/                       # Admin Management
    ├── admin_list.html         # Danh sách admin accounts
    ├── admin_create.html       # Tạo admin mới
    ├── admin_detail.html       # Chi tiết admin account
    └── admin_edit.html         # Chỉnh sửa admin account
```

## 🔧 Tính năng từng module

### 🔐 **Authentication (`auth/`)**
- **Login/Register**: Giao diện đăng nhập/đăng ký admin
- **Responsive**: Tối ưu cho mobile và desktop
- **Validation**: Client-side và server-side validation

### 📊 **Dashboard (`dashboard/`)**
- **Overview**: Thống kê tổng quan hệ thống
- **API Status**: Kiểm tra trạng thái API
- **Quick Actions**: Các hành động nhanh

### 👥 **User Management (`users/`)**
- **List View**: Hiển thị danh sách users từ API
- **Detail View**: Chi tiết thông tin user
- **Edit Form**: Chỉnh sửa thông tin user
- **Search/Filter**: Tìm kiếm và lọc users

### 📺 **Watchlist Management (`watchlist/`)**
- **List View**: Danh sách watchlist items
- **Detail View**: Chi tiết movie trong watchlist
- **Edit Form**: Chỉnh sửa watchlist item
- **Bulk Actions**: Thao tác hàng loạt

### 👑 **Admin Management (`admin/`)**
- **List View**: Danh sách admin accounts
- **Create Form**: Tạo admin mới với validation
- **Detail View**: Chi tiết admin account
- **Edit Form**: Chỉnh sửa admin account
- **Role Management**: Quản lý vai trò (admin/super_admin/moderator)

## 🎨 Design System

### **Base Template (`base.html`)**
- **Responsive Layout**: Bootstrap 5.3.2
- **Modern UI**: Font Awesome 6.5.1 + Google Fonts (Inter)
- **Color Scheme**: CSS Variables cho dễ tùy chỉnh
- **Sidebar Navigation**: Collapsible sidebar với active states
- **Header**: Compact header với user info

### **Common Features**
- **Loading States**: Spinner và skeleton loading
- **Alert Messages**: Success/Error/Warning notifications
- **Form Validation**: Real-time validation feedback
- **Mobile First**: Responsive design cho mọi thiết bị

## 🚀 Benefits của cấu trúc mới

### ✅ **Dễ quản lý**
- Mỗi module có thư mục riêng
- Tên file rõ ràng và có ý nghĩa
- Dễ tìm và sửa file

### ✅ **Dễ mở rộng**
- Thêm module mới chỉ cần tạo thư mục
- Không ảnh hưởng đến module khác
- Template inheritance rõ ràng

### ✅ **Dễ bảo trì**
- Code được tổ chức theo chức năng
- Giảm conflict khi làm việc nhóm
- Dễ debug và fix lỗi

### ✅ **Performance**
- Load template nhanh hơn
- Cache hiệu quả hơn
- Giảm memory usage

## 📝 Notes

- **Base Template**: Tất cả templates đều extend từ `base.html`
- **URL Patterns**: Đã được cập nhật trong `views.py`
- **Static Files**: CSS/JS được tổ chức trong `static/` folder
- **API Integration**: Real-time data từ external Node.js API

"""Data export views – CSV, Excel, and PDF downloads for various collections."""
import csv
import io
from datetime import datetime

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse, JsonResponse

from users_management.models import AuditLog
from users_management.services import mongo_admin, clean_value
from users_management.views_legacy import check_admin_access

__all__ = ['export_collection']


EXPORT_CONFIGS = {
    'users': {
        'collection': 'users',
        'fields': ['_id', 'name', 'email', 'isEmailVerified', 'createdAt'],
        'headers': ['ID', 'Name', 'Email', 'Email Verified', 'Created At'],
        'projection': {'password': 0, 'emailVerificationToken': 0},
    },
    'comments': {
        'collection': 'comments',
        'fields': ['_id', 'movieId', 'type', 'content', 'isDeleted', 'createdAt'],
        'headers': ['ID', 'Movie ID', 'Type', 'Content', 'Deleted', 'Created At'],
    },
    'watchprogresses': {
        'collection': 'watchprogresses',
        'fields': ['_id', 'userId', 'contentId', 'title', 'isTVShow', 'progress', 'lastWatched'],
        'headers': ['ID', 'User ID', 'Content ID', 'Title', 'Is TV Show', 'Progress', 'Last Watched'],
    },
    'searchhistories': {
        'collection': 'searchhistories',
        'fields': ['_id', 'userId', 'queries', 'updated_at'],
        'headers': ['ID', 'User ID', 'Queries', 'Updated At'],
    },
    'notifications': {
        'collection': 'notifications',
        'fields': ['_id', 'recipient', 'type', 'read', 'createdAt'],
        'headers': ['ID', 'Recipient', 'Type', 'Read', 'Created At'],
    },
    'audit_logs': {
        'source': 'postgres',
        'fields': ['id', 'admin_username', 'action', 'target_type', 'target_id', 'ip_address', 'timestamp'],
        'headers': ['ID', 'Admin', 'Action', 'Target Type', 'Target ID', 'IP', 'Timestamp'],
    },
}


def generate_excel_html(headers, rows, sheet_name="Data"):
    html = f"""<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel" xmlns="http://www.w3.org/TR/REC-html40">
<head>
    <meta http-equiv="content-type" content="text/html; charset=utf-8">
    <!--[if gte mso 9]>
    <xml>
        <x:ExcelWorkbook>
            <x:ExcelWorksheets>
                <x:ExcelWorksheet>
                    <x:Name>{sheet_name}</x:Name>
                    <x:WorksheetOptions>
                        <x:DisplayGridlines/>
                    </x:WorksheetOptions>
                </x:ExcelWorksheet>
            </x:ExcelWorksheets>
        </x:ExcelWorkbook>
    </xml>
    <![endif]-->
    <style>
        th {{ background-color: #3b82f6; color: #ffffff; font-weight: bold; border: 1px solid #e5e7eb; padding: 8px; text-align: left; }}
        td {{ border: 1px solid #e5e7eb; padding: 8px; mso-number-format:"\\@"; }}
    </style>
</head>
<body>
    <table>
        <thead>
            <tr>
                {"".join(f"<th>{h}</th>" for h in headers)}
            </tr>
        </thead>
        <tbody>"""
    for row in rows:
        html += "<tr>"
        for val in row:
            html += f"<td>{val}</td>"
        html += "</tr>"
    html += """</tbody>
    </table>
</body>
</html>"""
    return html


def generate_print_html(title, headers, rows):
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>{title}</title>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; padding: 30px; color: #1f2937; background: #ffffff; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #3b82f6; padding-bottom: 12px; margin-bottom: 20px; }}
        .title {{ font-size: 24px; font-weight: bold; }}
        .meta {{ font-size: 13px; color: #6b7280; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }}
        th, td {{ border: 1px solid #d1d5db; padding: 8px 10px; text-align: left; }}
        th {{ background-color: #f3f4f6; font-weight: bold; color: #374151; }}
        tr:nth-child(even) {{ background-color: #f9fafb; }}
        .print-btn {{ padding: 8px 16px; background-color: #3b82f6; color: white; border: none; border-radius: 4px; font-weight: bold; cursor: pointer; }}
        @media print {{
            body {{ padding: 0; }}
            .print-btn {{ display: none; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <div class="title">{title}</div>
            <div class="meta">Exported on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        </div>
        <button class="print-btn" onclick="window.print()">Print PDF</button>
    </div>
    <table>
        <thead>
            <tr>
                {"".join(f"<th>{h}</th>" for h in headers)}
            </tr>
        </thead>
        <tbody>"""
    for row in rows:
        html += "<tr>"
        for val in row:
            html += f"<td>{val}</td>"
        html += "</tr>"
    html += """</tbody>
    </table>
    <script>
        window.onload = function() {
            setTimeout(function() { window.print(); }, 500);
        };
    </script>
</body>
</html>"""
    return html


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def export_collection(request, collection_name):
    config = EXPORT_CONFIGS.get(collection_name)
    if not config:
        return JsonResponse({'error': 'Invalid collection'}, status=400)

    export_format = request.GET.get('format', 'csv')
    rows = []

    try:
        if config.get('source') == 'postgres':
            # Retrieve from SQL DB
            qs = AuditLog.objects.all().order_by('-timestamp')[:5000]
            for row in qs:
                rows.append([
                    row.id, row.admin_username, row.action,
                    row.target_type, row.target_id, row.ip_address,
                    row.timestamp.isoformat() if row.timestamp else '',
                ])
        else:
            # Retrieve from MongoDB
            db = mongo_admin.db()
            projection = config.get('projection', {})
            docs = list(db[config['collection']].find({}, projection).sort('_id', -1).limit(5000))
            docs = clean_value(docs)
            for doc in docs:
                row_data = []
                for field in config['fields']:
                    val = doc.get(field, '')
                    if isinstance(val, list):
                        val = ', '.join(str(v) for v in val)
                    row_data.append(val)
                rows.append(row_data)

        AuditLog.log(request, f'export_{collection_name}', collection_name, details={'format': export_format})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    # Return CSV format
    if export_format == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(config['headers'])
        writer.writerows(rows)
        
        response = HttpResponse(output.getvalue(), content_type='text/csv; charset=utf-8')
        filename = f'{collection_name}_{datetime.now():%Y%m%d_%H%M}.csv'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # Return Excel-compatible XML format
    elif export_format == 'excel':
        excel_content = generate_excel_html(config['headers'], rows, sheet_name=collection_name.capitalize())
        response = HttpResponse(excel_content, content_type='application/vnd.ms-excel; charset=utf-8')
        filename = f'{collection_name}_{datetime.now():%Y%m%d_%H%M}.xls'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # Return print-ready PDF layout
    elif export_format == 'pdf':
        print_content = generate_print_html(f"{collection_name.replace('_', ' ').capitalize()} Report", config['headers'], rows)
        return HttpResponse(print_content, content_type='text/html; charset=utf-8')

    return JsonResponse({'error': 'Unsupported format'}, status=400)

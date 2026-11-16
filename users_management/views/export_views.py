"""Data export views – CSV, Excel, and PDF downloads for various collections."""
import csv
import io
from datetime import datetime

from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse

from users_management.models import AuditLog
from users_management.services import clean_value, get_admin_profile, mongo_admin
from users_management.views_legacy import check_admin_access

__all__ = ['export_collection']


class _EchoBuffer:
    """File-like helper implementing write() for Django StreamingHttpResponse CSV streaming."""

    def write(self, value):
        return value


def _format_mongo_row(doc, fields):
    cleaned = clean_value(doc)
    row_data = []
    for field in fields:
        val = cleaned.get(field, '')
        if field == 'user_id' and not val:
            val = cleaned.get('userId', '')
        elif field == 'history' and isinstance(val, list):
            val = ', '.join(
                (item.get('query', '') if isinstance(item, dict) else str(item))
                for item in val
            )
        elif isinstance(val, list):
            val = ', '.join(str(v) for v in val)
        row_data.append(val)
    return row_data


EXPORT_CONFIGS = {
    'users': {
        'collection': 'users',
        'fields': ['_id', 'name', 'username', 'email', 'authType', 'isEmailVerified', 'isBanned', 'createdAt'],
        'headers': ['ID', 'Name', 'Username', 'Email', 'Auth Type', 'Email Verified', 'Banned', 'Created At'],
        'projection': {
            'password': 0,
            'emailVerificationToken': 0,
            'watchlist': 0,
            'friends': 0,
            'friendRequests': 0,
            'sentFriendRequests': 0,
            'pushTokens': 0,
        },
    },
    'sessions': {
        'collection': 'sessions',
        'fields': ['_id', 'userId', 'device', 'ip', 'location', 'lastActive', 'expiresAt'],
        'headers': ['ID', 'User ID', 'Device', 'IP Address', 'Location', 'Last Active', 'Expires At'],
        'projection': {'refreshToken': 0},
    },
    'comments': {
        'collection': 'comments',
        'fields': ['_id', 'movieId', 'type', 'content', 'likes', 'isDeleted', 'createdAt'],
        'headers': ['ID', 'Movie ID', 'Type', 'Content', 'Likes', 'Deleted', 'Created At'],
    },
    'watchlist': {
        'source': 'watchlist',
        'collection': 'users',
        'headers': ['User ID', 'User Name', 'User Email', 'Media ID', 'Title', 'Type', 'Poster Path'],
    },
    'userreports': {
        'collection': 'userreports',
        'fields': ['_id', 'reporterName', 'targetType', 'targetId', 'reportedUserName', 'reasonCategory', 'reason', 'status', 'actionTaken', 'resolvedBy', 'createdAt'],
        'headers': ['ID', 'Reporter', 'Target Type', 'Target ID', 'Author', 'Category', 'Reason', 'Status', 'Action Taken', 'Resolved By', 'Created At'],
    },
    'watchprogresses': {
        'collection': 'watchprogresses',
        'fields': ['_id', 'userId', 'contentId', 'title', 'isTVShow', 'currentTime', 'duration', 'server', 'audio', 'lastWatched'],
        'headers': ['ID', 'User ID', 'Content ID', 'Title', 'Is TV Show', 'Current Time (s)', 'Duration (s)', 'Server', 'Audio', 'Last Watched'],
    },
    'searchhistories': {
        'collection': 'searchhistories',
        'fields': ['_id', 'user_id', 'history', 'updated_at'],
        'headers': ['ID', 'User ID', 'Queries', 'Updated At'],
    },
    'notifications': {
        'collection': 'notifications',
        'fields': ['_id', 'recipient', 'type', 'read', 'createdAt'],
        'headers': ['ID', 'Recipient', 'Type', 'Read', 'Created At'],
    },
    'streamhistories': {
        'collection': 'streamhistories',
        'fields': ['_id', 'userId', 'title', 'movieId', 'contentType', 'audio', 'season', 'episode', 'roomId', 'createdCount', 'streamUrl', 'lastCreatedAt'],
        'headers': ['ID', 'User ID', 'Title', 'TMDB ID', 'Content Type', 'Audio', 'Season', 'Episode', 'Room ID', 'Created Count', 'Stream URL', 'Last Streamed At'],
    },
    'dailychallenges': {
        'collection': 'dailychallenges',
        'fields': ['_id', 'date', 'secret', 'totalParticipants', 'totalSolved', 'createdAt'],
        'headers': ['ID', 'Date (UTC+7)', 'Secret', 'Participants', 'Solved', 'Created At'],
    },
    'dailysubmissions': {
        'collection': 'dailysubmissions',
        'fields': ['_id', 'date', 'username', 'userId', 'guessCount', 'isSolved', 'isFinished', 'durationSeconds', 'xpAwarded', 'completedAt'],
        'headers': ['ID', 'Date', 'Username', 'User ID', 'Guesses', 'Solved', 'Finished', 'Duration (s)', 'XP Awarded', 'Completed At'],
    },
    'gameprofiles': {
        'collection': 'gameprofiles',
        'fields': ['_id', 'userId', 'playerName', 'rating', 'highestRating', 'wins', 'losses', 'currentWinStreak', 'bestWinStreak', 'totalXp', 'equippedTitleId'],
        'headers': ['ID', 'User ID', 'Player Name', 'Rating', 'Peak Rating', 'Wins', 'Losses', 'Current Streak', 'Best Streak', 'Total XP', 'Equipped Title'],
    },
    'gamehistories': {
        'collection': 'gamehistories',
        'fields': ['_id', 'roomId', 'winnerName', 'isAiRoom', 'aiDifficulty', 'status', 'endReason', 'totalGuesses', 'duration', 'createdAt'],
        'headers': ['ID', 'Room ID', 'Winner Name', 'AI Room', 'AI Difficulty', 'Status', 'End Reason', 'Total Guesses', 'Duration (s)', 'Created At'],
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


_EXPORT_PERM_MAP = {
    'users': 'users.export',
    'sessions': 'sessions.export',
    'comments': 'comments.export',
    'userreports': 'reports.export',
    'watchlist': 'watchlist.export',
    'watchprogresses': 'watch_progress.export',
    'searchhistories': 'search_history.export',
    'notifications': 'notifications.export',
    'streamhistories': 'stream_history.export',
    'dailychallenges': 'daily_challenge.export',
    'dailysubmissions': 'daily_challenge.export',
    'gameprofiles': 'games.export',
    'gamehistories': 'games.export',
    'audit_logs': 'audit_logs.export',
}


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
def export_collection(request, collection_name):
    config = EXPORT_CONFIGS.get(collection_name)
    if not config:
        return JsonResponse({'error': 'Invalid collection'}, status=400)

    req_perm = _EXPORT_PERM_MAP.get(collection_name, 'dashboard.export')
    if not request.user.is_superuser:
        profile = get_admin_profile(request.user, request)
        if not (profile and profile.has_perm(req_perm)):
            return JsonResponse({'error': f'Permission denied ({req_perm})'}, status=403)

    export_format = request.GET.get('format', 'csv')
    if export_format not in ('csv', 'excel', 'pdf'):
        return JsonResponse({'error': 'Unsupported format'}, status=400)

    def _iter_rows():
        if config.get('source') == 'postgres':
            qs = AuditLog.objects.all().order_by('-timestamp')[:5000]
            for row in qs.iterator(chunk_size=500):
                yield [
                    row.id, row.admin_username, row.action,
                    row.target_type, row.target_id, row.ip_address,
                    row.timestamp.isoformat() if row.timestamp else '',
                ]
        elif config.get('source') == 'watchlist':
            db = mongo_admin.db()
            pipeline = [
                {'$match': {'watchlist.0': {'$exists': True}}},
                {'$project': {'name': 1, 'email': 1, 'watchlist': 1}},
                {'$unwind': '$watchlist'},
                {'$sort': {'watchlist.title': 1}},
                {'$limit': 5000},
            ]
            for doc in db.users.aggregate(pipeline):
                wl = doc.get('watchlist') or {}
                yield [
                    str(doc.get('_id', '')),
                    doc.get('name', ''),
                    doc.get('email', ''),
                    str(wl.get('id', '')),
                    wl.get('title', ''),
                    wl.get('type', ''),
                    wl.get('poster_path', ''),
                ]
        else:
            db = mongo_admin.db()
            projection = config.get('projection', {})
            cursor = db[config['collection']].find({}, projection).sort('_id', -1).limit(5000)
            if hasattr(cursor, 'batch_size'):
                cursor = cursor.batch_size(500)
            fields = config['fields']
            for doc in cursor:
                yield _format_mongo_row(doc, fields)

    if export_format == 'csv':
        try:
            AuditLog.log(request, f'export_{collection_name}', collection_name, details={'format': export_format})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

        def _csv_stream():
            writer = csv.writer(_EchoBuffer())
            yield '\ufeff' + writer.writerow(config['headers'])
            for row in _iter_rows():
                yield writer.writerow(row)

        response = StreamingHttpResponse(_csv_stream(), content_type='text/csv; charset=utf-8')
        filename = f'{collection_name}_{datetime.now():%Y%m%d_%H%M}.csv'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    try:
        rows = list(_iter_rows())
        AuditLog.log(request, f'export_{collection_name}', collection_name, details={'format': export_format})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    # Return Excel-compatible XML format
    if export_format == 'excel':
        excel_content = generate_excel_html(config['headers'], rows, sheet_name=collection_name.capitalize())
        response = HttpResponse(excel_content, content_type='application/vnd.ms-excel; charset=utf-8')
        filename = f'{collection_name}_{datetime.now():%Y%m%d_%H%M}.xls'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # Return print-ready PDF layout
    print_content = generate_print_html(f"{collection_name.replace('_', ' ').capitalize()} Report", config['headers'], rows)
    return HttpResponse(print_content, content_type='text/html; charset=utf-8')

"""Backup & Safe Dry-Run Restore management views for MongoDB collections."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from bson import ObjectId, json_util
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from pymongo import DeleteMany, InsertOne, UpdateOne

from users_management.decorators import is_json_request, require_permission
from users_management.models import AuditLog, BackupLog
from users_management.services import MONGO_COLLECTIONS, mongo_admin, parallel_run
from users_management.views_legacy import check_admin_access

__all__ = [
    'create_mongo_backup_payload',
    'write_mongo_backup_snapshot',
    'parse_backup_snapshot',
    'backup_management',
    'backup_download',
    'backup_restore_preview',
    'backup_restore_confirm',
    'backup_restore_cancel',
]

_RESTORE_SESSION_KEY = '_backup_restore_pending'
_RESTORE_STAGING_DIR = Path(tempfile.gettempdir()) / 'cinearena_backup_restore_staging'
_MAX_RESTORE_BYTES = 50 * 1024 * 1024  # 50 MB


def _can_destructive_replace(user):
    if user.is_superuser:
        return True
    profile = getattr(user, 'admin_profile', None)
    return bool(profile and (profile.role == 'super_admin' or profile.has_perm('backup.delete')))


def _cleanup_staging_entry(entry):
    if not isinstance(entry, dict):
        return
    fpath = entry.get('file_path')
    if fpath:
        try:
            Path(fpath).unlink(missing_ok=True)
        except OSError:
            pass


def _resolve_backup_collections(selected=None):
    valid_selected = [
        c for c in (selected or list(MONGO_COLLECTIONS.keys()))
        if c in MONGO_COLLECTIONS
    ]
    if not valid_selected:
        valid_selected = list(MONGO_COLLECTIONS.keys())
    return valid_selected


def write_mongo_backup_snapshot(out_stream, selected=None, include_secrets=False):
    """Stream selected MongoDB collections as compact Extended JSON (v2 envelope) to a binary stream.

    Avoids holding the full database plus redundant JSON buffers in RAM.
    Returns ``(bytes_written, counts_dict, valid_collections_list)``.
    """
    valid_selected = _resolve_backup_collections(selected)
    db = mongo_admin.db()
    counts = {}
    bytes_written = 0

    def _write(text):
        nonlocal bytes_written
        chunk = text.encode('utf-8')
        out_stream.write(chunk)
        bytes_written += len(chunk)

    _write('{"collections":{')
    for idx, coll_name in enumerate(valid_selected):
        if idx > 0:
            _write(',')
        _write(json.dumps(coll_name) + ':[')
        proj = {} if include_secrets else MONGO_COLLECTIONS[coll_name].get('projection', {})
        cursor = db[coll_name].find({}, proj) if proj else db[coll_name].find({})
        if hasattr(cursor, 'batch_size'):
            cursor = cursor.batch_size(500)
        doc_count = 0
        for doc in cursor:
            if doc_count > 0:
                _write(',')
            _write(json_util.dumps(
                doc,
                ensure_ascii=False,
                separators=(',', ':'),
                json_options=json_util.RELAXED_JSON_OPTIONS,
            ))
            doc_count += 1
        _write(']')
        counts[coll_name] = doc_count

    meta = {
        'format': 'cinearena_backup_v2',
        'exported_at': datetime.utcnow().isoformat() + 'Z',
        'include_secrets': bool(include_secrets),
        'collections': valid_selected,
        'counts': counts,
    }
    _write('},"_meta":')
    _write(json_util.dumps(
        meta,
        ensure_ascii=False,
        separators=(',', ':'),
        json_options=json_util.RELAXED_JSON_OPTIONS,
    ))
    _write('}')
    return bytes_written, counts, valid_selected


def create_mongo_backup_payload(selected=None, include_secrets=False):
    """Export selected MongoDB collections into Extended JSON (v2 envelope preserving BSON types).

    Returns ``(json_str, counts_dict, valid_collections_list)``.
    """
    valid_selected = _resolve_backup_collections(selected)

    db = mongo_admin.db()
    collections_data = {}
    counts = {}

    for coll_name in valid_selected:
        proj = {} if include_secrets else MONGO_COLLECTIONS[coll_name].get('projection', {})
        docs = list(db[coll_name].find({}, proj)) if proj else list(db[coll_name].find({}))
        collections_data[coll_name] = docs
        counts[coll_name] = len(docs)

    envelope = {
        '_meta': {
            'format': 'cinearena_backup_v2',
            'exported_at': datetime.utcnow().isoformat() + 'Z',
            'include_secrets': bool(include_secrets),
            'collections': valid_selected,
            'counts': counts,
        },
        'collections': collections_data,
    }
    json_str = json_util.dumps(
        envelope,
        ensure_ascii=False,
        separators=(',', ':'),
        json_options=json_util.RELAXED_JSON_OPTIONS,
    )
    return json_str, counts, valid_selected


def _coerce_v1_doc(doc):
    """Convert stringified 24-hex ObjectId _id fields in legacy v1 backups."""
    if not isinstance(doc, dict):
        return doc
    copied = dict(doc)
    raw_id = copied.get('_id')
    if isinstance(raw_id, str) and ObjectId.is_valid(raw_id):
        copied['_id'] = ObjectId(raw_id)
    for fk in ('userId', 'recipient', 'actor', 'movieId'):
        val = copied.get(fk)
        if isinstance(val, str) and ObjectId.is_valid(val):
            copied[fk] = ObjectId(val)
    return copied


def parse_backup_snapshot(raw_bytes):
    """Parse uploaded v1/v2 backup bytes using bson.json_util and validate collections."""
    decoded = raw_bytes.decode('utf-8')
    parsed = json_util.loads(decoded)
    if not isinstance(parsed, dict):
        raise ValueError('Snapshot root JSON must be an object mapping collection names to document lists.')

    meta = {}
    if isinstance(parsed.get('collections'), dict) and isinstance(parsed.get('_meta'), dict):
        meta = parsed['_meta']
        raw_collections = parsed['collections']
        is_v2 = True
    else:
        raw_collections = {k: v for k, v in parsed.items() if k != '_meta'}
        is_v2 = False

    valid_data = {}
    ignored_collections = []
    for coll_name, docs in raw_collections.items():
        if coll_name not in MONGO_COLLECTIONS:
            ignored_collections.append(str(coll_name))
            continue
        if not isinstance(docs, list):
            raise ValueError(f'Collection "{coll_name}" must contain a JSON array of documents.')
        cleaned_docs = []
        for idx, doc in enumerate(docs):
            if not isinstance(doc, dict):
                raise ValueError(f'Document #{idx} in "{coll_name}" must be a JSON object.')
            cleaned_docs.append(doc if is_v2 else _coerce_v1_doc(doc))
        valid_data[coll_name] = cleaned_docs

    if not valid_data:
        raise ValueError('No valid/recognized MongoDB collections found in snapshot file.')

    return {
        'is_v2': is_v2,
        'meta': meta,
        'include_secrets': bool(meta.get('include_secrets', False)) if meta else False,
        'collections': valid_data,
        'ignored_collections': ignored_collections,
    }


def _compute_restore_preview(parsed_snapshot, mode):
    """Read-only dry-run impact analysis comparing incoming snapshot docs with live MongoDB."""
    db = mongo_admin.db()
    per_collection = []
    warnings = []

    if parsed_snapshot['ignored_collections']:
        warnings.append(
            'Ignored unrecognized collections: ' + ', '.join(parsed_snapshot['ignored_collections'])
        )

    if 'users' in parsed_snapshot['collections'] and not parsed_snapshot['include_secrets']:
        warnings.append(
            'Snapshot exported WITHOUT sensitive fields (password hashes / tokens). '
            'Upsert preserves existing passwords on matched users; new inserts will have no password hash.'
        )

    def _inspect_collection(coll_name, docs):
        incoming = len(docs)
        existing_total = int(db[coll_name].count_documents({}))
        doc_ids = [d['_id'] for d in docs if '_id' in d and d['_id'] is not None]

        matching_ids = 0
        for i in range(0, len(doc_ids), 500):
            chunk_ids = doc_ids[i:i + 500]
            if chunk_ids:
                matching_ids += int(db[coll_name].count_documents({'_id': {'$in': chunk_ids}}))

        if mode == 'replace':
            will_insert = incoming
            will_update = 0
            will_delete = existing_total
            will_skip = 0
        elif mode == 'insert_only':
            will_insert = max(0, incoming - matching_ids)
            will_update = 0
            will_delete = 0
            will_skip = matching_ids
        else:  # upsert
            will_insert = max(0, incoming - matching_ids)
            will_update = matching_ids
            will_delete = 0
            will_skip = 0

        return {
            'collection': coll_name,
            'label': MONGO_COLLECTIONS[coll_name]['label'],
            'incoming': incoming,
            'existing_total': existing_total,
            'matching_ids': matching_ids,
            'will_insert': will_insert,
            'will_update': will_update,
            'will_skip': will_skip,
            'will_delete': will_delete,
        }

    items = list(parsed_snapshot['collections'].items())
    if items:
        analyzed = parallel_run(
            {coll_name: (lambda cn=coll_name, d=docs: _inspect_collection(cn, d)) for coll_name, docs in items},
            max_workers=min(6, len(items)),
        )
        per_collection = [analyzed[coll_name] for coll_name, _ in items]

    return {
        'mode': mode,
        'is_v2': parsed_snapshot['is_v2'],
        'exported_at': parsed_snapshot['meta'].get('exported_at', ''),
        'include_secrets': parsed_snapshot['include_secrets'],
        'rows': per_collection,
        'warnings': warnings,
    }


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission('backup.view')
def backup_management(request):
    logs = BackupLog.objects.all()[:20]
    collections = [
        {
            'name': key,
            'label': cfg.get('label', key),
            'has_secrets_projection': bool(cfg.get('projection')),
        }
        for key, cfg in MONGO_COLLECTIONS.items()
    ]

    pending_restore = request.session.get(_RESTORE_SESSION_KEY)
    if pending_restore:
        expires_at = pending_restore.get('expires_at', 0)
        fpath = pending_restore.get('file_path', '')
        if int(timezone.now().timestamp()) > int(expires_at) or not (fpath and Path(fpath).is_file()):
            _cleanup_staging_entry(pending_restore)
            request.session.pop(_RESTORE_SESSION_KEY, None)
            pending_restore = None

    context = {
        'logs': logs,
        'collections': collections,
        'pending_restore': pending_restore,
        'can_destructive_replace': _can_destructive_replace(request.user),
    }
    return render(request, 'admin_dashboard/backup/backup.html', context)


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_permission(('backup.export', 'backup.edit'))
def backup_download(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    selected = request.POST.getlist('collections') or list(MONGO_COLLECTIONS.keys())
    include_secrets = request.POST.get('include_secrets') == 'on'

    log = BackupLog.objects.create(
        created_by=request.user,
        collections=[c for c in selected if c in MONGO_COLLECTIONS],
        status='in_progress',
    )
    try:
        spooled = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode='w+b')
        file_size, counts, valid_selected = write_mongo_backup_snapshot(
            spooled,
            selected=selected,
            include_secrets=include_secrets,
        )
        spooled.seek(0)
        log.collections = valid_selected
        log.file_size = file_size
        log.status = 'success'
        log.save()

        AuditLog.log(
            request,
            'backup_database',
            'backup',
            str(log.id),
            {'collections': valid_selected, 'counts': counts, 'include_secrets': include_secrets, 'size': log.file_size},
        )

        filename = f'backup_{datetime.now():%Y%m%d_%H%M%S}.json'
        response = FileResponse(
            spooled,
            as_attachment=True,
            filename=filename,
            content_type='application/json; charset=utf-8',
        )
        response['Content-Length'] = file_size
        return response
    except Exception as e:
        log.status = 'failed'
        log.error_message = str(e)
        log.save()
        messages.error(request, f'Sao lưu thất bại: {e}')
        return redirect('admin_dashboard:backup')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
@require_permission('backup.edit')
def backup_restore_preview(request):
    """Upload backup JSON and compute read-only Dry-Run diff preview before restore execution."""
    mode = (request.POST.get('mode') or 'upsert').strip()
    if mode not in ('upsert', 'insert_only', 'replace'):
        mode = 'upsert'

    if mode == 'replace' and not _can_destructive_replace(request.user):
        if is_json_request(request):
            return JsonResponse({'success': False, 'error': 'Chế độ Thay thế toàn bộ yêu cầu quyền xóa sao lưu.'}, status=403)
        messages.error(request, 'Chế độ Thay thế toàn bộ yêu cầu quyền xóa sao lưu hoặc Super Admin!')
        return redirect('admin_dashboard:backup')

    upload = request.FILES.get('backup_file')
    if not upload:
        if is_json_request(request):
            return JsonResponse({'success': False, 'error': 'Vui lòng chọn file backup JSON.'}, status=400)
        messages.error(request, 'Vui lòng chọn file backup (.json) để kiểm tra trước khi khôi phục.')
        return redirect('admin_dashboard:backup')

    if upload.size > _MAX_RESTORE_BYTES:
        if is_json_request(request):
            return JsonResponse({'success': False, 'error': 'File backup vượt quá giới hạn 50MB.'}, status=400)
        messages.error(request, 'File backup vượt quá giới hạn tối đa 50MB!')
        return redirect('admin_dashboard:backup')

    raw_bytes = upload.read()
    try:
        parsed = parse_backup_snapshot(raw_bytes)
        preview = _compute_restore_preview(parsed, mode)
    except Exception as exc:
        if is_json_request(request):
            return JsonResponse({'success': False, 'error': f'Invalid backup file: {exc}'}, status=400)
        messages.error(request, f'File backup không hợp lệ: {exc}')
        return redirect('admin_dashboard:backup')

    # Clean up any prior staged file for this session
    _cleanup_staging_entry(request.session.get(_RESTORE_SESSION_KEY))

    _RESTORE_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staged_path = _RESTORE_STAGING_DIR / f'{token}.json'
    staged_path.write_bytes(raw_bytes)
    sha256 = hashlib.sha256(raw_bytes).hexdigest()

    pending_payload = {
        'token': token,
        'sha256': sha256,
        'file_path': str(staged_path),
        'filename': os.path.basename(upload.name or 'backup.json'),
        'file_size': len(raw_bytes),
        'user_id': request.user.pk,
        'expires_at': int(timezone.now().timestamp()) + 900,
        'preview': preview,
    }
    request.session[_RESTORE_SESSION_KEY] = pending_payload
    request.session.modified = True

    if is_json_request(request):
        return JsonResponse({'success': True, 'token': token, 'sha256': sha256, 'preview': preview})

    messages.info(
        request,
        f'Đã phân tích bản xem trước cho "{pending_payload["filename"]}". '
        'Kiểm tra bảng tác động bên dưới và nhấn Xác nhận khôi phục.',
    )
    return redirect('admin_dashboard:backup')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
@require_permission('backup.edit')
def backup_restore_confirm(request):
    """Execute restore from a previously previewed and verified staging snapshot."""
    from users_management.rate_limit import consume_action_rate_limit, format_retry_wait
    allowed, retry_after, _ = consume_action_rate_limit('admin_sensitive', request)
    if not allowed:
        msg = f'Thao tác quá nhanh. Vui lòng thử lại sau {format_retry_wait(retry_after)}.'
        if is_json_request(request):
            return JsonResponse({'success': False, 'error': msg, 'retryAfter': retry_after}, status=429)
        messages.error(request, msg)
        return redirect('admin_dashboard:backup')

    pending = request.session.get(_RESTORE_SESSION_KEY)
    confirm_token = (request.POST.get('confirm_token') or '').strip()

    if not pending or pending.get('user_id') != request.user.pk or not confirm_token or confirm_token != pending.get('token'):
        if is_json_request(request):
            return JsonResponse({'success': False, 'error': 'Phiên Dry-run không hợp lệ hoặc đã hết hạn.'}, status=400)
        messages.error(request, 'Phiên xác nhận khôi phục không hợp lệ hoặc đã hết hạn. Vui lòng upload & xem trước lại.')
        return redirect('admin_dashboard:backup')

    if int(timezone.now().timestamp()) > int(pending.get('expires_at') or 0):
        _cleanup_staging_entry(pending)
        request.session.pop(_RESTORE_SESSION_KEY, None)
        if is_json_request(request):
            return JsonResponse({'success': False, 'error': 'Phiên xem trước đã hết hạn.'}, status=400)
        messages.error(request, 'Phiên xem trước đã hết hạn. Vui lòng tải lên lại.')
        return redirect('admin_dashboard:backup')

    mode = (request.POST.get('mode') or pending.get('preview', {}).get('mode') or 'upsert').strip()
    if mode not in ('upsert', 'insert_only', 'replace'):
        mode = 'upsert'

    if mode == 'replace':
        if not _can_destructive_replace(request.user):
            if is_json_request(request):
                return JsonResponse({'success': False, 'error': 'Chế độ Thay thế toàn bộ yêu cầu quyền xóa sao lưu.'}, status=403)
            messages.error(request, 'Chế độ Thay thế toàn bộ yêu cầu quyền xóa sao lưu hoặc Super Admin!')
            return redirect('admin_dashboard:backup')
        if (request.POST.get('confirm_phrase') or '').strip() != 'RESTORE':
            if is_json_request(request):
                return JsonResponse({'success': False, 'error': 'Vui lòng nhập chính xác chữ RESTORE để xác nhận.'}, status=400)
            messages.error(request, 'Chế độ Thay thế toàn bộ yêu cầu nhập chính xác "RESTORE" để xác nhận!')
            return redirect('admin_dashboard:backup')

    staged_path = Path(pending.get('file_path') or '')
    if not staged_path.is_file():
        request.session.pop(_RESTORE_SESSION_KEY, None)
        if is_json_request(request):
            return JsonResponse({'success': False, 'error': 'File tạm xem trước không còn tồn tại.'}, status=400)
        messages.error(request, 'File tạm xem trước không còn tồn tại. Vui lòng xem trước lại.')
        return redirect('admin_dashboard:backup')

    raw_bytes = staged_path.read_bytes()
    if hashlib.sha256(raw_bytes).hexdigest() != pending.get('sha256'):
        _cleanup_staging_entry(pending)
        request.session.pop(_RESTORE_SESSION_KEY, None)
        return JsonResponse({'success': False, 'error': 'Snapshot checksum mismatch'}, status=400)

    try:
        parsed = parse_backup_snapshot(raw_bytes)
        selected_colls = request.POST.getlist('restore_collections')
        target_colls = [
            c for c in (selected_colls or list(parsed['collections'].keys()))
            if c in parsed['collections']
        ]
        if not target_colls:
            raise ValueError('Chưa chọn collection nào để khôi phục.')

        db = mongo_admin.db()
        results = {}
        for coll_name in target_colls:
            docs = parsed['collections'][coll_name]
            inserted = 0
            updated = 0
            skipped = 0
            deleted = 0

            if mode == 'replace':
                del_res = db[coll_name].delete_many({})
                deleted = int(del_res.deleted_count or 0)
                for idx in range(0, len(docs), 500):
                    chunk = docs[idx:idx + 500]
                    if chunk:
                        ins_res = db[coll_name].insert_many(chunk, ordered=False)
                        inserted += len(ins_res.inserted_ids)
            else:
                ops = []
                for doc in docs:
                    doc_id = doc.get('_id')
                    fields = {k: v for k, v in doc.items() if k != '_id'}
                    if doc_id is None:
                        ops.append(InsertOne(doc))
                    elif mode == 'insert_only':
                        ops.append(UpdateOne({'_id': doc_id}, {'$setOnInsert': fields}, upsert=True))
                    else:  # upsert with $set preserves unexported secret fields
                        ops.append(UpdateOne({'_id': doc_id}, {'$set': fields}, upsert=True))

                    if len(ops) >= 500:
                        bw = db[coll_name].bulk_write(ops, ordered=False)
                        inserted += int(bw.inserted_count or 0) + int(bw.upserted_count or 0)
                        updated += int(bw.modified_count or 0)
                        if mode == 'insert_only':
                            skipped += int(bw.matched_count or 0)
                        ops = []
                if ops:
                    bw = db[coll_name].bulk_write(ops, ordered=False)
                    inserted += int(bw.inserted_count or 0) + int(bw.upserted_count or 0)
                    updated += int(bw.modified_count or 0)
                    if mode == 'insert_only':
                        skipped += int(bw.matched_count or 0)

            results[coll_name] = {
                'inserted': inserted,
                'updated': updated,
                'skipped': skipped,
                'deleted': deleted,
            }

        AuditLog.log(
            request,
            'restore_database',
            'backup',
            pending.get('filename', ''),
            {'mode': mode, 'collections': target_colls, 'results': results},
        )
        from users_management.webhook_dispatcher import dispatch_webhook_event
        dispatch_webhook_event('backup.completed', {
            'action': 'restore',
            'mode': mode,
            'filename': pending.get('filename', ''),
            'collections': target_colls,
        })
    except Exception as exc:
        if is_json_request(request):
            return JsonResponse({'success': False, 'error': str(exc)}, status=500)
        messages.error(request, f'Khôi phục thất bại: {exc}')
        return redirect('admin_dashboard:backup')
    finally:
        _cleanup_staging_entry(pending)
        request.session.pop(_RESTORE_SESSION_KEY, None)
        request.session.modified = True

    if is_json_request(request):
        return JsonResponse({'success': True, 'mode': mode, 'results': results})

    total_ins = sum(r['inserted'] for r in results.values())
    total_upd = sum(r['updated'] for r in results.values())
    mode_labels = {
        'upsert': 'cập nhật & bổ sung',
        'insert_only': 'chỉ thêm mới',
        'replace': 'thay thế toàn bộ',
    }
    mode_label = mode_labels.get(mode, mode)
    messages.success(
        request,
        f'Khôi phục thành công ({mode_label}): +{total_ins} bản ghi thêm mới, ~{total_upd} bản ghi cập nhật trên {len(results)} bảng dữ liệu.',
    )
    return redirect('admin_dashboard:backup')


@login_required
@user_passes_test(check_admin_access, login_url='/dashboard/login/')
@require_POST
@require_permission('backup.edit')
def backup_restore_cancel(request):
    """Cancel pending Dry-run restore session and remove temp staged snapshot."""
    _cleanup_staging_entry(request.session.get(_RESTORE_SESSION_KEY))
    request.session.pop(_RESTORE_SESSION_KEY, None)
    request.session.modified = True
    messages.info(request, 'Đã hủy phiên xem trước khôi phục.')
    return redirect('admin_dashboard:backup')

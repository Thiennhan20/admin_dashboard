"""Comprehensive unit and integration tests for Admin Panel security, RBAC, 2FA, Push Devices, Sessions, Backup/Restore, and Scheduled Tasks."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import hmac
import io
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bson import ObjectId, json_util
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from pymongo import InsertOne, UpdateOne

from users_management.models import (
    AdminProfile,
    AuditLog,
    BackupLog,
    ErrorLog,
    ScheduledTask,
    TaskRunLog,
    WebhookEndpoint,
    WebhookLog,
)
from users_management.rate_limit import (
    check_rate_limit,
    clear_failures,
    consume_action_rate_limit,
    record_failure,
    reset_memory_rate_limits,
)
from users_management.services import mongo_admin
from users_management.templatetags.admin_tags import format_interval
from users_management.totp import (
    generate_backup_codes,
    generate_totp_secret,
    get_totp_code,
    render_qr_svg_data_uri,
    verify_and_consume_backup_code,
    verify_totp_code,
)
from users_management.views.backup import (
    create_mongo_backup_payload,
    parse_backup_snapshot,
)
from users_management.views.push_devices import send_expo_push_batch
from users_management.views.scheduled_tasks import run_due_scheduled_tasks
from users_management.webhook_dispatcher import dispatch_webhook_event


def _match_query(doc, query):
    """Evaluate basic MongoDB find/update filter predicates on in-memory dicts."""
    if not query:
        return True
    for key, expected in query.items():
        if key == '$or':
            if not any(_match_query(doc, branch) for branch in expected):
                return False
            continue
        if key in ('pushTokens.0', 'watchlist.0'):
            field_name = key.split('.', 1)[0]
            exists_want = bool((expected or {}).get('$exists', False))
            has_elem = isinstance(doc.get(field_name), list) and len(doc[field_name]) > 0
            if exists_want != has_elem:
                return False
            continue

        if key in doc:
            val = doc.get(key)
        elif '.' in key:
            cur = doc
            for part in key.split('.'):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = None
                    break
            val = cur
        else:
            val = None
        if isinstance(expected, dict):
            if '$in' in expected:
                hay = val if isinstance(val, list) else [val]
                if not any(item in expected['$in'] for item in hay):
                    return False
            if '$ne' in expected:
                if val == expected['$ne']:
                    return False
            if '$lt' in expected:
                if not (val is not None and val < expected['$lt']):
                    return False
            if '$lte' in expected:
                if not (val is not None and val <= expected['$lte']):
                    return False
            if '$gte' in expected:
                if not (val is not None and val >= expected['$gte']):
                    return False
            if '$gt' in expected:
                if not (val is not None and val > expected['$gt']):
                    return False
            if '$exists' in expected:
                exists = key in doc
                if bool(expected['$exists']) != exists:
                    return False
            if '$regex' in expected:
                flags = re.IGNORECASE if 'i' in str(expected.get('$options', '')) else 0
                if val is None or not re.search(expected['$regex'], str(val), flags):
                    return False
        else:
            if isinstance(val, list) and not isinstance(expected, list):
                if expected not in val:
                    return False
            elif val != expected:
                return False
    return True


def _apply_projection(doc, projection):
    if not projection:
        return deepcopy(doc)
    excludes = [k for k, v in projection.items() if v == 0]
    includes = [k for k, v in projection.items() if v == 1]
    if excludes and not includes:
        return {k: deepcopy(v) for k, v in doc.items() if k not in excludes}
    if includes:
        res = {k: deepcopy(doc[k]) for k in includes if k in doc}
        if projection.get('_id', 1) != 0 and '_id' in doc:
            res['_id'] = deepcopy(doc['_id'])
        return res
    return deepcopy(doc)


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *args, **kwargs):
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeCollection:
    """In-memory MongoDB collection stand-in for offline tests."""

    def __init__(self, docs=None):
        self.docs = [deepcopy(d) for d in (docs or [])]

    def find(self, query=None, projection=None):
        matched = [_apply_projection(d, projection) for d in self.docs if _match_query(d, query)]
        return FakeCursor(matched)

    def find_one(self, query=None, projection=None):
        for d in self.docs:
            if _match_query(d, query):
                return _apply_projection(d, projection)
        return None

    def count_documents(self, query=None):
        return sum(1 for d in self.docs if _match_query(d, query))

    def estimated_document_count(self):
        return len(self.docs)

    def distinct(self, field, query=None):
        seen = []
        for d in self.docs:
            if field in d and _match_query(d, query):
                val = d[field]
                if val not in seen:
                    seen.append(val)
        return seen

    def delete_one(self, query):
        for idx, d in enumerate(self.docs):
            if _match_query(d, query):
                del self.docs[idx]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    def delete_many(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _match_query(d, query)]
        return SimpleNamespace(deleted_count=before - len(self.docs))

    def insert_one(self, doc):
        item = deepcopy(doc)
        if '_id' not in item:
            item['_id'] = ObjectId()
        self.docs.append(item)
        return SimpleNamespace(inserted_id=item['_id'])

    def insert_many(self, docs, ordered=False):
        ids = []
        for d in docs:
            item = deepcopy(d)
            if '_id' not in item:
                item['_id'] = ObjectId()
            ids.append(item['_id'])
            self.docs.append(item)
        return SimpleNamespace(inserted_ids=ids)

    def update_one(self, query, update, upsert=False):
        return self._apply_update(query, update, upsert=upsert, multi=False)

    def update_many(self, query, update, upsert=False):
        return self._apply_update(query, update, upsert=upsert, multi=True)

    def _apply_update(self, query, update, upsert=False, multi=True):
        matched = 0
        modified = 0
        upserted_id = None
        for d in self.docs:
            if not _match_query(d, query):
                continue
            matched += 1
            changed = False
            if '$set' in update:
                for k, v in update['$set'].items():
                    if d.get(k) != v:
                        d[k] = deepcopy(v)
                        changed = True
            if '$pull' in update:
                for field, cond in update['$pull'].items():
                    arr = d.get(field)
                    if isinstance(arr, list):
                        targets = cond['$in'] if isinstance(cond, dict) and '$in' in cond else [cond]
                        new_arr = [x for x in arr if x not in targets]
                        if len(new_arr) != len(arr):
                            d[field] = new_arr
                            changed = True
            if '$inc' in update:
                for k, delta in update['$inc'].items():
                    d[k] = (d.get(k) or 0) + delta
                    changed = True
            if changed:
                modified += 1
            if not multi:
                break

        if matched == 0 and upsert:
            new_doc = {}
            if '_id' in query:
                new_doc['_id'] = deepcopy(query['_id'])
            else:
                new_doc['_id'] = ObjectId()
            for k, v in (update.get('$setOnInsert') or {}).items():
                new_doc[k] = deepcopy(v)
            for k, v in (update.get('$set') or {}).items():
                new_doc[k] = deepcopy(v)
            self.docs.append(new_doc)
            upserted_id = new_doc['_id']

        return SimpleNamespace(
            matched_count=matched,
            modified_count=modified,
            upserted_id=upserted_id,
        )

    def bulk_write(self, ops, ordered=False):
        inserted_count = 0
        upserted_count = 0
        modified_count = 0
        matched_count = 0
        for op in ops:
            if isinstance(op, InsertOne):
                doc = deepcopy(op._doc)
                if '_id' not in doc:
                    doc['_id'] = ObjectId()
                self.docs.append(doc)
                inserted_count += 1
            elif isinstance(op, UpdateOne):
                res = self._apply_update(op._filter, op._doc, upsert=bool(op._upsert), multi=False)
                matched_count += res.matched_count
                modified_count += res.modified_count
                if res.upserted_id is not None:
                    upserted_count += 1
        return SimpleNamespace(
            inserted_count=inserted_count,
            upserted_count=upserted_count,
            modified_count=modified_count,
            matched_count=matched_count,
        )

    def aggregate(self, pipeline):
        # Support pushTokens, watchlist, and analytics aggregations tested across views
        has_facet = any('$facet' in st for st in pipeline)
        has_malformed_count = any('$count' in st and st['$count'] == 'malformed' for st in pipeline)
        has_malformed_group = any('$group' in st and 'tokens' in st['$group'] for st in pipeline)
        has_kpi_group = any('$group' in st and 'users_count' in st['$group'] for st in pipeline)
        has_sidebar_group = any('$group' in st and 'total' in st['$group'] for st in pipeline)

        if any(st.get('$unwind') == '$watchlist' for st in pipeline):
            wl_rows = []
            for u in self.docs:
                for item in (u.get('watchlist') or []):
                    wl_rows.append({
                        '_id': u['_id'],
                        'name': u.get('name', ''),
                        'email': u.get('email', ''),
                        'watchlist': deepcopy(item),
                    })
            if has_facet:
                counts_by_id = {}
                type_counts = {}
                for row in wl_rows:
                    wl = row['watchlist'] or {}
                    wid = wl.get('id')
                    if wid is not None:
                        bucket = counts_by_id.setdefault(wid, {'_id': wid, 'title': wl.get('title', ''), 'count': 0})
                        bucket['count'] += 1
                    wtype = wl.get('type') or 'unknown'
                    type_counts[wtype] = type_counts.get(wtype, 0) + 1
                top_wl = sorted(counts_by_id.values(), key=lambda x: x['count'], reverse=True)[:10]
                types_list = [{'_id': k, 'count': v} for k, v in type_counts.items()]
                return [{'top_watchlisted': top_wl, 'content_types': types_list}]
            return wl_rows

        if has_facet and any('heatmap' in (st.get('$facet') or {}) for st in pipeline):
            by_hour = {}
            per_day = {}
            heatmap = {}
            for doc in self.docs:
                ts = doc.get('createdAt')
                if isinstance(ts, datetime):
                    by_hour[ts.hour] = by_hour.get(ts.hour, 0) + 1
                    d_str = ts.strftime('%Y-%m-%d')
                    per_day[d_str] = per_day.get(d_str, 0) + 1
                    dow = ((ts.weekday() + 1) % 7) + 1
                    hk = (dow, ts.hour)
                    heatmap[hk] = heatmap.get(hk, 0) + 1
            return [{
                'by_hour': [{'_id': h, 'count': c} for h, c in sorted(by_hour.items())],
                'per_day': [{'_id': d, 'count': c} for d, c in sorted(per_day.items())],
                'heatmap': [{'_id': {'dow': d, 'hour': h}, 'count': c} for (d, h), c in heatmap.items()],
            }]

        token_rows = []
        for u in self.docs:
            for idx, tok in enumerate(u.get('pushTokens') or []):
                is_valid = bool(re.match(r'^ExponentPushToken\[.+\]$|^ExpoPushToken\[.+\]$', str(tok)))
                token_rows.append({
                    'user_id': str(u['_id']),
                    'user_name': u.get('name', 'Unknown'),
                    'user_email': u.get('email', ''),
                    'is_locked': bool(u.get('isLocked', False)),
                    'updatedAt': u.get('updatedAt'),
                    'token': tok,
                    'token_index': idx,
                    'is_valid_format': is_valid,
                })

        if has_sidebar_group:
            return [{'_id': None, 'total': len(token_rows)}] if token_rows else []
        if has_malformed_count:
            bad = sum(1 for r in token_rows if not r['is_valid_format'])
            return [{'malformed': bad}] if bad else []
        if has_malformed_group:
            bad_toks = sorted({r['token'] for r in token_rows if not r['is_valid_format']})
            return [{'_id': None, 'tokens': bad_toks}] if bad_toks else []
        if has_kpi_group:
            users_with = [u for u in self.docs if u.get('pushTokens')]
            return [{
                '_id': None,
                'users_count': len(users_with),
                'tokens_count': len(token_rows),
                'locked_users': sum(1 for u in users_with if u.get('isLocked')),
            }]
        if has_facet:
            if any('$unwind' in st and '$pushTokens' in str(st) for st in pipeline):
                return [{'meta': [{'total': len(token_rows)}], 'items': token_rows}]
            return [{'meta': [{'total': len(self.docs)}], 'items': deepcopy(self.docs)}]
        if any('$limit' in st for st in pipeline):
            return deepcopy(self.docs)
        return []


class FakeDatabase:
    def __init__(self, initial=None):
        self._collections = {k: FakeCollection(v) for k, v in (initial or {}).items()}

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return self._collections.setdefault(name, FakeCollection())

    def __getitem__(self, name):
        return self._collections.setdefault(name, FakeCollection())


@override_settings(
    UPSTASH_REDIS_URL='',
    UPSTASH_REDIS_TOKEN='',
    SECURE_SSL_REDIRECT=False,
    SESSION_COOKIE_SECURE=False,
    CSRF_COOKIE_SECURE=False,
)
class BaseAdminTestCase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        reset_memory_rate_limits()

    def _create_admin(self, username, role='admin', is_superuser=False, permissions=None, password='Password123!'):
        user = User.objects.create_user(
            username=username,
            email=f'{username}@example.com',
            password=password,
            is_staff=True,
            is_superuser=is_superuser,
        )
        profile = AdminProfile.objects.create(
            user=user,
            role=role,
            permissions=permissions if permissions is not None else {},
        )
        return user, profile


class RbacAndMiddlewareTests(BaseAdminTestCase):
    def test_bootstrap_admin_register_creates_super_admin_and_closes(self):
        self.assertFalse(User.objects.filter(is_staff=True).exists())
        resp = self.client.post(reverse('admin_dashboard:register'), {
            'username': 'bootstrap_super',
            'password1': 'StrongPass!2026',
            'password2': 'StrongPass!2026',
        })
        self.assertEqual(resp.status_code, 302)
        user = User.objects.get(username='bootstrap_super')
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.admin_profile.role, 'super_admin')
        self.assertEqual(user.admin_profile.permissions, {})

        # Once a staff account exists, anonymous registration must be closed
        resp2 = self.client.get(reverse('admin_dashboard:register'))
        self.assertRedirects(resp2, reverse('admin_dashboard:login'), fetch_redirect_response=False)

    def test_admin_profile_has_perm_overrides_and_boolean_coercion(self):
        _, viewer_prof = self._create_admin('viewer1', role='viewer', permissions={'settings.view': True, 'users.view': False})
        self.assertTrue(viewer_prof.has_perm('dashboard.view'))
        self.assertTrue(viewer_prof.has_perm('settings.view'))
        self.assertFalse(viewer_prof.has_perm('users.view'))
        self.assertFalse(viewer_prof.has_perm('users.delete'))

        _, super_prof = self._create_admin('super1', role='super_admin', is_superuser=False)
        self.assertTrue(super_prof.is_super_admin)
        self.assertTrue(super_prof.has_perm('backup.delete'))

    def test_admin_edit_matrix_and_escalation_guards(self):
        super_user, _ = self._create_admin('super_actor', role='super_admin', is_superuser=True)
        normal_admin, _ = self._create_admin(
            'sub_admin',
            role='admin',
            is_superuser=False,
            permissions={'admins.edit': True, 'admins.view': True},
        )
        viewer_user, _ = self._create_admin('target_viewer', role='viewer')

        # Normal admin with overridden admins.edit cannot escalate viewer -> super_admin (view guard refuses & returns 200)
        self.client.force_login(normal_admin)
        resp = self.client.post(reverse('admin_dashboard:admin_edit', args=[viewer_user.id]), {
            'username': 'target_viewer',
            'email': 'target_viewer@example.com',
            'role': 'super_admin',
            'is_active': 'on',
        })
        self.assertEqual(resp.status_code, 200)
        viewer_user.admin_profile.refresh_from_db()
        self.assertEqual(viewer_user.admin_profile.role, 'viewer')

        # Moderator without admins.edit is blocked on JSON requests with 403
        mod_user, _ = self._create_admin('mod_no_admin_edit', role='moderator')
        self.client.force_login(mod_user)
        resp_mod = self.client.post(
            reverse('admin_dashboard:admin_edit', args=[viewer_user.id]),
            {'username': 'target_viewer', 'email': 'target_viewer@example.com', 'role': 'admin'},
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(resp_mod.status_code, 403)
        viewer_user.admin_profile.refresh_from_db()
        self.assertEqual(viewer_user.admin_profile.role, 'viewer')

        # Super admin saves custom permission overrides via matrix without crashing
        self.client.force_login(super_user)
        resp_get = self.client.get(reverse('admin_dashboard:admin_edit', args=[viewer_user.id]))
        self.assertEqual(resp_get.status_code, 200)

        resp_post = self.client.post(reverse('admin_dashboard:admin_edit', args=[viewer_user.id]), {
            'username': 'target_viewer',
            'email': 'target_viewer@example.com',
            'role': 'viewer',
            'is_active': 'on',
            'perm_matrix_submitted': '1',
            'perm_dashboard.view': 'on',
            'perm_users.export': 'on',
        })
        self.assertEqual(resp_post.status_code, 302)
        viewer_user.admin_profile.refresh_from_db()
        self.assertTrue(viewer_user.admin_profile.has_perm('users.export'))

    def test_admin_delete_requires_post_and_admins_delete_perm(self):
        super_user, _ = self._create_admin('super_del', role='super_admin', is_superuser=True)
        viewer_user, _ = self._create_admin('victim_viewer', role='viewer')

        self.client.force_login(super_user)
        self.assertEqual(
            self.client.get(reverse('admin_dashboard:admin_delete', args=[viewer_user.id])).status_code,
            405,
        )

        mod_user, _ = self._create_admin('mod_no_del', role='moderator')
        self.client.force_login(mod_user)
        # Middleware + decorator deny moderator on /dashboard/admins/
        resp_mod = self.client.post(
            reverse('admin_dashboard:admin_delete', args=[viewer_user.id]),
            HTTP_ACCEPT='application/json',
        )
        self.assertEqual(resp_mod.status_code, 403)
        self.assertTrue(User.objects.filter(id=viewer_user.id).exists())

        self.client.force_login(super_user)
        resp_ok = self.client.post(reverse('admin_dashboard:admin_delete', args=[viewer_user.id]))
        self.assertEqual(resp_ok.status_code, 302)
        self.assertFalse(User.objects.filter(id=viewer_user.id).exists())

    @patch('users_management.services.public_rooms', return_value=([], None))
    @patch('users_management.services.mongo_admin')
    def test_viewer_read_only_and_sidebar_gating(self, mock_mongo, _mock_rooms):
        fake_db = FakeDatabase()
        mock_mongo.enabled = True
        mock_mongo.db.return_value = fake_db

        viewer, _ = self._create_admin('viewer_ro', role='viewer')
        self.client.force_login(viewer)

        # Viewer can read profile and sidebar hides restricted links
        prof_resp = self.client.get(reverse('admin_dashboard:admin_profile'))
        self.assertEqual(prof_resp.status_code, 200)
        html = prof_resp.content.decode('utf-8')
        self.assertIn(reverse('admin_dashboard:sessions_list'), html)
        self.assertIn(reverse('admin_dashboard:push_devices'), html)
        self.assertIn('sidebar-collapsed-init', html)
        self.assertIn('grid-template-columns: 72px', html)
        self.assertNotIn('fa-chart-Mixed', html)
        self.assertNotIn('fa-shield-keyhole', html)
        self.assertNotIn(reverse('admin_dashboard:system_settings'), html)
        self.assertNotIn(reverse('admin_dashboard:backup'), html)
        self.assertNotIn('href="/admin/"', html)

        # Viewer blocked on mutation & restricted endpoints (JSON -> 403)
        for url in [
            reverse('admin_dashboard:webhooks'),
            reverse('admin_dashboard:backup'),
            reverse('admin_dashboard:system_settings'),
            reverse('admin_dashboard:api_push_devices_remove'),
            reverse('admin_dashboard:api_vidsrc_scan'),
        ]:
            res = self.client.post(url, {}, HTTP_ACCEPT='application/json')
            self.assertEqual(res.status_code, 403, f'Expected 403 on POST {url}, got {res.status_code}')

    def test_django_admin_login_route_is_locked(self):
        resp = self.client.get('/admin/login/?next=/dashboard/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/dashboard/login/', resp['Location'])

    def test_auth_templates_alerts_and_toggles(self):
        # 1. Register page during bootstrap keeps Login link (no Django Admin button) + compact toggles
        reg_resp = self.client.get(reverse('admin_dashboard:register'))
        self.assertEqual(reg_resp.status_code, 200)
        self.assertContains(reg_resp, 'id="themeToggle"')
        self.assertContains(reg_resp, 'data-lang-compact="true"')
        self.assertContains(reg_resp, reverse('admin_dashboard:login'))
        self.assertNotContains(reg_resp, reverse('admin:index'))
        self.assertContains(reg_resp, 'localStorage.getItem(\'admin-theme\') || \'dark\'')

        # 2. Login page renders controls, brand lockup, semantic error alert (and hides Django Admin button)
        login_get = self.client.get(reverse('admin_dashboard:login'))
        self.assertEqual(login_get.status_code, 200)
        self.assertContains(login_get, 'id="themeToggle"')
        self.assertContains(login_get, 'data-lang-compact="true"')
        self.assertContains(login_get, 'fa-icons')
        self.assertContains(login_get, 'brand-lockup')
        self.assertContains(login_get, '<div class="brand-title">ENTN</div>')
        self.assertNotContains(login_get, reverse('admin:index'))

        login_err = self.client.post(reverse('admin_dashboard:login'), {'username': '', 'password': ''})
        self.assertEqual(login_err.status_code, 200)
        self.assertContains(login_err, 'alert-error')
        self.assertContains(login_err, 'alert-icon')
        self.assertContains(login_err, 'fa-circle-xmark')
        self.assertContains(login_err, 'Vui lòng nhập đầy đủ thông tin!')

        # 3. Login 2FA page renders controls and alert/theme styles cleanly
        admin_user, prof = self._create_admin('auth_ui_admin', role='admin')
        prof.totp_secret = generate_totp_secret()
        prof.totp_enabled = True
        prof.save()
        post_login = self.client.post(
            reverse('admin_dashboard:login'),
            {'username': 'auth_ui_admin', 'password': 'Password123!'},
        )
        self.assertRedirects(post_login, reverse('admin_dashboard:login_2fa'), fetch_redirect_response=False)
        tfa_resp = self.client.get(reverse('admin_dashboard:login_2fa'))
        self.assertEqual(tfa_resp.status_code, 200)
        self.assertContains(tfa_resp, 'id="themeToggle"')
        self.assertContains(tfa_resp, 'data-lang-compact="true"')
        self.assertContains(tfa_resp, 'localStorage.getItem(\'admin-theme\') || \'dark\'')

        tfa_err = self.client.post(reverse('admin_dashboard:login_2fa'), {'code': '000001'})
        self.assertEqual(tfa_err.status_code, 200)
        self.assertContains(tfa_err, 'alert-error')
        self.assertContains(tfa_err, 'fa-circle-xmark')


class TotpTwoFactorTests(BaseAdminTestCase):
    def test_totp_helpers_and_single_use_backup_codes(self):
        secret = generate_totp_secret()
        code = get_totp_code(secret)
        self.assertEqual(len(code), 6)
        self.assertTrue(verify_totp_code(secret, code))
        self.assertFalse(verify_totp_code(secret, '000000' if code != '000000' else '111111'))

        qr_uri = render_qr_svg_data_uri(f'otpauth://totp/Test?secret={secret}')
        self.assertTrue(qr_uri.startswith('data:image/svg+xml;base64,'))

        user, profile = self._create_admin('totp_helper_user', role='admin')
        plain, hashed = generate_backup_codes(count=8)
        self.assertEqual(len(plain), 8)
        profile.totp_backup_codes = hashed
        profile.save()

        first_code = plain[0]
        self.assertTrue(verify_and_consume_backup_code(profile, first_code))
        profile.refresh_from_db()
        self.assertEqual(len(profile.totp_backup_codes), 7)
        # Reusing the same code fails
        self.assertFalse(verify_and_consume_backup_code(profile, first_code))

    @patch('users_management.services.public_rooms', return_value=([], None))
    @patch('users_management.services.mongo_admin')
    def test_2fa_enrollment_login_challenge_backup_code_and_superadmin_reset(self, mock_mongo, _mock_rooms):
        mock_mongo.enabled = False
        super_user, _ = self._create_admin('super_2fa', role='super_admin', is_superuser=True)
        admin_user, profile = self._create_admin('admin_2fa', role='admin')

        self.client.force_login(admin_user)
        # Step 1: initiate 2FA setup -> renders offline SVG QR code on profile page
        self.client.post(reverse('admin_dashboard:profile_2fa_setup'))
        prof_resp = self.client.get(reverse('admin_dashboard:admin_profile'))
        self.assertContains(prof_resp, 'data:image/svg+xml;base64,')

        secret = self.client.session.get('_2fa_setup_secret')
        self.assertTrue(secret)

        # Invalid OTP keeps 2FA disabled
        self.client.post(reverse('admin_dashboard:profile_2fa_enable'), {'otp_code': '999999'})
        profile.refresh_from_db()
        self.assertFalse(profile.totp_enabled)

        # Valid OTP enables 2FA and generates 8 backup codes
        self.client.post(reverse('admin_dashboard:profile_2fa_enable'), {'otp_code': get_totp_code(secret)})
        profile.refresh_from_db()
        self.assertTrue(profile.totp_enabled)
        self.assertEqual(len(profile.totp_backup_codes), 8)

        # Read flashed recovery codes from profile response before logout
        enabled_page = self.client.get(reverse('admin_dashboard:admin_profile')).content.decode('utf-8')
        codes_found = re.findall(r'<code[^>]*>([2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4})</code>', enabled_page)
        self.assertEqual(len(codes_found), 8)
        backup_code_to_use = codes_found[0]

        self.client.logout()

        # Login with password should NOT authenticate yet; must redirect to /dashboard/login/2fa/
        login_resp = self.client.post(reverse('admin_dashboard:login'), {
            'username': 'admin_2fa',
            'password': 'Password123!',
        })
        self.assertRedirects(login_resp, reverse('admin_dashboard:login_2fa'), fetch_redirect_response=False)
        self.assertIn('_2fa_user_id', self.client.session)
        self.assertFalse(self.client.get(reverse('admin_dashboard:admin_profile')).status_code == 200)

        # Verify OTP completes login
        otp_resp = self.client.post(reverse('admin_dashboard:login_2fa'), {'code': get_totp_code(secret)})
        self.assertRedirects(otp_resp, reverse('admin_dashboard:dashboard'), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('admin_dashboard:admin_profile')).status_code, 200)

        # Logout and verify login via single-use backup code
        self.client.logout()
        self.client.post(reverse('admin_dashboard:login'), {'username': 'admin_2fa', 'password': 'Password123!'})
        bc_resp = self.client.post(reverse('admin_dashboard:login_2fa'), {'code': backup_code_to_use})
        self.assertRedirects(bc_resp, reverse('admin_dashboard:dashboard'), fetch_redirect_response=False)
        profile.refresh_from_db()
        self.assertEqual(len(profile.totp_backup_codes), 7)

        # Cancel flow clears pending 2FA session
        self.client.logout()
        self.client.post(reverse('admin_dashboard:login'), {'username': 'admin_2fa', 'password': 'Password123!'})
        self.client.post(reverse('admin_dashboard:login_2fa_cancel'))
        self.assertNotIn('_2fa_user_id', self.client.session)

        # Super Admin emergency reset disables 2FA
        self.client.force_login(super_user)
        reset_resp = self.client.post(reverse('admin_dashboard:admin_reset_2fa', args=[admin_user.id]))
        self.assertEqual(reset_resp.status_code, 302)
        profile.refresh_from_db()
        self.assertFalse(profile.totp_enabled)
        self.assertEqual(profile.totp_secret, '')
        self.assertEqual(profile.totp_backup_codes, [])


class LoginRateLimitTests(BaseAdminTestCase):
    def test_login_and_2fa_rate_limit_triggers_429_and_clears(self):
        user, profile = self._create_admin('rl_admin', role='admin')

        # 5 wrong password attempts trigger lockout (429 on 5th)
        for attempt in range(4):
            res = self.client.post(reverse('admin_dashboard:login'), {
                'username': 'rl_admin',
                'password': 'wrong-password',
            })
            self.assertEqual(res.status_code, 200, f'Attempt {attempt + 1} should return 200')

        res_fifth = self.client.post(reverse('admin_dashboard:login'), {
            'username': 'rl_admin',
            'password': 'wrong-password',
        })
        self.assertEqual(res_fifth.status_code, 429)

        # Even valid password stays blocked while locked
        blocked_valid = self.client.post(reverse('admin_dashboard:login'), {
            'username': 'rl_admin',
            'password': 'Password123!',
        })
        self.assertEqual(blocked_valid.status_code, 429)

        # Reset and verify clean login succeeds + 2FA challenge also enforces 429
        reset_memory_rate_limits()
        secret = generate_totp_secret()
        profile.totp_secret = secret
        profile.totp_enabled = True
        profile.save()

        self.assertRedirects(
            self.client.post(reverse('admin_dashboard:login'), {'username': 'rl_admin', 'password': 'Password123!'}),
            reverse('admin_dashboard:login_2fa'),
            fetch_redirect_response=False,
        )
        for _ in range(4):
            r = self.client.post(reverse('admin_dashboard:login_2fa'), {'code': '000001'})
            self.assertEqual(r.status_code, 200)
        r5 = self.client.post(reverse('admin_dashboard:login_2fa'), {'code': '000001'})
        self.assertEqual(r5.status_code, 429)

    def test_rate_limit_unit_clear_failures(self):
        fake_req = SimpleNamespace(META={'REMOTE_ADDR': '203.0.113.42'})
        for _ in range(5):
            record_failure('admin_login', fake_req, 'unit_acct')
        allowed, retry_after, _ = check_rate_limit('admin_login', fake_req, 'unit_acct')
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)

        clear_failures('admin_login', fake_req, 'unit_acct')
        allowed_after, _, _ = check_rate_limit('admin_login', fake_req, 'unit_acct')
        self.assertTrue(allowed_after)


class SessionsAndPushDevicesTests(BaseAdminTestCase):
    @patch('users_management.services.public_rooms', return_value=([], None))
    @patch('users_management.views.push_devices.mongo_admin')
    @patch('users_management.views.token_mgmt.mongo_admin')
    @patch('users_management.services.mongo_admin')
    def test_sessions_and_push_devices_pages_and_actions(self, svc_mongo, tok_mongo, pd_mongo, _mock_rooms):
        uid1 = ObjectId()
        uid2 = ObjectId()
        sid1 = ObjectId()
        now = datetime.utcnow()
        fake_db = FakeDatabase({
            'users': [
                {
                    '_id': uid1,
                    'name': 'Alice',
                    'email': 'alice@example.com',
                    'isLocked': False,
                    'updatedAt': now,
                    'pushTokens': ['ExponentPushToken[valid1]', 'bad_token_123'],
                },
                {
                    '_id': uid2,
                    'name': 'Bob',
                    'email': 'bob@example.com',
                    'isLocked': False,
                    'updatedAt': now,
                    'pushTokens': ['ExponentPushToken[dead1]'],
                },
            ],
            'sessions': [
                {
                    '_id': sid1,
                    'userId': uid1,
                    'device': 'Chrome Windows',
                    'ip': '198.51.100.5',
                    'location': 'Hanoi, VN',
                    'createdAt': now - timedelta(hours=1),
                    'lastActive': now,
                    'expiresAt': now + timedelta(days=7),
                },
            ],
        })
        for m in (svc_mongo, tok_mongo, pd_mongo):
            m.enabled = True
            m.db.return_value = fake_db

        admin_user, _ = self._create_admin('ops_admin', role='super_admin', is_superuser=True)
        self.client.force_login(admin_user)

        # Sessions list + CSV export
        sess_resp = self.client.get(reverse('admin_dashboard:sessions_list'))
        self.assertEqual(sess_resp.status_code, 200)
        self.assertContains(sess_resp, 'Chrome Windows')

        csv_resp = self.client.get(reverse('admin_dashboard:sessions_list') + '?export=csv')
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn('text/csv', csv_resp['Content-Type'])
        self.assertIn('198.51.100.5', csv_resp.content.decode('utf-8'))

        # Push devices list
        pd_resp = self.client.get(reverse('admin_dashboard:push_devices'))
        self.assertEqual(pd_resp.status_code, 200)
        self.assertContains(pd_resp, 'ExponentPushToken[valid1]')

        # Test Expo Push auto-pruning of DeviceNotRegistered
        mock_http_resp = MagicMock()
        mock_http_resp.ok = True
        mock_http_resp.json.return_value = {
            'data': [
                {'status': 'ok', 'id': 'ticket-1'},
                {'status': 'error', 'details': {'error': 'DeviceNotRegistered'}},
            ]
        }
        with patch('users_management.views.push_devices.requests.post', return_value=mock_http_resp):
            push_res = send_expo_push_batch(
                ['ExponentPushToken[valid1]', 'ExponentPushToken[dead1]', 'invalid_fmt'],
                title='Hello',
                body='World',
            )
        self.assertEqual(push_res['sent'], 1)
        self.assertEqual(push_res['pruned'], 1)
        self.assertEqual(push_res['pruned_tokens'], ['ExponentPushToken[dead1]'])
        bob_doc = fake_db.users.find_one({'_id': uid2})
        self.assertEqual(bob_doc['pushTokens'], [])

        # Remove malformed tokens via API
        rem_resp = self.client.post(
            reverse('admin_dashboard:api_push_devices_remove'),
            data=json.dumps({'remove_malformed': True}),
            content_type='application/json',
        )
        self.assertEqual(rem_resp.status_code, 200)
        self.assertTrue(rem_resp.json()['success'])
        alice_doc = fake_db.users.find_one({'_id': uid1})
        self.assertEqual(alice_doc['pushTokens'], ['ExponentPushToken[valid1]'])


class BackupAndDryRunRestoreTests(BaseAdminTestCase):
    @patch('users_management.services.public_rooms', return_value=([], None))
    @patch('users_management.views.backup.mongo_admin')
    @patch('users_management.services.mongo_admin')
    def test_bson_v2_snapshot_and_dry_run_restore_modes(self, svc_mongo, bk_mongo, _mock_rooms):
        uid_existing = ObjectId()
        uid_new = ObjectId()
        created_dt = datetime(2026, 1, 15, 12, 0, 0)
        fake_db = FakeDatabase({
            'users': [
                {
                    '_id': uid_existing,
                    'name': 'Live User',
                    'email': 'live@example.com',
                    'password': '$2b$10$secret_hash_must_survive',
                    'createdAt': created_dt,
                },
            ],
        })
        for m in (svc_mongo, bk_mongo):
            m.enabled = True
            m.db.return_value = fake_db

        # 1. Default snapshot excludes password hash; include_secrets=True includes it + preserves ObjectId/datetime
        safe_json, _, _ = create_mongo_backup_payload(selected=['users'], include_secrets=False)
        parsed_safe = parse_backup_snapshot(safe_json.encode('utf-8'))
        self.assertTrue(parsed_safe['is_v2'])
        self.assertNotIn('password', parsed_safe['collections']['users'][0])
        self.assertIsInstance(parsed_safe['collections']['users'][0]['_id'], ObjectId)
        self.assertIsInstance(parsed_safe['collections']['users'][0]['createdAt'], datetime)

        full_json, _, _ = create_mongo_backup_payload(selected=['users'], include_secrets=True)
        parsed_full = parse_backup_snapshot(full_json.encode('utf-8'))
        self.assertEqual(parsed_full['collections']['users'][0]['password'], '$2b$10$secret_hash_must_survive')

        super_user, _ = self._create_admin('backup_super', role='super_admin', is_superuser=True)
        self.client.force_login(super_user)

        # Build incoming v2 snapshot updating existing user name + adding one new user (without secrets)
        incoming_payload = {
            '_meta': {'format': 'cinearena_backup_v2', 'include_secrets': False},
            'collections': {
                'users': [
                    {'_id': uid_existing, 'name': 'Restored Name', 'email': 'live@example.com', 'createdAt': created_dt},
                    {'_id': uid_new, 'name': 'New User', 'email': 'new@example.com', 'createdAt': created_dt},
                ],
            },
        }
        snapshot_bytes = json_util.dumps(incoming_payload, json_options=json_util.RELAXED_JSON_OPTIONS).encode('utf-8')
        upload = SimpleUploadedFile('snap.json', snapshot_bytes, content_type='application/json')

        # 2. Dry-Run Preview MUST NOT mutate MongoDB
        prev_resp = self.client.post(
            reverse('admin_dashboard:backup_restore_preview'),
            {'backup_file': upload, 'mode': 'upsert'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(prev_resp.status_code, 200)
        prev_data = prev_resp.json()
        self.assertTrue(prev_data['success'])
        row = prev_data['preview']['rows'][0]
        self.assertEqual(row['will_insert'], 1)
        self.assertEqual(row['will_update'], 1)
        self.assertEqual(fake_db.users.count_documents({}), 1)
        self.assertEqual(fake_db.users.find_one({'_id': uid_existing})['name'], 'Live User')

        token = prev_data['token']

        # Wrong confirm token is rejected cleanly
        bad_confirm = self.client.post(
            reverse('admin_dashboard:backup_restore_confirm'),
            {'confirm_token': 'wrong-token', 'mode': 'upsert'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(bad_confirm.status_code, 400)

        # 3. Confirm upsert updates non-secret fields & preserves live password hash!
        ok_confirm = self.client.post(
            reverse('admin_dashboard:backup_restore_confirm'),
            {'confirm_token': token, 'mode': 'upsert', 'restore_collections': ['users']},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(ok_confirm.status_code, 200)
        self.assertEqual(fake_db.users.count_documents({}), 2)
        updated_existing = fake_db.users.find_one({'_id': uid_existing})
        self.assertEqual(updated_existing['name'], 'Restored Name')
        self.assertEqual(updated_existing['password'], '$2b$10$secret_hash_must_survive')

        # 4. Preview + Cancel cleans up staged file
        upload2 = SimpleUploadedFile('snap2.json', snapshot_bytes, content_type='application/json')
        prev2 = self.client.post(
            reverse('admin_dashboard:backup_restore_preview'),
            {'backup_file': upload2, 'mode': 'replace'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        ).json()
        staged_path = Path(self.client.session['_backup_restore_pending']['file_path'])
        self.assertTrue(staged_path.is_file())

        # Replace mode rejects missing RESTORE phrase
        bad_phrase = self.client.post(
            reverse('admin_dashboard:backup_restore_confirm'),
            {'confirm_token': prev2['token'], 'mode': 'replace', 'confirm_phrase': 'wrong'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(bad_phrase.status_code, 400)

        # Cancel deletes staging file & clears session key
        self.client.post(reverse('admin_dashboard:backup_restore_cancel'))
        self.assertFalse(staged_path.exists())
        self.assertNotIn('_backup_restore_pending', self.client.session)


class ScheduledTasksCronTests(BaseAdminTestCase):
    def test_human_interval_formatting_replaces_broken_divisibleby(self):
        self.assertEqual(format_interval(15), '15 min')
        self.assertEqual(format_interval(60), '1 hour')
        self.assertEqual(format_interval(90), '1h 30m')
        self.assertEqual(format_interval(1440), '1 day')
        self.assertEqual(format_interval(10080), '7 days')

        t = ScheduledTask(name='Daily Backup', task_type='backup_db', interval_minutes=1440)
        self.assertEqual(t.human_interval, '1 day')

    @patch('users_management.views.backup.mongo_admin')
    @patch('users_management.views.scheduled_tasks.mongo_admin')
    def test_cron_due_execution_real_backup_and_weekly_report(self, sched_mongo, bk_mongo):
        fake_db = FakeDatabase({
            'users': [{'_id': ObjectId(), 'name': 'U1', 'createdAt': datetime.utcnow()}],
            'comments': [],
            'streamhistories': [],
            'sessions': [],
        })
        sched_mongo.enabled = True
        sched_mongo.db.return_value = fake_db
        bk_mongo.enabled = True
        bk_mongo.db.return_value = fake_db

        due_backup = ScheduledTask.objects.create(
            name='Auto DB Backup',
            task_type='backup_db',
            interval_minutes=1440,
            is_active=True,
            last_run=None,
        )
        due_report = ScheduledTask.objects.create(
            name='Weekly Summary',
            task_type='weekly_report',
            interval_minutes=10080,
            is_active=True,
            last_run=timezone.now() - timedelta(days=8),
        )
        not_due = ScheduledTask.objects.create(
            name='Recent Custom',
            task_type='custom',
            interval_minutes=60,
            is_active=True,
            last_run=timezone.now() - timedelta(minutes=5),
        )
        inactive = ScheduledTask.objects.create(
            name='Inactive Task',
            task_type='custom',
            interval_minutes=10,
            is_active=False,
            last_run=None,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with override_settings(BASE_DIR=Path(tmpdir)):
                results = run_due_scheduled_tasks()
                auto_files = list((Path(tmpdir) / 'auto_backups').glob('auto_backup_*.json'))
                self.assertEqual(len(auto_files), 1)

        executed_ids = {r['task_id'] for r in results}
        self.assertEqual(executed_ids, {due_backup.id, due_report.id})
        self.assertTrue(all(r['success'] for r in results))

        due_backup.refresh_from_db()
        due_report.refresh_from_db()
        not_due.refresh_from_db()
        self.assertEqual(due_backup.last_status, 'success')
        self.assertEqual(due_report.last_status, 'success')
        self.assertEqual(not_due.last_status, '')
        self.assertTrue(BackupLog.objects.filter(status='success').exists())
        self.assertTrue(AuditLog.objects.filter(action='weekly_report', admin_username='system_cron').exists())
        self.assertEqual(TaskRunLog.objects.filter(status='success').count(), 2)

        # Management command `--all --json` executes active due/forced tasks and prints valid JSON
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir2:
            with override_settings(BASE_DIR=Path(tmpdir2)):
                call_command('run_scheduled_tasks', all=True, json=True, stdout=out)
        cli_results = json.loads(out.getvalue())
        self.assertEqual(len(cli_results), 3)
        self.assertTrue(all(item['success'] for item in cli_results))
        self.assertFalse(ScheduledTask.objects.get(id=inactive.id).run_logs.exists())


def _extract_js_translation_dict(text, lang):
    pattern = re.compile(rf'\b{lang}\s*:\s*\{{')
    results = {}
    for match in pattern.finditer(text):
        idx = match.end()
        depth = 1
        end = idx
        in_str = None
        escaped = False
        while end < len(text) and depth > 0:
            ch = text[end]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == '\\':
                    escaped = True
                elif ch == in_str:
                    in_str = None
            else:
                if ch in ("'", '"', '`'):
                    in_str = ch
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
            end += 1
        block = text[idx:end - 1]
        for km in re.finditer(r"['\"]([a-zA-Z0-9_.-]+)['\"]\s*:\s*(['\"])((?:\\.|(?!\2).)*)\2", block):
            results[km.group(1)] = km.group(3)
    return results


class SingleLanguageI18nContractTests(BaseAdminTestCase):
    _VN_CHARS = set(
        'àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ'
        'ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ'
    )

    def test_static_i18n_dictionaries_and_template_keys_complete(self):
        base_dir = Path(settings.BASE_DIR)
        i18n_js = (base_dir / 'static' / 'admin_dashboard' / 'js' / 'admin_i18n.js').read_text(encoding='utf-8')
        global_en = _extract_js_translation_dict(i18n_js, 'en')
        global_vi = _extract_js_translation_dict(i18n_js, 'vi')

        self.assertEqual(set(global_en), set(global_vi), 'Global EN and VI keysets must match')
        self.assertIn('common.unknown', global_en)
        self.assertIn('notif.newUser', global_en)
        self.assertIn('notif.newComment', global_vi)

        tpl_dir = base_dir / 'templates' / 'admin_dashboard'
        for tpl_path in sorted(tpl_dir.rglob('*.html')):
            txt = tpl_path.read_text(encoding='utf-8')
            local_en = _extract_js_translation_dict(txt, 'en')
            local_vi = _extract_js_translation_dict(txt, 'vi')
            self.assertEqual(
                set(local_en),
                set(local_vi),
                f'EN/VI key mismatch in {tpl_path.name}',
            )
            for key, val in local_en.items():
                self.assertFalse(
                    any(ch in self._VN_CHARS for ch in val),
                    f'Vietnamese character leaked into EN key {key} in {tpl_path.name}: {val}',
                )

            merged_en = {**global_en, **local_en}
            merged_vi = {**global_vi, **local_vi}
            refs = set(re.findall(r'data-i18n(?:-[a-z-]+)?="([a-zA-Z0-9_.-]+)"', txt))
            refs |= set(re.findall(r"\bt\(\s*'([a-zA-Z0-9_.-]+)'\s*[),]", txt))
            for key in refs:
                if '{{' in key:
                    continue
                self.assertIn(key, merged_en, f'Missing EN key {key} referenced in {tpl_path.name}')
                self.assertIn(key, merged_vi, f'Missing VI key {key} referenced in {tpl_path.name}')

            no_comments = re.sub(r'<!--.*?-->|/\*.*?\*/|\{#.*?#\}', '', txt, flags=re.S)
            self.assertNotRegex(no_comments, re.compile(r'Movie Admin|CineArena', re.I), f'Stale brand in {tpl_path.name}')

            if '|timesince' in txt:
                self.assertIn('i18n-timesince', txt, f'Unwrapped |timesince in {tpl_path.name}')

    @patch('users_management.views.servers.requests.get')
    @patch('users_management.views.games.arena_health', return_value={'enabled': False, 'rooms': [], 'totals': {}})
    @patch('users_management.services.arena_health', return_value={'enabled': False, 'rooms': [], 'totals': {}})
    @patch('users_management.services.public_rooms', return_value=([], None))
    def test_key_cleaned_views_render_single_language_contracts(self, _mock_rooms, _mock_arena_svc, _mock_arena_games, mock_http_get):
        mock_http_get.return_value = MagicMock(status_code=200, ok=True, json=lambda: {})
        now = datetime.utcnow()
        uid = ObjectId()
        fake_db = FakeDatabase({
            'users': [{'_id': uid, 'name': 'Alice', 'email': 'alice@example.com', 'createdAt': now, 'updatedAt': now, 'pushTokens': ['ExponentPushToken[ok1]']}],
            'comments': [{'_id': ObjectId(), 'userId': uid, 'content': 'Great film', 'movieId': '101', 'mediaType': 'movie', 'createdAt': now}],
            'sessions': [{'_id': ObjectId(), 'userId': uid, 'device': 'Chrome', 'createdAt': now, 'lastActive': now, 'expiresAt': now + timedelta(days=1)}],
            'passwordresets': [{'_id': ObjectId(), 'email': 'alice@example.com', 'expiresAt': now + timedelta(hours=1), 'createdAt': now}],
            'blacklistedtokens': [{'_id': ObjectId(), 'token': 'tok_123', 'expiresAt': now + timedelta(hours=1), 'createdAt': now}],
            'notifications': [{'_id': ObjectId(), 'recipient': uid, 'type': 'system_broadcast', 'read': False, 'metadata': {'versionMessage': 'Hello'}, 'createdAt': now}],
            'streamhistories': [{'_id': ObjectId(), 'userId': uid, 'movieId': '101', 'title': 'Demo Film', 'mediaType': 'movie', 'links': [{'url': 'https://cdn.example/1.m3u8'}], 'createdAt': now}],
            'gameprofiles': [{'_id': ObjectId(), 'userId': uid, 'elo': 1200, 'wins': 5, 'losses': 2, 'updatedAt': now}],
            'gamehistories': [{'_id': ObjectId(), 'roomTitle': 'Match 1', 'winner': 'Alice', 'createdAt': now}],
            'userreports': [{
                '_id': ObjectId(),
                'reporterId': ObjectId(),
                'reporterName': 'Bob',
                'targetType': 'comment',
                'targetId': ObjectId(),
                'reportedUserId': uid,
                'reportedUserName': 'Alice',
                'movieId': 101,
                'contentType': 'movie',
                'targetDetail': 'Reported text sample',
                'reasonCategory': 'spam',
                'details': 'Advertising link',
                'reason': 'Spam / Advertising: Advertising link',
                'status': 'pending',
                'actionTaken': 'none',
                'createdAt': now,
            }],
        })

        ScheduledTask.objects.create(name='Nightly Backup', task_type='backup_db', interval_minutes=1440)
        admin_user, _ = self._create_admin('i18n_super', role='super_admin', is_superuser=True)
        self.client.force_login(admin_user)

        with patch.object(mongo_admin, 'uri', 'mongodb://fake'), patch.object(mongo_admin, 'db', return_value=fake_db):
            pages = [
                'admin_dashboard:dashboard',
                'admin_dashboard:servers',
                'admin_dashboard:game_profiles',
                'admin_dashboard:game_histories',
                'admin_dashboard:stream_history',
                'admin_dashboard:admin_list',
                'admin_dashboard:users_list',
                'admin_dashboard:password_resets',
                'admin_dashboard:blacklisted_tokens',
                'admin_dashboard:notifications',
                'admin_dashboard:scheduled_tasks',
                'admin_dashboard:watchlist_list',
                'admin_dashboard:comments',
                'admin_dashboard:reports',
                'admin_dashboard:content_management',
                'admin_dashboard:search_history',
                'admin_dashboard:watch_progress',
                'admin_dashboard:cache',
            ]
            for url_name in pages:
                resp = self.client.get(reverse(url_name))
                self.assertEqual(resp.status_code, 200, f'Expected 200 for {url_name}, got {resp.status_code}')
                html = resp.content.decode('utf-8')
                visible_html = re.sub(r'<!--.*?-->|/\*.*?\*/', '', html, flags=re.S)
                self.assertNotIn('Movie Admin', visible_html, f'Found Movie Admin in {url_name}')
                self.assertNotIn('CineArena', visible_html, f'Found CineArena in {url_name}')
                self.assertNotIn('PDF / In', visible_html, f'Found mixed PDF / In in {url_name}')

            sh_html = self.client.get(reverse('admin_dashboard:stream_history')).content.decode('utf-8')
            self.assertNotIn('Dubbed (Thuyết minh)', sh_html)

            st_html = self.client.get(reverse('admin_dashboard:scheduled_tasks')).content.decode('utf-8')
            self.assertIn('data-i18n="st.type_backup_db"', st_html)
            self.assertIn('class="fw-semibold interval-label"', st_html)

            notif_api = self.client.get(reverse('admin_dashboard:api_admin_notifications'))
            self.assertEqual(notif_api.status_code, 200)
            payload = notif_api.json()
            self.assertTrue(payload['success'])
            self.assertTrue(all('detail' in ev for ev in payload['events']))

            wh_resp = self.client.post(
                reverse('admin_dashboard:webhooks'),
                {'action': 'create', 'name': 'CI Hook', 'url': 'https://example.com/hook', 'is_active': 'on'},
                follow=True,
            )
            self.assertEqual(wh_resp.status_code, 200)
            wh_html = wh_resp.content.decode('utf-8')
            self.assertIn('class="flash-message-text"', wh_html)
            self.assertIn('Đã tạo Webhook thành công!', wh_html)


class UserReportModerationTests(BaseAdminTestCase):
    def _seed_mongo(self):
        now = datetime.utcnow()
        reporter_id = ObjectId()
        reporter2_id = ObjectId()
        author_id = ObjectId()
        parent_comment_id = ObjectId()
        reply_comment_id = ObjectId()
        report1_id = ObjectId()
        report2_id = ObjectId()
        session_id = ObjectId()
        notif_id = ObjectId()

        return {
            'ids': {
                'reporter': reporter_id,
                'reporter2': reporter2_id,
                'author': author_id,
                'parent': parent_comment_id,
                'reply': reply_comment_id,
                'report1': report1_id,
                'report2': report2_id,
            },
            'db': FakeDatabase({
                'users': [
                    {'_id': author_id, 'name': 'ToxicAuthor', 'email': 'toxic@example.com', 'isBanned': False, 'createdAt': now},
                    {'_id': reporter_id, 'name': 'ReporterOne', 'email': 'rep1@example.com', 'isBanned': False, 'createdAt': now},
                ],
                'sessions': [
                    {'_id': session_id, 'userId': author_id, 'device': 'Chrome', 'createdAt': now, 'lastActive': now, 'expiresAt': now + timedelta(days=1)},
                ],
                'comments': [
                    {
                        '_id': parent_comment_id,
                        'movieId': 550,
                        'type': 'movie',
                        'userId': reporter_id,
                        'content': 'Root discussion comment',
                        'replies': [reply_comment_id],
                        'replyCount': 1,
                        'isDeleted': False,
                        'createdAt': now,
                    },
                    {
                        '_id': reply_comment_id,
                        'movieId': 550,
                        'type': 'movie',
                        'userId': author_id,
                        'parentId': parent_comment_id,
                        'content': 'Buy crypto now spam link',
                        'isDeleted': False,
                        'createdAt': now,
                    },
                ],
                'notifications': [
                    {
                        '_id': notif_id,
                        'recipient': reporter_id,
                        'type': 'comment_replied',
                        'metadata': {'commentId': reply_comment_id, 'parentCommentId': parent_comment_id},
                        'createdAt': now,
                    },
                ],
                'userreports': [
                    {
                        '_id': report1_id,
                        'reporterId': reporter_id,
                        'reporterName': 'ReporterOne',
                        'targetType': 'comment',
                        'targetId': reply_comment_id,
                        'parentCommentId': parent_comment_id,
                        'reportedUserId': author_id,
                        'reportedUserName': 'ToxicAuthor',
                        'movieId': 550,
                        'contentType': 'movie',
                        'targetDetail': 'Buy crypto now spam link',
                        'commentSnapshot': {'content': 'Buy crypto now spam link', 'authorName': 'ToxicAuthor'},
                        'reasonCategory': 'spam',
                        'details': 'Crypto scam link',
                        'reason': 'Spam / Advertising: Crypto scam link',
                        'status': 'pending',
                        'actionTaken': 'none',
                        'createdAt': now,
                    },
                    {
                        '_id': report2_id,
                        'reporterId': reporter2_id,
                        'reporterName': 'ReporterTwo',
                        'targetType': 'comment',
                        'targetId': reply_comment_id,
                        'parentCommentId': parent_comment_id,
                        'reportedUserId': author_id,
                        'reportedUserName': 'ToxicAuthor',
                        'movieId': 550,
                        'contentType': 'movie',
                        'targetDetail': 'Buy crypto now spam link',
                        'commentSnapshot': {'content': 'Buy crypto now spam link', 'authorName': 'ToxicAuthor'},
                        'reasonCategory': 'spam',
                        'details': 'Spam comment',
                        'reason': 'Spam / Advertising: Spam comment',
                        'status': 'pending',
                        'actionTaken': 'none',
                        'createdAt': now,
                    },
                ],
            }),
        }

    def test_reports_list_notifications_hide_restore_delete_and_ban(self):
        seeded = self._seed_mongo()
        fake_db = seeded['db']
        ids = seeded['ids']

        mod_user, _ = self._create_admin('mod1', role='moderator', permissions={
            'reports.view': True,
            'reports.edit': True,
            'comments.edit': True,
            'comments.delete': True,
            'users.edit': True,
        })
        self.client.force_login(mod_user)

        with patch.object(mongo_admin, 'uri', 'mongodb://fake'), patch.object(mongo_admin, 'db', return_value=fake_db):
            # 1. List renders MongoDB userreports + hydrated content + badge + notification bell
            resp = self.client.get(reverse('admin_dashboard:reports'))
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode('utf-8')
            self.assertIn('Buy crypto now spam link', html)
            self.assertIn('ToxicAuthor', html)
            self.assertIn('2 <span data-i18n="rep.reports_count">', html)

            # Search filter works
            search_resp = self.client.get(reverse('admin_dashboard:reports'), {'search': 'Crypto scam'})
            self.assertEqual(search_resp.status_code, 200)
            self.assertEqual(len(search_resp.context['reports']), 1)

            # Admin notification bell includes pending reports
            bell = self.client.get(reverse('admin_dashboard:api_admin_notifications')).json()
            self.assertTrue(bell['success'])
            self.assertEqual(bell['pending_reports'], 2)
            self.assertTrue(any(ev['type'] == 'report' for ev in bell['events']))

            # 2. Reviewing transitions status only
            act_url = reverse('admin_dashboard:report_action', args=[str(ids['report1'])])
            r_rev = self.client.post(act_url, {'action': 'reviewing'})
            self.assertEqual(r_rev.status_code, 302)
            self.assertEqual(fake_db.userreports.find_one({'_id': ids['report1']})['status'], 'reviewing')

            # 3. Hide comment resolves report + sibling report and updates parent replyCount
            r_hide = self.client.post(act_url, {
                'action': 'hide_comment',
                'note': 'Hidden due to spam',
                'apply_siblings': '1',
            })
            self.assertEqual(r_hide.status_code, 302)
            self.assertTrue(fake_db.comments.find_one({'_id': ids['reply']})['isDeleted'])
            self.assertEqual(fake_db.comments.find_one({'_id': ids['parent']})['replyCount'], 0)
            self.assertEqual(fake_db.userreports.find_one({'_id': ids['report1']})['status'], 'resolved')
            self.assertEqual(fake_db.userreports.find_one({'_id': ids['report2']})['status'], 'resolved')
            self.assertEqual(fake_db.userreports.find_one({'_id': ids['report1']})['actionTaken'], 'comment_hidden')

            # 4. Restore comment brings replyCount back to 1
            r_rest = self.client.post(act_url, {'action': 'restore_comment'})
            self.assertEqual(r_rest.status_code, 302)
            self.assertFalse(fake_db.comments.find_one({'_id': ids['reply']})['isDeleted'])
            self.assertEqual(fake_db.comments.find_one({'_id': ids['parent']})['replyCount'], 1)

            # 5. Ban author hides comment, bans user, and revokes sessions
            r_ban = self.client.post(act_url, {
                'action': 'ban_author',
                'ban_reason': 'Spam bot',
                'hide_comment': '1',
            })
            self.assertEqual(r_ban.status_code, 302)
            self.assertTrue(fake_db.users.find_one({'_id': ids['author']})['isBanned'])
            self.assertEqual(fake_db.sessions.count_documents({'userId': ids['author']}), 0)
            self.assertTrue(fake_db.comments.find_one({'_id': ids['reply']})['isDeleted'])

            # 6. Delete comment removes comment & related notification and decrements parent replyCount
            r_del = self.client.post(act_url, {'action': 'delete_comment'})
            self.assertEqual(r_del.status_code, 302)
            self.assertIsNone(fake_db.comments.find_one({'_id': ids['reply']}))
            self.assertEqual(fake_db.comments.find_one({'_id': ids['parent']})['replyCount'], 0)
            self.assertEqual(fake_db.notifications.count_documents({}), 0)

    def test_report_action_permission_guards(self):
        seeded = self._seed_mongo()
        fake_db = seeded['db']
        ids = seeded['ids']

        limited_mod, _ = self._create_admin('mod_limited', role='moderator', permissions={
            'reports.view': True,
            'reports.edit': True,
            'comments.edit': False,
            'comments.delete': False,
            'users.edit': False,
        })
        self.client.force_login(limited_mod)
        act_url = reverse('admin_dashboard:report_action', args=[str(ids['report1'])])

        with patch.object(mongo_admin, 'uri', 'mongodb://fake'), patch.object(mongo_admin, 'db', return_value=fake_db):
            self.assertEqual(self.client.post(act_url, {'action': 'hide_comment'}).status_code, 403)
            self.assertEqual(self.client.post(act_url, {'action': 'delete_comment'}).status_code, 403)
            self.assertEqual(self.client.post(act_url, {'action': 'ban_author'}).status_code, 403)
            # Basic dismiss still allowed with reports.edit
            self.assertEqual(self.client.post(act_url, {'action': 'dismissed'}).status_code, 302)


class GroupAAndCFeatureTests(BaseAdminTestCase):
    """Coverage for Nhóm A (UX/safety/exports/avatars) & Nhóm C (Email/Webhooks/RateLimit/2FA/Monitoring/Analytics/Logs)."""

    def test_a1_watchlist_export_csv_excel_pdf_and_perm_guard(self):
        now = datetime.utcnow()
        uid = ObjectId()
        fake_db = FakeDatabase({
            'users': [{
                '_id': uid,
                'name': 'Watch User',
                'email': 'watch@example.com',
                'watchlist': [
                    {'id': 550, 'title': 'Fight Club', 'type': 'movie', 'poster_path': '/fc.jpg'},
                    {'id': 1396, 'title': 'Breaking Bad', 'type': 'tv', 'poster_path': '/bb.jpg'},
                ],
                'createdAt': now,
            }],
        })

        viewer, _ = self._create_admin('wl_viewer', role='viewer')
        super_admin, _ = self._create_admin('wl_super', role='super_admin', is_superuser=True)
        url = reverse('admin_dashboard:api_export', args=['watchlist'])

        with patch.object(mongo_admin, 'uri', 'mongodb://fake'), patch.object(mongo_admin, 'db', return_value=fake_db):
            self.client.force_login(viewer)
            self.assertEqual(self.client.get(url).status_code, 403)

            self.client.force_login(super_admin)
            r_csv = self.client.get(f'{url}?format=csv')
            self.assertEqual(r_csv.status_code, 200)
            csv_body = b''.join(r_csv.streaming_content).decode('utf-8-sig')
            self.assertIn('Watch User', csv_body)
            self.assertIn('Fight Club', csv_body)
            self.assertIn('Breaking Bad', csv_body)

            r_xls = self.client.get(f'{url}?format=excel')
            self.assertEqual(r_xls.status_code, 200)
            self.assertIn('Fight Club', r_xls.content.decode('utf-8'))

            r_pdf = self.client.get(f'{url}?format=pdf')
            self.assertEqual(r_pdf.status_code, 200)
            self.assertIn('Breaking Bad', r_pdf.content.decode('utf-8'))

    @patch('users_management.services.public_rooms', return_value=([], None))
    def test_a2_bulk_comments_and_notifications_plus_ui(self, _mock_rooms):
        now = datetime.utcnow()
        uid = ObjectId()
        cid1, cid2 = ObjectId(), ObjectId()
        nid1, nid2 = ObjectId(), ObjectId()
        fake_db = FakeDatabase({
            'users': [{'_id': uid, 'name': 'Alice', 'email': 'a@example.com', 'createdAt': now}],
            'comments': [
                {'_id': cid1, 'userId': uid, 'movieId': '10', 'type': 'movie', 'content': 'Spam 1', 'isDeleted': False, 'createdAt': now},
                {'_id': cid2, 'userId': uid, 'movieId': '10', 'type': 'movie', 'content': 'Spam 2', 'isDeleted': False, 'createdAt': now},
            ],
            'notifications': [
                {'_id': nid1, 'recipient': uid, 'type': 'system_broadcast', 'read': False, 'createdAt': now},
                {'_id': nid2, 'recipient': uid, 'type': 'system_broadcast', 'read': False, 'createdAt': now},
            ],
        })
        super_admin, _ = self._create_admin('bulk_super', role='super_admin', is_superuser=True)
        self.client.force_login(super_admin)

        with patch.object(mongo_admin, 'uri', 'mongodb://fake'), patch.object(mongo_admin, 'db', return_value=fake_db):
            cm_html = self.client.get(reverse('admin_dashboard:comments')).content.decode('utf-8')
            self.assertIn('commentsBulkBar', cm_html)
            self.assertIn('comment-checkbox', cm_html)

            nf_html = self.client.get(reverse('admin_dashboard:notifications')).content.decode('utf-8')
            self.assertIn('notifBulkBar', nf_html)
            self.assertIn('noti-checkbox', nf_html)

            bulk_url = reverse('admin_dashboard:api_bulk_action')
            r_hide = self.client.post(
                bulk_url,
                data=json.dumps({'collection': 'comments', 'action': 'hide', 'ids': [str(cid1), str(cid2)]}),
                content_type='application/json',
            )
            self.assertEqual(r_hide.status_code, 200)
            self.assertEqual(r_hide.json()['count'], 2)
            self.assertTrue(fake_db.comments.find_one({'_id': cid1})['isDeleted'])

            r_read = self.client.post(
                bulk_url,
                data=json.dumps({'collection': 'notifications', 'action': 'read', 'ids': [str(nid1)]}),
                content_type='application/json',
            )
            self.assertEqual(r_read.status_code, 200)
            self.assertTrue(r_read.json()['success'])
            self.assertTrue(fake_db.notifications.find_one({'_id': nid1})['read'])
            self.assertFalse(fake_db.notifications.find_one({'_id': nid2})['read'])

            r_del = self.client.post(
                bulk_url,
                data=json.dumps({'collection': 'comments', 'action': 'delete', 'ids': [str(cid2)]}),
                content_type='application/json',
            )
            self.assertEqual(r_del.status_code, 200)
            self.assertIsNone(fake_db.comments.find_one({'_id': cid2}))

    @patch('users_management.services.public_rooms', return_value=([], None))
    @patch('users_management.views_legacy._upstash_rooms', return_value=1)
    def test_a3_audit_logs_on_room_close_and_media_delete_plus_ui_guards(self, _mock_rooms_redis, _mock_rooms):
        now = datetime.utcnow()
        uid = ObjectId()
        fake_db = FakeDatabase({
            'users': [{'_id': uid, 'name': 'UserA', 'email': 'ua@example.com', 'watchlist': [{'id': 550, 'title': 'Movie 550', 'type': 'movie'}], 'createdAt': now}],
            'watchprogresses': [{'_id': ObjectId(), 'userId': uid, 'contentId': '550', 'isTVShow': False}],
            'comments': [{'_id': ObjectId(), 'userId': uid, 'movieId': 550, 'type': 'movie', 'content': 'c'}],
            'streamhistories': [],
        })
        super_admin, _ = self._create_admin('a3_super', role='super_admin', is_superuser=True)
        self.client.force_login(super_admin)

        with patch.object(mongo_admin, 'uri', 'mongodb://fake'), patch.object(mongo_admin, 'db', return_value=fake_db):
            r_room = self.client.post(reverse('admin_dashboard:rooms'), {'action': 'delete', 'room_id': 'ROOM-XYZ1'})
            self.assertEqual(r_room.status_code, 302)
            self.assertTrue(AuditLog.objects.filter(action='close_room', target_id='ROOM-XYZ1').exists())

            r_movie = self.client.post(reverse('admin_dashboard:movies'), {'action': 'delete_media', 'media_id': '550', 'is_tv': 'false'})
            self.assertEqual(r_movie.status_code, 302)
            self.assertTrue(AuditLog.objects.filter(action='delete_movie', target_id='550').exists())

            cache_html = self.client.get(reverse('admin_dashboard:cache')).content.decode('utf-8')
            self.assertIn("requireText: 'FLUSH'", cache_html)
            users_html = self.client.get(reverse('admin_dashboard:users_list')).content.decode('utf-8')
            self.assertIn("requireText: 'DELETE'", users_html)

    @patch('users_management.services.public_rooms', return_value=([], None))
    def test_a4_admin_avatar_update_validation_and_clear(self, _mock_rooms):
        super_admin, profile = self._create_admin('avatar_admin', role='super_admin', is_superuser=True)
        self.client.force_login(super_admin)
        prof_url = reverse('admin_dashboard:admin_profile')

        # Reject javascript: scheme
        self.client.post(prof_url, {'action': 'update_profile', 'avatar': 'javascript:alert(1)'})
        profile.refresh_from_db()
        self.assertEqual(profile.avatar, '')

        # Reject oversized payload
        oversized = 'data:image/png;base64,' + ('A' * 400050)
        self.client.post(prof_url, {'action': 'update_profile', 'avatar': oversized})
        profile.refresh_from_db()
        self.assertEqual(profile.avatar, '')

        # Accept valid HTTPS avatar URL & render in topbar
        valid_url = 'https://cdn.example.com/avatar.png'
        r_ok = self.client.post(prof_url, {'action': 'update_profile', 'avatar': valid_url, 'email': 'avatar@example.com'}, follow=True)
        profile.refresh_from_db()
        self.assertEqual(profile.avatar, valid_url)
        self.assertIn(f'<img src="{valid_url}"', r_ok.content.decode('utf-8'))

        # Clear avatar
        self.client.post(prof_url, {'action': 'update_profile', 'avatar': ''})
        profile.refresh_from_db()
        self.assertEqual(profile.avatar, '')

    @override_settings(
        EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
        EMAIL_HOST='smtp.example.com',
        DEFAULT_FROM_EMAIL='noreply@example.com',
        ADMIN_REPORT_EMAIL='ops@example.com',
    )
    @patch('users_management.services.public_rooms', return_value=([], None))
    def test_c1_weekly_report_smtp_delivery_and_email_template_ui(self, _mock_rooms):
        from django.core import mail
        from users_management.views.scheduled_tasks import _run_weekly_report

        now = datetime.utcnow()
        fake_db = FakeDatabase({
            'users': [{'_id': ObjectId(), 'name': 'U1', 'email': 'u1@example.com', 'createdAt': now}],
            'comments': [],
            'streamhistories': [],
            'sessions': [],
        })
        super_admin, _ = self._create_admin('email_super', role='super_admin', is_superuser=True)
        self.client.force_login(super_admin)

        with patch.object(mongo_admin, 'uri', 'mongodb://fake'), patch.object(mongo_admin, 'db', return_value=fake_db):
            mail.outbox.clear()
            msg = _run_weekly_report()
            self.assertIn('email_sent=1', msg)
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].to, ['ops@example.com'])
            self.assertIn('Weekly Operations Report', mail.outbox[0].body)

            email_page = self.client.get(reverse('admin_dashboard:email')).content.decode('utf-8')
            self.assertIn('emailTemplateSelect', email_page)
            self.assertIn('smtp.example.com', email_page)

            mail.outbox.clear()
            self.client.post(reverse('admin_dashboard:email'), {
                'action': 'send',
                'recipient_type': 'single',
                'recipient_email': 'member@example.com',
                'subject': 'Hello',
                'body': 'Notice text',
            })
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].to, ['member@example.com'])

    @patch('users_management.webhook_dispatcher.requests.post')
    @patch('users_management.services.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))])
    def test_c2_webhook_dispatcher_hmac_slack_discord_and_ssrf_block(self, _mock_dns, mock_post):
        mock_post.return_value = MagicMock(status_code=200, text='ok')
        admin_user, _ = self._create_admin('wh_admin', role='super_admin', is_superuser=True)

        ep_std = WebhookEndpoint.objects.create(
            name='Standard HMAC',
            url='https://hooks.example.com/events',
            events=['user.banned'],
            secret='super-secret-key',
            is_active=True,
            created_by=admin_user,
        )
        ep_slack = WebhookEndpoint.objects.create(
            name='Slack Hook',
            url='https://hooks.slack.com/services/T0/B0/XX',
            events=['user.banned'],
            is_active=True,
            created_by=admin_user,
        )
        ep_discord = WebhookEndpoint.objects.create(
            name='Discord Hook',
            url='https://discord.com/api/webhooks/12345/abcdef',
            events=['user.banned'],
            is_active=True,
            created_by=admin_user,
        )
        ep_ssrf = WebhookEndpoint.objects.create(
            name='Localhost SSRF',
            url='http://127.0.0.1:8080/internal',
            events=['user.banned'],
            is_active=True,
            created_by=admin_user,
        )

        delivered = dispatch_webhook_event('user.banned', {'user_id': 'u99', 'reason': 'spam'}, sync=True)
        self.assertEqual(delivered, 4)
        self.assertEqual(mock_post.call_count, 3)  # SSRF blocked before HTTP call

        # Verify HMAC signature on standard webhook log
        log_std = WebhookLog.objects.get(endpoint=ep_std)
        self.assertEqual(log_std.response_status, 200)
        std_call = [c for c in mock_post.call_args_list if c.args[0] == ep_std.url][0]
        sig_header = std_call.kwargs['headers'].get('X-Webhook-Signature')
        expected_encoded = json.dumps(std_call.kwargs['json'], separators=(',', ':'), default=str).encode('utf-8')
        expected_sig = hmac.new(b'super-secret-key', expected_encoded, hashlib.sha256).hexdigest()
        self.assertEqual(sig_header, expected_sig)

        # Verify Slack & Discord formatting
        slack_call = [c for c in mock_post.call_args_list if 'hooks.slack.com' in c.args[0]][0]
        self.assertIn('text', slack_call.kwargs['json'])
        discord_call = [c for c in mock_post.call_args_list if 'discord.com' in c.args[0]][0]
        self.assertIn('content', discord_call.kwargs['json'])

        # Verify SSRF log error without outbound call
        log_ssrf = WebhookLog.objects.get(endpoint=ep_ssrf)
        self.assertEqual(log_ssrf.response_status, 0)
        self.assertIn('Blocked unsafe URL', log_ssrf.response_body)

        # Verify UI blocks creating SSRF webhook
        self.client.force_login(admin_user)
        self.client.post(reverse('admin_dashboard:webhooks'), {
            'action': 'create',
            'name': 'Bad Metadata Hook',
            'url': 'http://169.254.169.254/latest/meta-data/',
            'is_active': 'on',
        })
        self.assertFalse(WebhookEndpoint.objects.filter(name='Bad Metadata Hook').exists())

    @override_settings(
        ENFORCE_SUPERADMIN_2FA=True,
        ADMIN_RATE_LIMITS={'admin_sensitive': {'ip_max': 2, 'account_max': 2, 'window_seconds': 60}},
    )
    @patch('users_management.services.public_rooms', return_value=([], None))
    def test_c3_sensitive_action_rate_limit_and_superadmin_2fa_enforcement(self, _mock_rooms):
        now = datetime.utcnow()
        cid = ObjectId()
        fake_db = FakeDatabase({
            'comments': [{'_id': cid, 'content': 'hi', 'isDeleted': False, 'createdAt': now}],
        })
        super_admin, profile = self._create_admin('c3_super', role='super_admin', is_superuser=True, password='SuperPass!2026')

        # 1. Interactive login without 2FA triggers mandatory 2FA fence
        r_login = self.client.post(reverse('admin_dashboard:login'), {
            'username': 'c3_super',
            'password': 'SuperPass!2026',
        })
        self.assertRedirects(r_login, reverse('admin_dashboard:admin_profile'), fetch_redirect_response=False)
        self.assertTrue(self.client.session.get('_require_2fa_setup'))

        # Accessing dashboard or JSON APIs while fence is active is blocked
        r_dash = self.client.get(reverse('admin_dashboard:system_settings'))
        self.assertRedirects(r_dash, reverse('admin_dashboard:admin_profile'), fetch_redirect_response=False)
        r_api = self.client.post(
            reverse('admin_dashboard:api_bulk_action'),
            data=json.dumps({'collection': 'comments', 'action': 'hide', 'ids': [str(cid)]}),
            content_type='application/json',
        )
        self.assertEqual(r_api.status_code, 403)

        # 2. Complete 2FA setup clears fence & unlocks dashboard
        self.client.post(reverse('admin_dashboard:profile_2fa_setup'))
        secret = self.client.session['_2fa_setup_secret']
        r_enable = self.client.post(reverse('admin_dashboard:profile_2fa_enable'), {'otp_code': get_totp_code(secret)})
        self.assertRedirects(r_enable, reverse('admin_dashboard:admin_profile'), fetch_redirect_response=False)
        self.assertFalse(self.client.session.get('_require_2fa_setup', False))
        profile.refresh_from_db()
        self.assertTrue(profile.totp_enabled)

        # 3. Super Admin cannot disable 2FA
        self.client.post(reverse('admin_dashboard:profile_2fa_disable'), {'current_password': 'SuperPass!2026'})
        profile.refresh_from_db()
        self.assertTrue(profile.totp_enabled)

        # 4. Sensitive mutation rate limit returns 429 on 3rd attempt
        with patch.object(mongo_admin, 'uri', 'mongodb://fake'), patch.object(mongo_admin, 'db', return_value=fake_db):
            bulk_url = reverse('admin_dashboard:api_bulk_action')
            payload = json.dumps({'collection': 'comments', 'action': 'hide', 'ids': [str(cid)]})
            self.assertEqual(self.client.post(bulk_url, data=payload, content_type='application/json').status_code, 200)
            self.assertEqual(self.client.post(bulk_url, data=payload, content_type='application/json').status_code, 200)
            r_429 = self.client.post(bulk_url, data=payload, content_type='application/json')
            self.assertEqual(r_429.status_code, 429)

    @patch('users_management.services.public_rooms', return_value=([], None))
    @patch('users_management.views.monitoring._check_nodejs_health', return_value={'status': 'online', 'response_ms': 12})
    @patch('users_management.views.monitoring._check_redis_cache_health', return_value={'status': 'online', 'response_ms': 5})
    @patch('users_management.views.monitoring._check_redis_rooms_health', return_value={'status': 'online', 'response_ms': 6})
    @patch('users_management.views.monitoring._check_postgresql_health', return_value={'status': 'online', 'response_ms': 2})
    def test_c4_temp_ban_monitoring_analytics_logs_and_bell(self, _pg, _rr, _rc, _nj, _mock_rooms):
        from users_management.views.scheduled_tasks import _run_cleanup_tokens

        now = datetime.utcnow()
        uid = ObjectId()
        other_uid = ObjectId()
        fake_db = FakeDatabase({
            'users': [{
                '_id': uid, 'name': 'MetricUser', 'email': 'metric@example.com',
                'isBanned': False, 'isEmailVerified': True, 'authType': 'email',
                'watchlist': [{'id': 1, 'title': 'Demo', 'type': 'movie'}],
                'createdAt': now,
            }],
            'sessions': [
                {'_id': ObjectId(), 'userId': uid, 'device': 'Chrome', 'lastActive': now, 'expiresAt': now + timedelta(days=1)},
                {'_id': ObjectId(), 'userId': other_uid, 'device': 'Safari', 'lastActive': now, 'expiresAt': now + timedelta(days=1)},
            ],
            'comments': [{'_id': ObjectId(), 'userId': uid, 'movieId': '1', 'type': 'movie', 'content': 'nice', 'createdAt': now}],
            'watchprogresses': [{'_id': ObjectId(), 'userId': uid, 'contentId': '1', 'lastWatched': now}],
            'notifications': [],
            'streamhistories': [],
            'userreports': [],
            'passwordresettokens': [],
        })
        super_admin, _ = self._create_admin('c4_super', role='super_admin', is_superuser=True)
        self.client.force_login(super_admin)

        with patch.object(mongo_admin, 'uri', 'mongodb://fake'), patch.object(mongo_admin, 'db', return_value=fake_db), patch.object(mongo_admin, 'ping', return_value={'ok': 1}):
            # 1. Temp ban (24h) + expiration cleanup
            r_ban = self.client.post(
                reverse('admin_dashboard:api_user_ban'),
                data=json.dumps({'user_id': str(uid), 'action': 'ban', 'reason': 'Temp cooldown', 'duration_hours': 24}),
                content_type='application/json',
            )
            self.assertEqual(r_ban.status_code, 200)
            self.assertIsNotNone(r_ban.json()['banExpiresAt'])
            user_doc = fake_db.users.find_one({'_id': uid})
            self.assertTrue(user_doc['isBanned'])
            self.assertIsNotNone(user_doc['banExpiresAt'])
            self.assertEqual(fake_db.sessions.count_documents({'userId': uid}), 0)

            # Move expiry into the past and verify cleanup unbans user
            fake_db.users.update_one({'_id': uid}, {'$set': {'banExpiresAt': now - timedelta(minutes=5)}})
            cleanup_msg = _run_cleanup_tokens()
            self.assertIn('auto-unbanned 1', cleanup_msg)
            self.assertFalse(fake_db.users.find_one({'_id': uid})['isBanned'])

            # 2. Monitoring health check persists snapshot in admin_health_logs
            r_health = self.client.get(reverse('admin_dashboard:api_health_check'))
            self.assertEqual(r_health.status_code, 200)
            h_payload = r_health.json()
            self.assertTrue(h_payload['success'])
            self.assertEqual(len(h_payload['history']), 1)
            self.assertEqual(fake_db.admin_health_logs.count_documents({}), 1)

            # 3. Analytics returns KPIs (DAU/WAU/MAU/WoW) and 7x24 heatmap
            r_an = self.client.get(reverse('admin_dashboard:api_analytics') + '?refresh=1')
            self.assertEqual(r_an.status_code, 200)
            an_payload = r_an.json()
            self.assertTrue(an_payload['success'])
            self.assertEqual(an_payload['kpis']['dau'], 2)
            self.assertEqual(an_payload['kpis']['active_sessions'], 1)
            self.assertEqual(len(an_payload['heatmap']), 7)
            self.assertEqual(len(an_payload['heatmap'][0]), 24)
            mon_idx = ((((now.weekday() + 1) % 7) + 1) - 2) % 7
            self.assertGreaterEqual(an_payload['heatmap'][mon_idx][now.hour], 1)

            # 4. Audit Logs & Error Logs date filtering + trend
            AuditLog.objects.create(admin_user=super_admin, admin_username='alpha_admin', action='aa_first', target_type='test')
            ErrorLog.objects.create(level='critical', message='Database timeout occurred', request_path='/dashboard/')
            today_str = timezone.localtime(timezone.now()).strftime('%Y-%m-%d') if getattr(settings, 'USE_TZ', False) else datetime.now().strftime('%Y-%m-%d')
            r_au = self.client.get(f"{reverse('admin_dashboard:audit_logs')}?date_from={today_str}&date_to={today_str}&sort=action")
            self.assertEqual(r_au.status_code, 200)
            self.assertIn('aa_first', r_au.content.decode('utf-8'))

            r_err = self.client.get(f"{reverse('admin_dashboard:error_logs')}?date_from={today_str}&date_to={today_str}")
            self.assertEqual(r_err.status_code, 200)
            self.assertIn('7-Day Error Trend', r_err.content.decode('utf-8'))
            self.assertIn('Database timeout', r_err.content.decode('utf-8'))

            # 5. Notification Bell includes recent ErrorLog & live Watch Rooms
            cache.clear()
            with patch('users_management.services.public_rooms', return_value=([{'roomId': 'ROOM-1', 'movieTitle': 'Inception', 'createdAt': now}], None)):
                r_bell = self.client.get(reverse('admin_dashboard:api_admin_notifications') + '?refresh=1')
            self.assertEqual(r_bell.status_code, 200)
            bell_payload = r_bell.json()
            ev_types = {ev['type'] for ev in bell_payload['events']}
            self.assertIn('error', ev_types)
            self.assertIn('room', ev_types)
            self.assertTrue(all('detail' in ev for ev in bell_payload['events']))





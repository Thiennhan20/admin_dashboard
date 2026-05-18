from datetime import date, datetime

import requests
from bson import ObjectId
from bson.errors import InvalidId
from django.conf import settings
from pymongo import MongoClient
from pymongo.errors import PyMongoError


API_TIMEOUT = 8

MONGO_COLLECTIONS = {
    'users': {
        'label': 'Users',
        'sort': [('createdAt', -1)],
        'projection': {'password': 0, 'emailVerificationToken': 0},
    },
    'comments': {
        'label': 'Comments',
        'sort': [('createdAt', -1)],
    },
    'watchprogresses': {
        'label': 'Watch Progress',
        'sort': [('lastWatched', -1)],
    },
    'searchhistories': {
        'label': 'Search History',
        'sort': [('updated_at', -1)],
    },
    'notifications': {
        'label': 'Notifications',
        'sort': [('createdAt', -1)],
    },
    'passwordresettokens': {
        'label': 'Password Reset Tokens',
        'sort': [('createdAt', -1)],
        'projection': {'tokenHash': 0},
    },
    'blacklistedtokens': {
        'label': 'Blacklisted Tokens',
        'sort': [('createdAt', -1)],
        'projection': {'token': 0},
    },
}


def clean_value(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [clean_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): clean_value(item) for key, item in value.items()}
    return value


def api_get(path, params=None, timeout=API_TIMEOUT):
    url = f'{settings.API_BASE_URL}{path}'
    response = requests.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def server_health():
    try:
        response = requests.get(f'{settings.SERVER_URL}/health', timeout=API_TIMEOUT)
        return {
            'ok': response.ok,
            'status_code': response.status_code,
            'data': response.json() if response.headers.get('content-type', '').startswith('application/json') else {},
        }
    except requests.RequestException as exc:
        return {'ok': False, 'error': str(exc)}


def public_rooms():
    try:
        data = api_get('/rooms/public')
        return data.get('rooms', []), None
    except requests.RequestException as exc:
        return [], str(exc)


class MongoAdminService:
    def __init__(self):
        self.uri = settings.MONGODB_URI
        self.db_name = settings.MONGODB_NAME
        self._client = None
        self._db = None

    @property
    def enabled(self):
        return bool(self.uri)

    def db(self):
        if not self.enabled:
            raise RuntimeError('MONGODB_URI is not configured')
        if self._db is None:
            self._client = MongoClient(self.uri, serverSelectionTimeoutMS=8000)
            if self.db_name:
                self._db = self._client[self.db_name]
            else:
                self._db = self._client.get_default_database()
        return self._db

    def ping(self):
        if not self.enabled:
            return {'ok': False, 'error': 'MONGODB_URI is not configured'}
        try:
            self.db().command('ping')
            return {'ok': True}
        except Exception as exc:
            return {'ok': False, 'error': str(exc)}

    def count(self, collection, query=None):
        return self.db()[collection].count_documents(query or {})

    def list_collection(self, collection, page=1, limit=20, query=None):
        meta = MONGO_COLLECTIONS.get(collection)
        if not meta:
            raise ValueError('Unsupported collection')

        page = max(int(page or 1), 1)
        limit = min(max(int(limit or 20), 1), 100)
        skip = (page - 1) * limit
        query = query or {}

        cursor = (
            self.db()[collection]
            .find(query, meta.get('projection'))
            .sort(meta.get('sort', [('_id', -1)]))
            .skip(skip)
            .limit(limit)
        )
        rows = [clean_value(row) for row in cursor]
        total = self.count(collection, query)
        return {
            'rows': rows,
            'total': total,
            'page': page,
            'limit': limit,
            'has_more': skip + len(rows) < total,
        }

    def overview(self):
        if not self.enabled:
            return {'enabled': False, 'error': 'MONGODB_URI is not configured'}

        db = self.db()
        counts = {}
        for collection in MONGO_COLLECTIONS:
            try:
                counts[collection] = db[collection].count_documents({})
            except PyMongoError:
                counts[collection] = 0

        users = list(
            db.users.find({}, {'password': 0, 'emailVerificationToken': 0})
            .sort('createdAt', -1)
            .limit(8)
        )

        verified_users = db.users.count_documents({'isEmailVerified': True})
        unverified_users = max(counts.get('users', 0) - verified_users, 0)
        total_watchlist_items = sum(len(user.get('watchlist', [])) for user in users)

        recent_comments = list(db.comments.aggregate([
            {'$sort': {'createdAt': -1}},
            {'$limit': 10},
            {'$lookup': {
                'from': 'users',
                'localField': 'userId',
                'foreignField': '_id',
                'as': 'user',
            }},
            {'$unwind': {'path': '$user', 'preserveNullAndEmptyArrays': True}},
            {'$project': {
                'movieId': 1,
                'type': 1,
                'content': 1,
                'likes': 1,
                'isDeleted': 1,
                'createdAt': 1,
                'user.name': 1,
                'user.email': 1,
            }},
        ]))

        watch_progress = list(
            db.watchprogresses.find({})
            .sort('lastWatched', -1)
            .limit(10)
        )

        search_docs = list(
            db.searchhistories.find({})
            .sort('updated_at', -1)
            .limit(8)
        )

        notifications = list(
            db.notifications.find({})
            .sort('createdAt', -1)
            .limit(10)
        )

        return {
            'enabled': True,
            'counts': counts,
            'stats': {
                'verified_users': verified_users,
                'unverified_users': unverified_users,
                'total_watchlist_items_recent_users': total_watchlist_items,
            },
            'recent_users': clean_value(users),
            'recent_comments': clean_value(recent_comments),
            'watch_progress': clean_value(watch_progress),
            'search_histories': clean_value(search_docs),
            'notifications': clean_value(notifications),
        }

    def object_id(self, value):
        try:
            return ObjectId(value)
        except (InvalidId, TypeError):
            raise ValueError('Invalid object id')

    def delete_document(self, collection, document_id):
        if collection not in MONGO_COLLECTIONS or collection == 'users':
            raise ValueError('Unsupported delete target')

        oid = self.object_id(document_id)
        db = self.db()

        if collection == 'comments':
            comment = db.comments.find_one({'_id': oid})
            if not comment:
                return 0

            ids = [oid]
            if not comment.get('parentId'):
                replies = list(db.comments.find({'parentId': oid}, {'_id': 1}))
                ids.extend(reply['_id'] for reply in replies)
                db.comments.delete_many({'parentId': oid})
            else:
                db.comments.update_one(
                    {'_id': comment.get('parentId')},
                    {'$pull': {'replies': oid}},
                )
            db.notifications.delete_many({
                '$or': [
                    {'metadata.commentId': {'$in': ids}},
                    {'metadata.parentCommentId': {'$in': ids}},
                ]
            })
            result = db.comments.delete_one({'_id': oid})
            return result.deleted_count

        result = db[collection].delete_one({'_id': oid})
        return result.deleted_count

    def set_comment_deleted(self, comment_id, is_deleted=True):
        oid = self.object_id(comment_id)
        result = self.db().comments.update_one(
            {'_id': oid},
            {'$set': {'isDeleted': bool(is_deleted)}},
        )
        return result.modified_count

    def set_notification_read(self, notification_id, read=True):
        oid = self.object_id(notification_id)
        update = {'read': bool(read), 'readAt': datetime.utcnow() if read else None}
        result = self.db().notifications.update_one({'_id': oid}, {'$set': update})
        return result.modified_count


mongo_admin = MongoAdminService()

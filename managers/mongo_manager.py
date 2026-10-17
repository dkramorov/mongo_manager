import logging
import os
import pymongo
from bson.objectid import ObjectId
from managers.simple_logger import logger


logging.getLogger('pymongo').setLevel(logging.INFO)


class MockSettings:
    FULL_SETTINGS_SET = os.environ


try:
    from django.conf import settings
    hasattr(settings, 'DEBUG')
except Exception as e:
    #logger.info('Exception from django.conf import settings: %s' % e)
    settings = MockSettings()


def get_mongo_configs() -> dict:
    """Получение конфигураций монго из настроек
       Плохие алиасы, для которых не хватает данных будут пропущены
    """
    mongo_configs = {}
    search_patterns = (
        '_MONGO_URL',
        '_MONGO_DB',
    )
    def get_pattern_key(pattern: str):
        """Получение ключа настроек, например, _MONGO_URL => url
        """
        return pattern.split('_MONGO_')[-1].lower()

    for key, value in settings.FULL_SETTINGS_SET.items():
        if '_MONGO_' in key:
            search_pattern = [item for item in search_patterns if item in key]
            if not search_pattern:
                continue
            search_pattern = search_pattern[0]
            alias = key.split(search_pattern)[0].lower()
            if not alias in mongo_configs:
                mongo_configs[alias] = {}
            param_key = get_pattern_key(pattern=search_pattern)
            mongo_configs[alias][param_key] = value
    # Валидация, что всех данных хватает для алиаса
    bad_aliases = []
    for alias, data in mongo_configs.items():
        for search_pattern in search_patterns:
            key = get_pattern_key(pattern=search_pattern)
            if not key in data:
                bad_aliases.append(alias)
    if bad_aliases:
        logger.info('BAD MONGO ALIASES: %s' % bad_aliases)
    return {k: v for k, v in mongo_configs.items() if k not in bad_aliases}


def get_mongo_managers() -> dict:
    """Создать монго менеджеры для работы с монго
    """
    mongo_managers = {}
    mongo_configs = get_mongo_configs()
    for alias, config in mongo_configs.items():
        mongo_managers[alias] = MongoManager(
            url=config['url'],
            db=config['db'],
        )
    return mongo_managers


class MongoManager:
    """Монго менеджер
    """

    def __init__(self,
                 url: str = None,
                 db: str = 'heroku_ts1pfnh0',
                 collection: str = 'records'):
        if not url:
            url = settings.MONGO_BASE if hasattr(settings, 'MONGO_BASE') else None
        self.url = url
        if not self.url:
            raise Exception('MONGO_BASE url not set')
        self.client = pymongo.MongoClient(self.url)
        self.db = self.client[db]
        self.collection = self.db[collection]

    def set_collection(self, collection: str):
        """Задать коллекцию
           :param collection: коллекция
        """
        self.collection = self.db[collection]

    def find_all(self):
        """Найти все записи"""
        return self.collection.find({})

    def total_documents(self):
        """Количество документов по запросу
        """
        return self.collection.estimated_document_count()

    def count(self, cursor):
        """Получаем кол-во записей по запросу/курсору
           :param cursor: курсор
        """
        return len(list(cursor.clone()))

    def documents_count(self,
                        query: dict,
                        limit: int = 1000000,
                        projection: str = None,
                        sort: str = None,
                        hint: str = None) -> dict:
        """Получить количество записей по запросу
           skip слишком долго работает, поэтому проще
           запрашивать идентификаторы в цикле и считать их
           :param query: запрос (например, {} для всех документов)
           :param limit: ограничение по кол-ву
           :param hint: подсказка для поиска по индексу
           :param projection: извлекаемые поля
           :param sort: сортировка
           :return: словарь с количеством и аккумуляцией срезов айди
        """
        accum = [] # накопительный результат для нарезки на массивы айдишников
        count = 0
        lastpk = None
        if not sort:
            sort = {'_id': 1}
        if not projection:
            projection = {'_id': 1}
        while True:
            new_query = {k: v for k, v in query.items()}
            if lastpk:
                query['_id'] = {'$gt': lastpk}
            docs = self.find(
                cond = query,
                projection = projection,
                sort = sort,
                limit = limit,
                hint = hint,
            )
            docs = list(docs)
            if not docs:
                break
            count += len(docs)
            prevpk = lastpk
            lastpk = docs[-1]['_id']
            if prevpk and lastpk:
                accum.append({'from': prevpk, 'to': lastpk})
            logger.info('query: %s, lastpk: %s, count: %s' % (query, lastpk, count))
        #logger.info(accum)
        return {
            'count': count,
            'accum': accum,
        }

    def find_one(self, obj_id: str):
        """Найти запись
           :param obj_id: ид объекта
           :return: dict
        """
        return self.collection.find_one({
            '_id': ObjectId(obj_id),
        })

    def find(self,
             cond: dict,
             projection: dict = None,
             skip: int = None, # медленный очень
             sort: dict = None,
             limit: int = None,
             hint: str = None):
        """Найти записи
           :param cond: условие поиска
           :param projection: словарь с полями, которые хотим вытаскивать
           :param skip: количество, которое надо пропустить
           :param sort: сортировка по переданному словарю, например { _id: -1 }
           :param limit: ограничение по количеству
           :param hint: подсказка по индексу для поиска
           :return: cursor
        """
        cursor = self.collection.find(cond, projection=projection)
        if skip:
            cursor = cursor.skip(skip)
        if sort:
            cursor = cursor.sort(sort)
        if limit:
            cursor = cursor.limit(limit)
        if hint:
            cursor = cursor.hint(hint)
        return cursor

    def update_many(self, cond: dict, data: dict):
        """Обновить записи
           :param cond: условие для обновления
           :param data: новые данные
        """
        if not cond or not data:
            logger.info('[ERROR]: update_many => not enough data: cond=%s, data=%s' % (cond, data))
            return
        return self.collection.update_many(cond, {'$set': data})

    def update_one(self, obj_id: str, data: dict):
        """Обновить запись
           :param obj_id: ид объекта
           :param data: новые данные
        """
        if not obj_id or not data:
            logger.info('[ERROR]: update_one => not enough data: obj_id=%s, data=%s' % (obj_id, data))
            return
        return self.collection.update_one({
            '_id': ObjectId(obj_id),
        }, {'$set': data})

    def unset_many(self, cond: dict, fields: list):
        """Удалить поля
           :param cond: условие для обновления
           :param fields: поля, которые хотим удалить
        """
        if not cond or not fields:
            logger.info('[ERROR]: unset_many => not enough data: cond=%s, fields=%s' % (cond, fields))
            return
        return self.collection.update_many(cond, {'$unset': {field: 1 for field in fields}})

    def unset_one(self, obj_id: str, fields: list):
        """Удалить поля
           :param obj_id: ид объекта
           :param fields: поля, которые хотим удалить
        """
        if not obj_id or not fields:
            logger.info('[ERROR]: unset_one => not enough data: obj_id=%s, fields=%s' % (obj_id, fields))
            return
        return self.collection.update_one({
          '_id': ObjectId(obj_id),
        }, {'$unset': {field: 1 for field in fields}})

    def delete_one(self, obj_id: str):
        """Удаление записи
           :param obj_id: ид объекта
        """
        return self.collection.delete_one({'_id': ObjectId(obj_id)})

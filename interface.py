"""Owner appearance, custom emoji and chat discovery."""

import os, re, unicodedata
import json
from datetime import datetime, timezone, timedelta
from collections import OrderedDict
from html import escape as _html_escape, unescape
from aiogram.exceptions import TelegramBadRequest
import logging
from string import Formatter
from collections import Counter
from html.parser import HTMLParser
from html import escape as e
from aiogram import F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import asyncio, secrets
from aiogram import BaseMiddleware
from aiogram.types import KeyboardButton, KeyboardButtonRequestChat, ReplyKeyboardMarkup, ReplyKeyboardRemove



# Переводим отображаемые значения, сохраняя коды базы и Telegram API.
RUSSIAN = {
    'mode': {'observation':'Наблюдение','confirmation':'Подтверждение администратором','automatic':'Автоматическая модерация'},
    'status': {'pending':'Ожидает проверки','processing':'Обрабатывается','accepted':'Подтверждено','rejected':'Отклонено',
        'automatic':'Применено автоматически','uncertain':'Нужно проверить результат','passed':'Нарушений не найдено',
        'approve':'Обжалование принято','reject':'Обжалование отклонено'},
    'action': {'none':'Без наказания','pass':'Пропустить','observe':'Наблюдать','review':'Проверить вручную',
        'warn':'Предупреждение','mute':'Ограничение отправки сообщений','ban':'Блокировка','lift':'Снятие ограничения',
        'unmute':'Снятие ограничения','reset':'Сброс предупреждений'},
    'category': {'none':'Нет нарушения','clean':'Нет нарушения','discussion':'Обсуждение или цитата',
        'profanity':'Нецензурная лексика','personal_insult':'Личное оскорбление','insult':'Личное оскорбление',
        'threat':'Угроза','spam':'Спам','other':'Другое нарушение'},
    'member': {'creator':'Владелец чата','administrator':'Администратор','member':'Участник',
        'restricted':'Ограничен','kicked':'Заблокирован','left':'Покинул чат'},
    'style': {'original':'Исходный / наследовать','neutral':'Обычный','primary':'Основной','success':'Успех','danger':'Опасность'},
    'columns': {'original':'Исходная раскладка','1':'По одной','2':'По две','3':'По три'},
    'audit': {'mode':'Изменён режим модерации','warn':'Выдано предупреждение','mute':'Выдано ограничение',
        'ban':'Участник заблокирован','lift':'Ограничение снято','accepted':'Решение подтверждено',
        'rejected':'Решение отклонено','uncertain':'Требуется проверка результата',
        'appeal_approve':'Обжалование принято','appeal_reject':'Обжалование отклонено'},
    'model': {'local-rules-2.0':'Локальные правила','local-fallback-2.0':'Локальные правила (ИИ недоступен)'}
}

def ru(kind,value):
    value=str(value)
    if kind=='audit' and value.startswith('manual_'):
        return 'Действие администратора: '+ru('action',value[7:]).lower()
    if kind=='model':return RUSSIAN[kind].get(value,'Внешняя модель · '+value)
    return RUSSIAN.get(kind,{}).get(value,{'category':'Другое нарушение','audit':'Другое действие'}.get(kind,'Неизвестное значение'))

def ru_date(value):
    if not value:return 'не указано'
    try:
        stamp=value if isinstance(value,datetime) else datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M мск')
    except (ValueError,TypeError):return 'не указано'

def ru_restriction(row):
    if not row:return 'нет'
    return ru('action',row[0])+(' · до '+ru_date(row[1]) if row[1] else ' · бессрочно')

def ru_context(value):
    if not value:return 'не сохранён'
    try:
        items=json.loads(value)
        if not isinstance(items,list):return 'недоступен'
        lines=[]
        for item in items:
            if not isinstance(item,dict):continue
            if 'reply' in item:lines.append('Ответ на сообщение: '+str(item['reply']))
            elif 'text' in item:lines.append('Участник '+str(item.get('user_id','—'))+': '+str(item['text']))
        return '\n'.join(lines) or 'не сохранён'
    except (ValueError,TypeError):return 'недоступен'

def user_error(exc):
    """Не показываем пользователю необработанные ошибки сторонних библиотек."""
    value=str(exc)
    if isinstance(exc,ValueError) and re.search('[А-Яа-яЁё]',value):return value
    lowered=value.lower()
    for fragment,message in (
        ('not enough rights','Боту не хватает прав администратора.'),
        ('chat not found','Чат не найден или недоступен боту.'),
        ('user not found','Участник не найден.'),
        ('user not a participant','Участник не состоит в чате.'),
        ("can't parse entities",'Проверьте разметку текста.'),
        ('message is too long','Текст слишком длинный. Сократите его.'),
        ('button_data_invalid','Не удалось обработать кнопку. Откройте панель заново.'),
        ('forbidden','Боту запрещён доступ. Проверьте его права.'),
        ('retry after','Слишком много запросов. Повторите действие немного позже.')
    ):
        if fragment in lowered:return message
    return 'Проверьте ввод, права бота и доступность Telegram, затем повторите действие.'

ALTERNATIVES = OrderedDict()
TAG = re.compile('<tg-emoji\\s+emoji-id=[\\"\\x27]([1-9][0-9]{0,19})[\\"\\x27]\\s*>([^<>]+)</tg-emoji>')

def enabled():
    return os.getenv('CUSTOM_EMOJI_ENABLED', '1').lower() not in {'0', 'false', 'no'}

def valid_id(value):
    return isinstance(value, str) and re.fullmatch('[1-9][0-9]{0,19}', value) is not None and (int(value) < 2 ** 64)

def from_message(message):
    text = message.text or ''
    raw = text.encode('utf-16-le')
    spans = []
    for entity in getattr(message, 'entities', None) or []:
        if entity.type != 'custom_emoji':
            continue
        if not valid_id(entity.custom_emoji_id):
            raise ValueError('Некорректный идентификатор премиум-эмодзи.')
        start, end = (entity.offset * 2, (entity.offset + entity.length) * 2)
        if start < 0 or end > len(raw) or start >= end:
            raise ValueError('Повреждённые границы эмодзи.')
        try:
            alt = raw[start:end].decode('utf-16-le')
        except UnicodeDecodeError as exc:
            raise ValueError('Повреждённая позиция эмодзи.') from exc
        spans.append((start, end, entity.custom_emoji_id, alt))
    spans.sort()
    previous = 0
    parts = []
    for start, end, identifier, alt in spans:
        if start < previous:
            raise ValueError('Эмодзи пересекаются.')
        parts.append(raw[previous:start].decode('utf-16-le'))
        parts.append(f'<tg-emoji emoji-id="{identifier}">{_html_escape(alt)}</tg-emoji>')
        previous = end
    parts.append(raw[previous:].decode('utf-16-le'))
    return ''.join(parts)

def plain(text):
    return TAG.sub(lambda m: m[2], str(text))

def button_parts(label, use_custom=None):
    value = str(label)
    matches = list(TAG.finditer(value))
    if not matches:
        return (value, None)
    use_custom = enabled() if use_custom is None else use_custom
    if not use_custom:
        return (unescape(plain(value)), None)
    first = matches[0]
    ALTERNATIVES[first[1]] = unescape(first[2])
    ALTERNATIVES.move_to_end(first[1])
    while len(ALTERNATIVES) > 512:
        ALTERNATIVES.popitem(last=False)
    return ((value[:first.start()] + value[first.end():]).strip(), first[1])

def safe_markup(markup):
    if markup is None:
        return None
    key = 'inline_keyboard' if hasattr(markup, 'inline_keyboard') else 'keyboard' if hasattr(markup, 'keyboard') else None
    if not key:
        return markup
    rows = []
    for row in getattr(markup, key):
        safe_row = []
        for button in row:
            if isinstance(button, str) or not getattr(button, 'icon_custom_emoji_id', None):
                safe_row.append(button)
                continue
            alt = ALTERNATIVES.get(button.icon_custom_emoji_id, '')
            safe_row.append(button.model_copy(update={'icon_custom_emoji_id': None, 'text': (alt + ' ' + button.text).strip()}))
        rows.append(safe_row)
    return markup.model_copy(update={key: rows})

def fallback(method):
    updates = {}
    if hasattr(method, 'text') and method.text is not None:
        value = plain(method.text)
        if value != method.text:
            updates['text'] = value
    if hasattr(method, 'reply_markup') and method.reply_markup is not None:
        safe = safe_markup(method.reply_markup)
        if safe != method.reply_markup:
            updates['reply_markup'] = safe
    return method.model_copy(update=updates) if updates else method

class EmojiFallback:

    async def __call__(self, make_request, bot, method):
        outgoing = method if enabled() else fallback(method)
        try:
            return await make_request(bot, outgoing)
        except TelegramBadRequest as exc:
            reason = str(exc).lower()
            if not any((word in reason for word in ('custom emoji', 'custom_emoji', 'emoji_id', 'emoji-id', 'premium', 'button_icon', 'emoji_invalid'))):
                raise
            safe = fallback(outgoing)
            if safe is outgoing:
                raise
            return await make_request(bot, safe)

def validate_tags(value):
    matches = list(TAG.finditer(value))
    if len(re.findall('<\\s*tg-emoji\\b', value, re.I)) != len(matches):
        raise ValueError('Используйте <tg-emoji emoji-id="число">⭐</tg-emoji> с обычным эмодзи внутри.')
    for match in matches:
        alt = unescape(match[2])
        if not valid_id(match[1]) or not any((unicodedata.category(c) == 'So' or c == '⃣' for c in alt)):
            raise ValueError('Проверьте идентификатор и обычный эмодзи внутри tg-emoji.')
        if '{' in alt or '}' in alt or len(alt) > 32:
            raise ValueError('Альтернатива премиум-эмодзи должна быть обычным эмодзи.')

CATALOG = {'c1ff8780621d': {'default': '‹ Назад', 'kind': 'button'},
 'c20945ceeebc': {'default': 'Главное меню', 'kind': 'button'},
 '1f690ad82730': {'default': 'Пользователь', 'kind': 'text'},
 '0c3eb76aa6af': {'default': 'Этот участник недоступен для ограничения.', 'kind': 'text'},
 'c4d4ae148682': {'default': 'Мут доступен в супергруппе обсуждений канала.', 'kind': 'text'},
 '28729c6f31dd': {'default': '⚖️ Обжаловать в ЛС', 'kind': 'button'},
 '6efdb3b3b163': {'default': '<b>◈ Модератор</b>\n\nЧаты, участники и обжалования — в одной панели.\n\n',
                  'kind': 'text'},
 '9ec4593f2aa5': {'default': 'Выберите чат для управления.', 'kind': 'text'},
 'cdfd00e80265': {'default': 'Пока нет подключённых чатов. Добавьте бота и себя в администраторы группы '
                             'обсуждений.',
                  'kind': 'text'},
 'c2b3f927088e': {'default': '‹', 'kind': 'button'},
 '53039c3d2d29': {'default': '›', 'kind': 'button'},
 'fec2b2edfb22': {'default': '＋ Подключить чат', 'kind': 'button'},
 '7e8aac83b310': {'default': 'Как это работает', 'kind': 'button'},
 '808970003ae5': {'default': 'Обновить', 'kind': 'button'},
 'd607794aa65c': {'default': 'включён', 'kind': 'text'},
 '59e61d989724': {'default': 'выключен', 'kind': 'text'},
 '7460d3ea71e0': {'default': '<b>◈ {v0}</b>\n'
                             '\n'
                             'Режим  ·  {v1}\n'
                             'Фильтр  ·  {v2}\n'
                             'Предупреждения  ·  {v3}\n'
                             'Мут  ·  {v4} мин.\n'
                             'Обжалования  ·  {v5}',
                  'kind': 'text'},
 'cb6f91a5c8bf': {'default': 'Приостановить фильтр', 'kind': 'button'},
 '64c59b4c5438': {'default': 'Включить фильтр', 'kind': 'button'},
 '348ce92dffca': {'default': 'Предупреждения', 'kind': 'button'},
 '653d48f72af6': {'default': 'Время мута', 'kind': 'button'},
 '9bc0685f61f5': {'default': 'Участники', 'kind': 'button'},
 '29adc9309c43': {'default': 'Нарушения', 'kind': 'button'},
 '79b5ce112c02': {'default': 'ИИ · Режим работы', 'kind': 'button'},
 'd2b8c03ef0d8': {'default': 'На проверке', 'kind': 'button'},
 '53d92a6a5b01': {'default': 'История решений', 'kind': 'button'},
 'cd59f9bfc7a8': {'default': 'Обжалования · {v0}', 'kind': 'button'},
 '23e67a4e5347': {'default': '‹ Все чаты', 'kind': 'button'},
 '3dbcfa050a2b': {'default': 'ограничен', 'kind': 'text'},
 'ccd79e755b49': {'default': 'участник', 'kind': 'text'},
 'b263ebf9e181': {'default': 'администратор', 'kind': 'text'},
 '2cccf1cf398d': {'default': 'владелец', 'kind': 'text'},
 '030a684d5681': {'default': 'вышел', 'kind': 'text'},
 '964466896474': {'default': 'заблокирован', 'kind': 'text'},
 'b6dfca22b845': {'default': '<b>Участник</b>\n'
                             '\n'
                             '{v0}\n'
                             'идентификатор  ·  <code>{v1}</code>\n'
                             'Предупреждения  ·  {v2}\n'
                             'Статус  ·  {v3}',
                  'kind': 'text'},
 '1886d473bfc1': {'default': 'Выдать мут', 'kind': 'button'},
 '1e36aa6a752b': {'default': 'Снять мут', 'kind': 'button'},
 '9edd61654e61': {'default': 'Сбросить предупреждения', 'kind': 'button'},
 '15d4c76ee0dc': {'default': '<b>Обжалования</b>\n\n', 'kind': 'text'},
 '08daed33c25d': {'default': 'Ожидают решения: {v0} · Страница {v1}\nВыберите заявку.', 'kind': 'text'},
 'a32d41f74d27': {'default': 'Все заявки обработаны.', 'kind': 'text'},
 '3800e260501a': {'default': 'Принять', 'kind': 'button'},
 '1c7e1b622835': {'default': 'Отклонить', 'kind': 'button'},
 'c85e45cf0eff': {'default': '‹ К заявкам', 'kind': 'button'},
 '84f177f5098a': {'default': '<b>Обжалование</b>\n\n{v0}\n{v1} · <code>{v2}</code>\n\n{v3}', 'kind': 'text'},
 'e8ba749a717e': {'default': 'В этом чате у тебя нет активного мута.', 'kind': 'text'},
 '32f03e7696a7': {'default': 'Твоя заявка уже ожидает решения.', 'kind': 'text'},
 'd15ab719bf8e': {'default': '<b>Обжаловать мут</b>\n'
                             '\n'
                             'Напишите причину одним сообщением, до 2000 символов. Её увидят только '
                             'администраторы в ЛС бота.',
                  'kind': 'text'},
 '1c17b29ec9bd': {'default': 'Отмена', 'kind': 'button'},
 '942bcaae65d6': {'default': 'Панель управления находится в ЛС бота.', 'kind': 'text'},
 'b02518e08c3d': {'default': 'Открыть панель в ЛС', 'kind': 'button'},
 'd5042e53e2b8': {'default': 'Не удалось проверить мут. Проверь, что бот остался администратором чата.',
                  'kind': 'text'},
 '9adae3b5a152': {'default': 'Укажи /connect -1001234567890. Ты и бот должны быть администраторами этого '
                             'чата.',
                  'kind': 'text'},
 '3bc5bf2f8837': {'default': 'Панель доступна только в ЛС.', 'kind': 'text'},
 '08507f3dffbc': {'default': '<b>Как это работает</b>\n'
                             '\n'
                             'Выберите чат, настройте фильтр и управляйте участниками. Все разделы '
                             'открываются в этой карточке.\n'
                             '\n'
                             'Боту нужны права удалять сообщения и ограничивать участников в группе '
                             'обсуждений. Обжалования доступны только в личных сообщениях.',
                  'kind': 'text'},
 'b3ff4a18f846': {'default': '‹ Главное меню', 'kind': 'button'},
 '711486dc5d9a': {'default': '<b>Подключить чат</b>\n'
                             '\n'
                             'Отправьте идентификатор группы обсуждений, например '
                             '<code>-1001234567890</code>. Вы и бот должны быть её администраторами.',
                  'kind': 'text'},
 '4b11439aefa6': {'default': 'Нет прав администратора.', 'kind': 'text'},
 'bd6d444a9285': {'default': 'Введи лимит предупреждений от 1 до 100.', 'kind': 'text'},
 '6cf83ed60c27': {'default': 'Введи длительность мута от 1 до 10080 минут.', 'kind': 'text'},
 'f3e684a99571': {'default': 'Введи числовой идентификатор участника или имя пользователя с @ (бот должен ранее видеть '
                             'его сообщения).',
                  'kind': 'text'},
 'e88d922a436a': {'default': 'идентификатор {v0} · {v1}', 'kind': 'button'},
 '0889085cbdb8': {'default': '<b>Нарушения</b>\n\n', 'kind': 'text'},
 '77211aeb983d': {'default': 'Последние 30 участников. Выберите карточку.', 'kind': 'text'},
 '07a82705517b': {'default': 'Активных предупреждений нет.', 'kind': 'text'},
 '310774d47c56': {'default': 'Заявка больше недоступна.', 'kind': 'text'},
 '2b9c2552b96a': {'default': '<b>Выдать мут?</b>\n\nУчастник <code>{v0}</code> не сможет писать {v1} мин.',
                  'kind': 'text'},
 'd3b8348beddf': {'default': 'Подтвердить', 'kind': 'button'},
 '44a9385dcf96': {'default': 'Мут выдан.', 'kind': 'text'},
 'd0b3378cfcaf': {'default': 'Мут снят, предупреждения сброшены.', 'kind': 'text'},
 '407755fe0f23': {'default': 'Предупреждения сброшены.', 'kind': 'text'},
 '73d87f95e939': {'default': 'Не удалось выполнить действие. Проверьте права бота и идентификатор участника.',
                  'kind': 'text'},
 '90140e43e069': {'default': 'Решения доступны только в ЛС.', 'kind': 'text'},
 '6e85116e3848': {'default': 'Заявка уже обработана или недействительна.', 'kind': 'text'},
 'a52b3cfa2a8a': {'default': 'Заявка уже обрабатывается.', 'kind': 'text'},
 '1894ed4325f8': {'default': 'Обрабатываю…', 'kind': 'text'},
 'f0cfe54b3437': {'default': 'Не удалось снять мут. Заявка сохранена, можно повторить.', 'kind': 'text'},
 '5bac65cb07e1': {'default': 'Повторить', 'kind': 'button'},
 '180473624e22': {'default': 'К заявкам', 'kind': 'button'},
 '2f8bdd486f10': {'default': '✅ Обжалование принято. Мут снят, предупреждения сброшены.', 'kind': 'text'},
 '74f75b558dcd': {'default': '❌ Обжалование отклонено.', 'kind': 'text'},
 'b5e3c85f7820': {'default': 'Открой ЛС бота через новую кнопку обжалования.', 'kind': 'text'},
 '61151ccc74fc': {'default': 'Старая заявка недействительна. Подай обжалование через ЛС бота.',
                  'kind': 'text'},
 '2f2c31d1c67b': {'default': 'Первые {v0} нарушения — предупреждения, затем мут на {v1} мин. Сброс после '
                             '{v2} дней без нарушений. Твой счётчик: {v3}. Обжалование — через ЛС бота.',
                  'kind': 'text'},
 '4266f1fc07d3': {'default': 'Ответь на сообщение участника этой командой. Управление в ЛС: /panel',
                  'kind': 'text'},
 '75fe50ac9385': {'default': '🔇 Участник идентификатор {v0} получил мут на {v1} мин.', 'kind': 'text'},
 '68e5811e6d09': {'default': 'Не удалось выполнить действие. Проверь участника, число минут (1–10080) и '
                             'права бота.',
                  'kind': 'text'},
 '247ced0bfdb7': {'default': 'Отправь текст или /cancel.', 'kind': 'text'},
 'b2de4e1f01d3': {'default': 'Сократи причину до 2000 символов.', 'kind': 'text'},
 '473201abe877': {'default': 'Мут уже не активен. Отмена: /cancel', 'kind': 'text'},
 'b7599b32dd9d': {'default': 'Заявка уже ожидает решения.', 'kind': 'text'},
 '1cc8c75acc70': {'default': 'Новое обжалование', 'kind': 'text'},
 '86e1ebfed67d': {'default': 'Открыть заявку', 'kind': 'button'},
 '222ec2fbdc5b': {'default': 'Заявка отправлена администраторам в ЛС.', 'kind': 'text'},
 'e190f23ee535': {'default': 'Заявка сохранена в панели. Администраторы увидят её, когда откроют раздел '
                             '«Обжалования» в ЛС бота.',
                  'kind': 'text'},
 '5e652fc85ecb': {'default': '<b>Проверьте ввод</b>\n'
                             '\n'
                             'Введите корректное значение. Для подключения чата вы и бот должны быть '
                             'администраторами.',
                  'kind': 'text'},
 '88c664e96810': {'default': 'Не удалось выполнить действие. Проверьте права бота и повторите ввод.',
                  'kind': 'text'},
 '7270592a02b0': {'default': ' · до {v0}', 'kind': 'text'},
 'ab687d0411e2': {'default': 'Telegram временно недоступен', 'kind': 'text'},
 '99272dc0500f': {'default': 'Неизвестный участник', 'kind': 'text'},
 '3adae8ec04f0': {'default': 'Без имени', 'kind': 'text'},
 '99f53adc19e3': {'default': 'до обновления, дата неизвестна', 'kind': 'text'},
 'd5330ae677c2': {'default': '<b>Участник · {v0}</b>\n'
                             'идентификатор · <code>{v1}</code>\n'
                             '@{v2}\n'
                             'Первое появление · {v3}\n'
                             'Текущие предупреждения · {v4}\n'
                             'За всё время с обновления: предупреждения {v5}, муты {v6}, баны {v7}\n'
                             'Telegram · {v8}\n',
                  'kind': 'text'},
 '523317637123': {'default': 'Последнее ограничение · {v0}\n', 'kind': 'text'},
 '97325da0acb0': {'default': 'нет', 'kind': 'text'},
 '6a99174a50be': {'default': 'Последние обжалования · {v0}\n{v1}', 'kind': 'text'},
 'c230407832cb': {'default': 'Предупредить', 'kind': 'button'},
 '59160acbdde5': {'default': 'Мут', 'kind': 'button'},
 '345ec8af5053': {'default': 'Бан', 'kind': 'button'},
 '080282c7472c': {'default': 'Снять наказание', 'kind': 'button'},
 '64e74dcb3055': {'default': 'История участника', 'kind': 'button'},
 '14e200495a33': {'default': '{v0} · {v1} · {v2}', 'kind': 'button'},
 '178a66d7e238': {'default': '‹ Назад', 'kind': 'text'},
 '9ee3b1a0ee74': {'default': 'Проверка решений', 'kind': 'text'},
 'cedce6596fc5': {'default': 'История решений', 'kind': 'text'},
 'f337f839c924': {'default': '<b>{v0}</b> · {v1}\nСтраница {v2}', 'kind': 'text'},
 '9c7026b1c51f': {'default': '\n\nПоследние действия:\n', 'kind': 'text'},
 'ddc9218f5195': {'default': 'система', 'kind': 'text'},
 'd6faf5261ecb': {'default': 'не применяется (локальное правило)', 'kind': 'text'},
 'ae794cb3939c': {'default': '{v0} — оценка модели, не вероятность', 'kind': 'text'},
 'e6edf5c21173': {'default': 'текст удалён по сроку хранения', 'kind': 'text'},
 'd54679d35c5f': {'default': '<b>Анализ #{v0}</b>\n'
                             '{v1}\n'
                             'Участник · {v2}\n'
                             'Категория · {v3}\n'
                             'Уверенность · {v4}\n'
                             'Режим · {v5}\n'
                             'Проверка · {v6}\n'
                             'Действие · {v7}\n'
                             'Рекомендация · {v8}\n'
                             'Правила · {v9} / {v10}\n'
                             '\n'
                             '{v11}\n'
                             '\n'
                             'Сообщение: {v12}\n'
                             'Фрагмент: {v13}\n'
                             'Контекст: {v14}\n'
                             '{v15}',
                  'kind': 'text'},
 '45641fc7728e': {'default': 'Карточка участника', 'kind': 'button'},
 'b120f16e3ee0': {'default': '‹ Решения', 'kind': 'button'},
 '2a84701915a6': {'default': '<b>ИИ-модерация</b>\nРежим · {v0}\n', 'kind': 'text'},
 '8d2a1c255d81': {'default': 'ИИ подключён', 'kind': 'text'},
 '0c8c6ef420ce': {'default': 'ИИ не настроен · работают локальные правила', 'kind': 'text'},
 '4ff051e19fa7': {'default': '\n'
                             '\n'
                             'Наблюдение: без удаления и санкций.\n'
                             'Подтверждение: санкция после решения администратора.\n'
                             'Автоматически: спорные случаи остаются на проверке.\n'
                             '\n'
                             'Проверено администраторами (подтверждено / отклонено):\n',
                  'kind': 'text'},
 'ffd6d9a8a375': {'default': '{v0}: {v1} / {v2} · отклонено {v3}', 'kind': 'text'},
 'd9fdc0529c76': {'default': 'Пока нет оценок.', 'kind': 'text'},
 '4ef07b9b5fe3': {'default': 'Наблюдение', 'kind': 'button'},
 '31263102c71e': {'default': 'Подтверждение', 'kind': 'button'},
 '52e45e66caef': {'default': 'Автоматически', 'kind': 'button'},
 '6b3405d2431c': {'default': 'Проверить решения', 'kind': 'button'},
 'e67387537361': {'default': 'Только в ЛС.', 'kind': 'text'},
 '7bab69d58743': {'default': '<b>Включить автоматическую модерацию?</b>\n'
                             'Однозначные локальные нарушения и уверенные рекомендации ИИ могут привести к '
                             'удалению сообщения и санкции.',
                  'kind': 'text'},
 '831245d46a32': {'default': 'Включить', 'kind': 'button'},
 'bee1f7f9cdbd': {'default': '<b>Подтвердить санкцию?</b>\n'
                             'Участник {v0}. Предупреждение или мут по лимиту; рекомендация «ограничение '
                             'отправки» означает ограничение отправки сообщений. Сообщение будет удалено.',
                  'kind': 'text'},
 '6f7b255f079f': {'default': 'Применить', 'kind': 'button'},
 '334be61abf66': {'default': 'Уже обработано.', 'kind': 'text'},
 '0f95f1550f86': {'default': 'Если результат требует проверки, проверьте статус участника в Telegram. '
                             'Автоматический повтор запрещён.',
                  'kind': 'text'},
 '3a8adb1ea710': {'default': '<b>Подтвердить действие {v0}?</b>\nУчастник {v1}', 'kind': 'text'},
 '24fae79d5eaa': {'default': 'Действие выполнено.', 'kind': 'text'},
 '37d72677ede0': {'default': 'Не удалось выполнить действие. Проверьте права и статус участника.',
                  'kind': 'text'},
 'f3392a2adf74': {'default': 'Модерация: {v0} · {v1} · {v2}', 'kind': 'text'},
 'f087a79d4b07': {'default': '<b>Есть решение на проверку</b>\nОткройте анализ сообщения в этой панели.',
                  'kind': 'text'},
 'b2f83f2a3d9c': {'default': 'Открыть анализ', 'kind': 'button'},
 '841fa27d7d8c': {'default': 'Синхронизировать чаты', 'kind': 'button'},
 '766d5301e754': {'default': 'Найти существующий чат', 'kind': 'button'},
 '46b733c94565': {'default': 'Выбрать группу', 'kind': 'button'},
 'b48c31408974': {'default': 'Выбрать канал', 'kind': 'button'},
 'ae386efa179c': {'default': 'Выбери чат, где бот уже состоит, кнопкой под полем ввода. После выбора я '
                             'проверю ваши права администратора.',
                  'kind': 'text'},
 'b6db8045441d': {'default': 'Выбор чата устарел. Открой «Найти существующий чат» снова.', 'kind': 'text'},
 '93c332681647': {'default': 'Чат подключён.', 'kind': 'text'},
 'f3d71269a3f3': {'default': 'Выбор чата отменён.', 'kind': 'text'},
 '05c2943f3f6b': {'default': 'Выбрать снова', 'kind': 'button'}}
_db = None
_overrides = {}
_options = {}
STYLES = {'original', 'neutral', 'primary', 'success', 'danger'}

class Label(str):

    def __new__(cls, value, key):
        obj = super().__new__(cls, value)
        obj.key = key
        return obj

def configure(db):
    global _db, _overrides, _options
    _db = db
    db.executescript('CREATE TABLE IF NOT EXISTS ui_overrides(key TEXT PRIMARY KEY,value TEXT);\n    CREATE TABLE IF NOT EXISTS ui_options(key TEXT PRIMARY KEY,value TEXT);\n    CREATE TABLE IF NOT EXISTS ui_drafts(user_id INTEGER PRIMARY KEY,kind TEXT,item TEXT,query TEXT,page INTEGER);\n    CREATE TABLE IF NOT EXISTS ui_audit(id INTEGER PRIMARY KEY,actor INTEGER,action TEXT,item TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);\n    ')
    db.commit()
    _overrides = dict(db.execute('SELECT key,value FROM ui_overrides'))
    _options = dict(db.execute('SELECT key,value FROM ui_options'))

def owner_id():
    try:
        return int(os.getenv('BOT_OWNER_ID', '0'))
    except ValueError:
        return 0

def is_owner(uid):
    return owner_id() > 0 and uid == owner_id()

def fields(template):
    result = []
    for _, field, spec, conversion in Formatter().parse(template):
        if field is not None:
            if not re.fullmatch('v\\d+', field) or spec or conversion:
                raise ValueError('Используйте только исходные поля {v0}, {v1} без преобразований.')
            result.append(field)
    return Counter(result)

class Markup(HTMLParser):

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack = []

    def handle_data(self, data):
        if '<' in data:
            raise ValueError('Экранируйте знак < как &lt;.')

    def handle_starttag(self, tag, attrs):
        if tag not in {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'code', 'pre', 'a', 'blockquote', 'tg-spoiler', 'span', 'tg-emoji'}:
            raise ValueError('Неподдерживаемый HTML-тег.')
        attrs = dict(attrs)
        if 'tg-emoji' in self.stack:
            raise ValueError('Внутрь премиум-эмодзи нельзя помещать теги.')
        if tag == 'tg-emoji':
            if set(attrs) != {'emoji-id'} or not valid_id(attrs['emoji-id']):
                raise ValueError('Для tg-emoji нужен числовой emoji-id.')
        elif tag == 'a':
            if set(attrs) != {'href'} or not (attrs['href'] or '').startswith(('https://', 'tg://user?id=')):
                raise ValueError('Для ссылки нужен href с https:// или tg://user?id=.')
        elif tag == 'span':
            if attrs != {'class': 'tg-spoiler'}:
                raise ValueError('Допустим только span class="tg-spoiler".')
        elif tag == 'code':
            if attrs and (set(attrs) != {'class'} or not attrs['class'].startswith('language-')):
                raise ValueError('Недопустимые атрибуты code.')
        elif attrs:
            raise ValueError('Атрибуты этого тега недоступны.')
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError('Проверьте закрывающие HTML-теги.')

    def handle_startendtag(self, tag, attrs):
        raise ValueError('Самозакрывающиеся HTML-теги недоступны.')

    def handle_comment(self, data):
        raise ValueError('HTML-комментарии недоступны.')

def validate(key, value):
    item = CATALOG[key]
    validate_tags(value)
    if item['kind'] != 'button' and re.search('&(?!amp;|lt;|gt;|quot;|#\\d+;|#x[0-9a-fA-F]+;)', value):
        raise ValueError('Экранируйте & как &amp;.')
    if not value.strip():
        raise ValueError('Элемент не может быть пустым.')
    if fields(value) != fields(item['default']):
        raise ValueError('Сохраните все поля {v0}, {v1} и число их повторений.')
    if item['kind'] == 'button':
        matches = list(TAG.finditer(value))
        if len(matches) > 1 or (matches and value[:matches[0].start()].strip()):
            raise ValueError('Для кнопки поставьте один премиум-эмодзи в начале.')
        visible = plain(value)
        if matches and (not button_parts(value, True)[0]):
            raise ValueError('Добавьте название кнопки после премиум-эмодзи.')
        if any((not valid_id(m[1]) for m in matches)):
            raise ValueError('Некорректный emoji-id.')
        if len(visible) > 64 or '\n' in visible or '<' in visible or ('>' in visible):
            raise ValueError('Кнопка: до 64 символов, без HTML и переноса строки.')
    else:
        if len(value) > max(len(item['default']), 512):
            raise ValueError('Слишком длинный текст: сократите оформление.')
        validate_fragment(item['default'], value)

def validate_fragment(default, value):

    def signature(template):
        parser = Markup()
        opens = []
        closes = []

        def end(tag):
            if parser.stack:
                if parser.stack.pop() != tag:
                    raise ValueError('Проверьте вложенность HTML-тегов.')
            else:
                closes.append(tag)
        parser.handle_endtag = end
        parser.feed(template)
        parser.close()
        opens.extend(parser.stack)
        return (closes, opens)
    if signature(default) != signature(value):
        raise ValueError('Сохраните граничные HTML-теги; внутренние теги должны закрываться.')

def get(key):
    return _overrides.get(key, CATALOG[key]['default'])

def text(key, **values):
    default = CATALOG[key]['default']
    try:
        result = get(key).format_map(values)
        if CATALOG[key]['kind'] == 'button' and (not plain(result).strip() or len(plain(result)) > 64):
            raise ValueError('Слишком длинное название кнопки')
        return Label(result, key)
    except (KeyError, ValueError, IndexError):
        logging.warning('Некорректное оформление %s; использован исходный текст', key)
        return Label(default.format_map(values), key)

def option(key, default='original'):
    return _options.get(key, default)

def style(label, original=None):
    value = option('style:' + getattr(label, 'key', ''))
    if value == 'original':
        value = option('style')
    return original if value == 'original' else None if value == 'neutral' else value if value in STYLES else original

def layout(rows):
    columns = option('columns')
    if columns not in ('1', '2', '3'):
        return rows
    buttons = [b for row in rows for b in row]
    n = int(columns)
    return [buttons[i:i + n] for i in range(0, len(buttons), n)]

def save(db, actor, key, value):
    if not is_owner(actor):
        raise PermissionError('Только создатель бота.')
    validate(key, value)
    db.execute('INSERT OR REPLACE INTO ui_overrides VALUES(?,?)', (key, value))
    db.execute("INSERT INTO ui_audit(actor,action,item) VALUES(?,'text',?)", (actor, key))
    db.commit()
    if db is _db:_overrides[key] = value

def set_option(db, actor, key, value):
    if not is_owner(actor):
        raise PermissionError('Только создатель бота.')
    if key == 'columns':
        if value not in ('original', '1', '2', '3'):
            raise ValueError('Недопустимая раскладка.')
    elif key == 'style' or (key.startswith('style:') and key[6:] in CATALOG and (CATALOG[key[6:]]['kind'] == 'button')):
        if value not in STYLES:
            raise ValueError('Недопустимый стиль.')
    else:
        raise ValueError('Недопустимая настройка.')
    db.execute('INSERT OR REPLACE INTO ui_options VALUES(?,?)', (key, value))
    db.execute("INSERT INTO ui_audit(actor,action,item) VALUES(?,'option',?)", (actor, key))
    db.commit()
    if db is _db:_options[key] = value

def reset(db, actor, key=None):
    if not is_owner(actor):
        raise PermissionError('Только создатель бота.')
    if key:
        if key not in CATALOG:
            raise ValueError('Неизвестный элемент.')
        db.execute('DELETE FROM ui_overrides WHERE key=?', (key,))
        db.execute('DELETE FROM ui_options WHERE key=?', ('style:' + key,))
    else:
        db.execute('DELETE FROM ui_overrides')
        db.execute('DELETE FROM ui_options')
    db.execute("INSERT INTO ui_audit(actor,action,item) VALUES(?,'reset',?)", (actor, key))
    db.commit()
    if db is _db:
        if key:
            _overrides.pop(key,None);_options.pop('style:'+key,None)
        else:
            _overrides.clear();_options.clear()

def button(text, **kwargs):
    from aiogram.types import InlineKeyboardButton
    chosen = style(text, kwargs.pop('style', None))
    if chosen:
        kwargs['style'] = chosen
    visible, icon = button_parts(text)
    if icon:
        kwargs['icon_custom_emoji_id'] = icon
    return InlineKeyboardButton(text=visible if len(visible)<=64 else visible[:63]+'…', **kwargs)

def keyboard_button(text, **kwargs):
    from aiogram.types import KeyboardButton
    visible, icon = button_parts(text)
    if icon:
        kwargs['icon_custom_emoji_id'] = icon
    chosen = style(text, kwargs.pop('style', None))
    if chosen:
        kwargs['style'] = chosen
    return KeyboardButton(text=visible if len(visible)<=64 else visible[:63]+'…', **kwargs)

def escape(value, quote=True):
    """Only catalog-owned Labels contain trusted HTML; member data stays escaped."""
    from html import escape as html_escape
    if isinstance(value, Label) and value.key in CATALOG and (CATALOG[value.key]['kind'] == 'text'):
        return str(value)
    return html_escape(value, quote=quote)


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])

def install_owner(a):

    async def show(m, text, rows, preview=None):
        markup = keyboard(rows)
        if preview is not None:
            markup.inline_keyboard.insert(0, [preview])
        await a.render(m, text, reply_markup=markup)

    async def home(m):
        a.clear_draft(m.chat.id)
        changed = a.db.execute('SELECT COUNT(*) FROM ui_overrides').fetchone()[0]
        await show(m, f"<b>Оформление · Только создатель</b>\n\nИзменено текстов: {changed} / {len(CATALOG)}\nОбщий стиль кнопок: {e(ru('style',option('style')))}\nКнопок в ряду: {e(ru('columns',option('columns')))}\n\nИзменения общие для всех пользователей бота. Права и действия кнопок сохраняются. Выберите элемент, измените текст, проверьте предпросмотр и сохраните.\nКоманда /design всегда открывает этот редактор.", [[('Тексты и сообщения', 'u:list:text:0'), ('Кнопки', 'u:list:button:0')], [('Поиск элемента', 'u:search')], [('Общий стиль кнопок', 'u:styles:global')], [('Раскладка кнопок', 'u:layout')], [('Сбросить оформление', 'u:resetask')], [('‹ Главное меню', 'p:home:0')]])

    async def listing(m, kind, page=0, query=''):
        query = query.casefold()
        items = [(k, v) for k, v in CATALOG.items() if (kind == 'all' or v['kind'] == kind) and (not query or query in (k + ' ' + v['default'] + ' ' + get(k)).casefold())]
        page = max(0, min(page, max(0, (len(items) - 1) // 6)))
        a.db.execute("INSERT OR REPLACE INTO ui_drafts VALUES(?,'browse',?,?,?)", (m.chat.id, kind, query, page))
        a.db.commit()
        rows = []
        for key, item in items[page * 6:page * 6 + 6]:
            label = plain(get(key)).replace('\n', ' ').strip()
            if len(label) > 45:
                label = label[:44] + '…'
            rows.append([(label or key, f'u:item:{key}')])
        nav = []
        if page:
            nav.append(('‹', f'u:page:{page - 1}'))
        if (page + 1) * 6 < len(items):
            nav.append(('›', f'u:page:{page + 1}'))
        if nav:
            rows.append(nav)
        rows += [[('Поиск', 'u:search')], [('‹ Оформление', 'u:home')]]
        await show(m, f'<b>Каталог оформления</b>\nЭлементов: {len(items)} · Страница {page + 1}\n' + (f'Поиск: {e(query)}\n' if query else '') + 'Выберите текст или кнопку.', rows)

    async def item(m, key, note=''):
        value = get(key)
        original = CATALOG[key]
        params = ', '.join(('{' + v + '}' for v in fields(original['default']))) or 'нет'
        rows = [[('Изменить текст', f'u:edit:{key}')]]
        if original['kind'] == 'button':
            rows.append([('Стиль этой кнопки', f'u:styles:{key}')])
        rows += [[('Вернуть исходный элемент', f'u:resetitemask:{key}')], [('‹ Каталог', 'u:back')], [('Оформление', 'u:home')]]
        await show(m, f"<b>{('Кнопка' if original['kind'] == 'button' else 'Текст')}</b> · <code>{key}</code>\nПоля: {e(params)}\n\nТекущее значение:\n<pre>{e(value[:3000])}</pre>\n{e(note)}", rows)

    async def styles(m, target):
        key = 'style' if target == 'global' else 'style:' + target
        rows = [[(label, f'u:style:{target}:{value}')] for label, value in [('Исходный / наследовать', 'original'), ('Обычный', 'neutral'), ('Основной', 'primary'), ('Успех', 'success'), ('Опасность', 'danger')]]
        rows.append([('‹ Назад', 'u:home' if target == 'global' else f'u:item:{target}')])
        if target == 'global':
            preview = button('Пример кнопки', callback_data='u:noop')
        else:
            values = {name: '123' for name in fields(get(target))}
            preview = button(text(target, **values), callback_data='u:noop')
        current_style = e(ru('style', option(key)))
        await show(m, f'<b>Стиль кнопок</b>\nТекущее значение: {current_style}\nЦвет и вид отображения зависят от версии приложения Telegram.', rows, preview=preview)

    async def consume(m):
        row = a.db.execute('SELECT kind,item,query,page FROM ui_drafts WHERE user_id=?', (m.from_user.id,)).fetchone()
        if not row or row[0] not in ('edit', 'search'):
            return False
        if not is_owner(m.from_user.id):
            a.db.execute('DELETE FROM ui_drafts WHERE user_id=?', (m.from_user.id,))
            a.db.commit()
            return False
        await a.clean_input(m)
        value = m.text or ''
        if row[0] == 'search':
            return await listing(m, 'all', 0, value[:100]) or True
        key = row[1]
        try:
            value = from_message(m)
            validate(key, value)
            a.db.execute("UPDATE ui_drafts SET kind='preview',query=? WHERE user_id=?", (value, m.from_user.id))
            a.db.commit()
            sample = {field: '123' for field in fields(value)}
            preview = value.format_map(sample)
            if CATALOG[key]['kind'] == 'button':
                rows = []
                preview_button = button(Label(preview, key), callback_data='u:noop')
                preview_text = 'Предпросмотр кнопки ниже.'
            else:
                rows = []
                preview_button = None
                try:
                    parser = Markup()
                    parser.feed(preview)
                    parser.close()
                    if parser.stack:
                        raise ValueError()
                    preview_text = preview
                except ValueError:
                    preview_text = '<pre>' + e(preview[:2500]) + '</pre>'
            rows += [[('Сохранить', f'u:save:{key}'), ('Править', f'u:edit:{key}')], [('Отмена', f'u:cancel:{key}')]]
            await show(m, '<b>Предпросмотр</b>\nПоля временно заменены на 123.\n\n' + preview_text, rows, preview=preview_button)
        except (ValueError, KeyError) as exc:
            a.db.execute("UPDATE ui_drafts SET kind='edit' WHERE user_id=?", (m.from_user.id,))
            a.db.commit()
            await show(m, '<b>Проверьте оформление</b>\n' + e(user_error(exc)) + '\nОтправьте исправленный текст.', [[('Отмена', f'u:cancel:{key}')]])
        return True

    @a.dp.callback_query(F.data.startswith('u:'))
    async def click(c):
        if not c.message or c.message.chat.type != 'private' or c.message.chat.id != c.from_user.id or (not is_owner(c.from_user.id)):
            await c.answer('Только создатель бота.', show_alert=True)
            return
        await c.answer()
        p = c.data.split(':')
        act = p[1]
        m = c.message
        uid = c.from_user.id
        try:
            if act == 'home':
                return await home(m)
            if act == 'noop':
                return
            if act == 'list':
                return await listing(m, p[2], int(p[3]))
            if act in ('page', 'back'):
                row = a.db.execute('SELECT item,query,page FROM ui_drafts WHERE user_id=?', (uid,)).fetchone()
                if row and row[0] in ('text', 'button', 'all'):
                    return await listing(m, row[0], int(p[2]) if act == 'page' else row[2], row[1])
                return await listing(m, 'all')
            if act == 'search':
                a.clear_draft(uid)
                a.db.execute("INSERT OR REPLACE INTO ui_drafts VALUES(?,'search','','',0)", (uid,))
                a.db.commit()
                return await show(m, '<b>Найти элемент</b>\nОтправьте часть текущего или исходного текста, например «Модератор» или «мут».', [[('Отмена', 'u:home')]])
            if act in ('item', 'edit', 'cancel', 'resetitemask', 'resetitem', 'save'):
                key = p[2]
                if key not in CATALOG:
                    raise ValueError('Неизвестный элемент.')
                if act == 'item':
                    return await item(m, key)
                if act == 'cancel':
                    a.clear_draft(uid)
                    return await item(m, key)
                if act == 'edit':
                    a.clear_draft(uid)
                    a.db.execute("INSERT INTO ui_drafts VALUES(?,'edit',?,'',0)", (uid, key))
                    a.db.commit()
                    value = get(key)
                    return await show(m, '<b>Изменить элемент</b>\nОтправьте новое значение одним сообщением. Сохраните поля {v0}, {v1}, если они есть. Можно отправить премиум-эмодзи прямо из Telegram: в тексте — несколько, на кнопке — один перед названием. Также доступны HTML-теги для текста.\n\n<pre>' + e(value[:3000]) + '</pre>', [[('Отмена', f'u:cancel:{key}')]])
                if act == 'save':
                    row = a.db.execute("SELECT query FROM ui_drafts WHERE user_id=? AND kind='preview' AND item=?", (uid, key)).fetchone()
                    if not row:
                        return await item(m, key, 'Предпросмотр устарел. Создайте его заново.')
                    save(a.db, uid, key, row[0])
                    a.clear_draft(uid)
                    return await item(m, key, 'Сохранено. Изменения видны при следующем открытии экрана.')
                if act == 'resetitemask':
                    return await show(m, '<b>Вернуть исходный текст и стиль элемента?</b>', [[('Вернуть', f'u:resetitem:{key}')], [('Отмена', f'u:item:{key}')]])
                reset(a.db, uid, key)
                return await item(m, key, 'Исходное оформление восстановлено.')
            if act == 'styles':
                return await styles(m, p[2])
            if act == 'style':
                set_option(a.db, uid, 'style' if p[2] == 'global' else 'style:' + p[2], p[3])
                return await styles(m, p[2])
            if act == 'layout':
                return await show(m, '<b>Раскладка кнопок</b>\nПрименяется к панелям; порядок и действия кнопок сохраняются.', [[('Исходная', 'u:columns:original')], [('По одной', 'u:columns:1'), ('По две', 'u:columns:2'), ('По три', 'u:columns:3')], [('‹ Назад', 'u:home')]])
            if act == 'columns':
                set_option(a.db, uid, 'columns', p[2])
                return await home(m)
            if act == 'resetask':
                return await show(m, '<b>Сбросить всё оформление?</b>\nТексты, стили и раскладка вернутся к исходным значениям.', [[('Сбросить', 'u:reset')], [('Отмена', 'u:home')]])
            if act == 'reset':
                reset(a.db, uid)
                return await home(m)
        except Exception as exc:
            a.logging.exception('Не удалось изменить оформление')
            await show(m, 'Не удалось изменить оформление. ' + e(user_error(exc))[:500], [[('Оформление', 'u:home')]])
    a.owner_design = home
    a.owner_design_input = consume


def install_chat_sync(a):
    a.db.execute('CREATE TABLE IF NOT EXISTS chat_pick_requests(user_id INTEGER PRIMARY KEY,request_id INTEGER)')
    a.db.commit()

    def restore():
        for table in ('chat_users', 'violations', 'settings', 'appeals', 'decisions', 'member_profiles', 'audit_log', 'restrictions', 'ai_settings'):
            a.db.execute(f'INSERT OR IGNORE INTO chats SELECT DISTINCT chat_id,CAST(chat_id AS TEXT) FROM {table} WHERE chat_id<0')
        a.db.commit()
    restore()

    class Discover(BaseMiddleware):

        async def __call__(self, handler, event, data):
            for field in ('message', 'edited_message', 'channel_post', 'edited_channel_post', 'my_chat_member', 'chat_member', 'chat_join_request'):
                item = getattr(event, field, None)
                if item:
                    chat = getattr(item, 'chat', None)
                    if chat:
                        a.remember_chat(chat)
                    origin = getattr(item, 'forward_origin', None)
                    if origin and getattr(origin, 'chat', None):
                        a.remember_chat(origin.chat)
            return await handler(event, data)
    a.dp.update.outer_middleware(Discover())

    async def add(cid, uid):
        if cid >= 0 or not await a.allowed(cid, uid):
            raise ValueError('Ты должен быть администратором выбранного чата.')
        chat = await a.bot.get_chat(cid)
        if not await a.is_admin(cid, a.bot.id):
            raise ValueError('Бот должен быть администратором чата.')
        a.remember_chat(chat)
        linked = getattr(chat, 'linked_chat_id', None)
        if isinstance(linked, int) and linked < 0 and await a.allowed(linked, uid):
            try:
                if await a.is_admin(linked, a.bot.id):
                    a.remember_chat(await a.bot.get_chat(linked))
            except Exception:
                a.logging.info('Связанная группа обсуждений недоступна')
        return chat

    async def sync(uid):
        restore()
        ids = [r[0] for r in a.db.execute('SELECT chat_id FROM chats').fetchall()]
        slots = asyncio.Semaphore(4)
        found = 0

        async def refresh(cid):
            nonlocal found
            async with slots:
                try:
                    await asyncio.wait_for(add(cid, uid), timeout=5)
                    found += 1
                except Exception:
                    pass
        try:
            await asyncio.wait_for(asyncio.gather(*(refresh(cid) for cid in ids)), timeout=15)
        except asyncio.TimeoutError:
            pass
        return found

    async def choose(m, uid):
        a.clear_draft(uid)
        rid = secrets.randbelow(2 ** 30)
        a.db.execute('INSERT OR REPLACE INTO chat_pick_requests VALUES(?,?)', (uid, rid))
        a.db.commit()
        keyboard = ReplyKeyboardMarkup(keyboard=[[keyboard_button(text=text('46b733c94565'), request_chat=KeyboardButtonRequestChat(request_id=rid, chat_is_channel=False, bot_is_member=True, request_title=True))], [keyboard_button(text=text('b48c31408974'), request_chat=KeyboardButtonRequestChat(request_id=rid + 1, chat_is_channel=True, bot_is_member=True, request_title=True))], [KeyboardButton(text='Отменить выбор чата')]], resize_keyboard=True, one_time_keyboard=True)
        await a.bot.send_message(uid, text('ae386efa179c'), reply_markup=keyboard)

    async def shared(m):
        if not m.from_user:
            return
        row = a.db.execute('SELECT request_id FROM chat_pick_requests WHERE user_id=?', (m.from_user.id,)).fetchone()
        share = getattr(m, 'chat_shared', None)
        if not row or not share or share.request_id not in (row[0], row[0] + 1):
            await a.render(m, text('b6db8045441d'))
            return
        try:
            chat = await add(share.chat_id, m.from_user.id)
        except Exception as exc:
            await a.render(m, 'Не удалось подключить чат. ' + a.escape(user_error(exc))[:300], reply_markup=a.kb([[(text('05c2943f3f6b'), 's:choose')], [(text('c20945ceeebc'), 'p:home:0')]]))
            return
        a.db.execute('DELETE FROM chat_pick_requests WHERE user_id=?', (m.from_user.id,))
        a.db.commit()
        await a.bot.send_message(m.from_user.id, text('93c332681647'), reply_markup=ReplyKeyboardRemove())
        await a.show_chat(m, chat.id)

    async def cancel_choice(m):
        a.db.execute('DELETE FROM chat_pick_requests WHERE user_id=?', (m.from_user.id,))
        a.db.commit()
        await a.bot.send_message(m.from_user.id, text('f3d71269a3f3'), reply_markup=ReplyKeyboardRemove())
        await a.panel(m, m.from_user.id)

    @a.dp.callback_query(F.data.startswith('s:'))
    async def click(c):
        if not c.message or c.message.chat.type != 'private' or c.message.chat.id != c.from_user.id:
            await c.answer('Только в ЛС.', show_alert=True)
            return
        await c.answer()
        if c.data == 's:choose':
            await choose(c.message, c.from_user.id)
        elif c.data == 's:sync':
            await sync(c.from_user.id)
            await a.panel(c.message, c.from_user.id)
    a.sync_chats = sync
    a.shared_chat = shared
    a.cancel_chat_choice = cancel_choice

"""Presentation-only templates. Authorization and callbacks are never customizable."""
import json, logging, os, re
from pathlib import Path
from string import Formatter
from collections import Counter
from html.parser import HTMLParser

CATALOG=json.loads(Path(__file__).with_name('ui_catalog.json').read_text(encoding='utf-8'))
_db=None
STYLES={'original','neutral','primary','success','danger'}
class Label(str):
    def __new__(cls,value,key):
        obj=super().__new__(cls,value);obj.key=key;return obj

def configure(db):
    global _db
    _db=db
    db.executescript('''CREATE TABLE IF NOT EXISTS ui_overrides(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS ui_options(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS ui_drafts(user_id INTEGER PRIMARY KEY,kind TEXT,item TEXT,query TEXT,page INTEGER);
    CREATE TABLE IF NOT EXISTS ui_audit(id INTEGER PRIMARY KEY,actor INTEGER,action TEXT,item TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    ''');db.commit()

def owner_id():
    try:return int(os.getenv('BOT_OWNER_ID','0'))
    except ValueError:return 0

def is_owner(uid):return owner_id()>0 and uid==owner_id()

def fields(template):
    result=[]
    for _,field,spec,conversion in Formatter().parse(template):
        if field is not None:
            if not re.fullmatch(r'v\d+',field) or spec or conversion:raise ValueError('Используйте только исходные поля {v0}, {v1} без преобразований.')
            result.append(field)
    return Counter(result)

class Markup(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=False);self.stack=[]
    def handle_data(self,data):
        if '<' in data:raise ValueError('Экранируйте знак < как &lt;.')
    def handle_starttag(self,tag,attrs):
        if tag not in {'b','strong','i','em','u','ins','s','strike','del','code','pre','a','blockquote','tg-spoiler','span'}:raise ValueError('Неподдерживаемый HTML-тег.')
        attrs=dict(attrs)
        if tag=='a':
            if set(attrs)!={'href'} or not (attrs['href'] or '').startswith(('https://','tg://user?id=')):raise ValueError('Для ссылки нужен href с https:// или tg://user?id=.')
        elif tag=='span':
            if attrs!={'class':'tg-spoiler'}:raise ValueError('Допустим только span class="tg-spoiler".')
        elif tag=='code':
            if attrs and (set(attrs)!={'class'} or not attrs['class'].startswith('language-')):raise ValueError('Недопустимые атрибуты code.')
        elif attrs:raise ValueError('Атрибуты этого тега недоступны.')
        self.stack.append(tag)
    def handle_endtag(self,tag):
        if not self.stack or self.stack.pop()!=tag:raise ValueError('Проверьте закрывающие HTML-теги.')
    def handle_startendtag(self,tag,attrs):raise ValueError('Самозакрывающиеся HTML-теги недоступны.')
    def handle_comment(self,data):raise ValueError('HTML-комментарии недоступны.')

def validate(key,value):
    item=CATALOG[key]
    if item['kind']!='button' and re.search(r'&(?!amp;|lt;|gt;|quot;|#\d+;|#x[0-9a-fA-F]+;)',value):raise ValueError('Экранируйте & как &amp;.')
    if not value.strip():raise ValueError('Элемент не может быть пустым.')
    if fields(value)!=fields(item['default']):raise ValueError('Сохраните все поля {v0}, {v1} и число их повторений.')
    if item['kind']=='button':
        if len(value)>64 or '\n' in value or '<' in value or '>' in value:raise ValueError('Кнопка: до 64 символов, без HTML и переноса строки.')
    else:
        if len(value)>max(len(item['default']),512):raise ValueError('Слишком длинный текст: сократите оформление.')
        # Templates can be fragments of a larger message. Preserve their boundary tags.
        # Compare parser stack effects with original instead of rejecting valid fragments.
        validate_fragment(item['default'],value)

def validate_fragment(default,value):
    def signature(template):
        parser=Markup();opens=[];closes=[]
        # Allow boundary closing tags that were already present in the original fragment.
        def end(tag):
            if parser.stack:
                if parser.stack.pop()!=tag:raise ValueError('Проверьте вложенность HTML-тегов.')
            else:closes.append(tag)
        parser.handle_endtag=end
        parser.feed(template);parser.close();opens.extend(parser.stack)
        return closes,opens
    if signature(default)!=signature(value):raise ValueError('Сохраните граничные HTML-теги; внутренние теги должны закрываться.')

def get(key):
    row=_db.execute('SELECT value FROM ui_overrides WHERE key=?',(key,)).fetchone() if _db else None
    return row[0] if row else CATALOG[key]['default']

def text(key,**values):
    default=CATALOG[key]['default']
    try:
        result=get(key).format_map(values)
        if CATALOG[key]['kind']=='button' and (not result.strip() or len(result)>64):raise ValueError('button too long')
        return Label(result,key)
    except (KeyError,ValueError,IndexError):
        logging.warning('Invalid UI customization for %s; using default',key)
        return Label(default.format_map(values),key)

def option(key,default='original'):
    row=_db.execute('SELECT value FROM ui_options WHERE key=?',(key,)).fetchone() if _db else None
    return row[0] if row else default

def style(label,original=None):
    value=option('style:'+getattr(label,'key',''))
    if value=='original':value=option('style')
    return original if value=='original' else None if value=='neutral' else value if value in STYLES else original

def layout(rows):
    columns=option('columns')
    if columns not in ('1','2','3'):return rows
    buttons=[b for row in rows for b in row];n=int(columns)
    return [buttons[i:i+n] for i in range(0,len(buttons),n)]

def save(db,actor,key,value):
    if not is_owner(actor):raise PermissionError('Только создатель бота.')
    validate(key,value)
    db.execute('INSERT OR REPLACE INTO ui_overrides VALUES(?,?)',(key,value))
    db.execute("INSERT INTO ui_audit(actor,action,item) VALUES(?,'text',?)",(actor,key));db.commit()

def set_option(db,actor,key,value):
    if not is_owner(actor):raise PermissionError('Только создатель бота.')
    if key=='columns':
        if value not in ('original','1','2','3'):raise ValueError('Недопустимая раскладка.')
    elif key=='style' or key.startswith('style:') and key[6:] in CATALOG and CATALOG[key[6:]]['kind']=='button':
        if value not in STYLES:raise ValueError('Недопустимый стиль.')
    else:raise ValueError('Недопустимая настройка.')
    db.execute('INSERT OR REPLACE INTO ui_options VALUES(?,?)',(key,value))
    db.execute("INSERT INTO ui_audit(actor,action,item) VALUES(?,'option',?)",(actor,key));db.commit()

def reset(db,actor,key=None):
    if not is_owner(actor):raise PermissionError('Только создатель бота.')
    if key:
        if key not in CATALOG:raise ValueError('Неизвестный элемент.')
        db.execute('DELETE FROM ui_overrides WHERE key=?',(key,));db.execute('DELETE FROM ui_options WHERE key=?',('style:'+key,))
    else:
        db.execute('DELETE FROM ui_overrides');db.execute('DELETE FROM ui_options')
    db.execute("INSERT INTO ui_audit(actor,action,item) VALUES(?,'reset',?)",(actor,key));db.commit()


def button(text,**kwargs):
    from aiogram.types import InlineKeyboardButton
    chosen=style(text,kwargs.pop('style',None))
    if chosen:kwargs['style']=chosen
    return InlineKeyboardButton(text=str(text),**kwargs)

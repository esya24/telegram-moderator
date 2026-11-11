"""Owner-only customization UI. Recovery controls deliberately use fixed labels."""
from html import escape as e
from aiogram import F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import ui_theme as theme


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text,callback_data=data) for text,data in row] for row in rows])


def install(a):
    async def show(m,text,rows,preview=None):
        markup=keyboard(rows)
        if preview is not None:markup.inline_keyboard.insert(0,[preview])
        await a.render(m,text,reply_markup=markup)

    async def home(m):
        a.clear_draft(m.chat.id)
        changed=a.db.execute('SELECT COUNT(*) FROM ui_overrides').fetchone()[0]
        await show(m,f'<b>Оформление · Только создатель</b>\n\n'
                   f'Изменено текстов: {changed} / {len(theme.CATALOG)}\n'
                   f'Общий стиль кнопок: {e(theme.option("style"))}\n'
                   f'Кнопок в ряду: {e(theme.option("columns"))}\n\n'
                   'Изменения общие для всех пользователей бота. Права и действия кнопок сохраняются. '
                   'Выберите элемент, измените текст, проверьте предпросмотр и сохраните.\n'
                   'Команда /design всегда открывает этот редактор.',[
                       [('Тексты и сообщения','u:list:text:0'),('Кнопки','u:list:button:0')],
                       [('Поиск элемента','u:search')],[('Общий стиль кнопок','u:styles:global')],
                       [('Раскладка кнопок','u:layout')],[('Сбросить оформление','u:resetask')],
                       [('‹ Главное меню','p:home:0')]])

    async def listing(m,kind,page=0,query=''):
        query=query.casefold()
        items=[(k,v) for k,v in theme.CATALOG.items() if (kind=='all' or v['kind']==kind) and
               (not query or query in (k+' '+v['default']+' '+theme.get(k)).casefold())]
        page=max(0,min(page,max(0,(len(items)-1)//6)))
        a.db.execute("INSERT OR REPLACE INTO ui_drafts VALUES(?,'browse',?,?,?)",(m.chat.id,kind,query,page));a.db.commit()
        rows=[]
        for key,item in items[page*6:page*6+6]:
            label=theme.get(key).replace('\n',' ').strip()
            if len(label)>45:label=label[:44]+'…'
            rows.append([(label or key,f'u:item:{key}')])
        nav=[]
        if page:nav.append(('‹',f'u:page:{page-1}'))
        if (page+1)*6<len(items):nav.append(('›',f'u:page:{page+1}'))
        if nav:rows.append(nav)
        rows += [[('Поиск','u:search')],[('‹ Оформление','u:home')]]
        await show(m,f'<b>Каталог оформления</b>\nЭлементов: {len(items)} · Страница {page+1}\n'
                   +(f'Поиск: {e(query)}\n' if query else '')+'Выберите текст или кнопку.',rows)

    async def item(m,key,note=''):
        value=theme.get(key);original=theme.CATALOG[key]
        params=', '.join('{'+v+'}' for v in theme.fields(original['default'])) or 'нет'
        rows=[[('Изменить текст',f'u:edit:{key}')]]
        if original['kind']=='button':rows.append([('Стиль этой кнопки',f'u:styles:{key}')])
        rows += [[('Вернуть исходный элемент',f'u:resetitemask:{key}')],[('‹ Каталог','u:back')],[('Оформление','u:home')]]
        await show(m,f'<b>{"Кнопка" if original["kind"]=="button" else "Текст"}</b> · <code>{key}</code>\n'
                   f'Поля: {e(params)}\n\nТекущее значение:\n<pre>{e(value[:3000])}</pre>\n'
                   f'{e(note)}',rows)

    async def styles(m,target):
        key='style' if target=='global' else 'style:'+target
        rows=[[(label,f'u:style:{target}:{value}')] for label,value in [
            ('Исходный / наследовать','original'),('Обычный','neutral'),('Основной','primary'),('Успех','success'),('Опасность','danger')]]
        rows.append([('‹ Назад','u:home' if target=='global' else f'u:item:{target}')])
        if target=='global':
            preview=theme.button('Пример кнопки',callback_data='u:noop')
        else:
            values={name:'123' for name in theme.fields(theme.get(target))}
            preview=theme.button(theme.text(target,**values),callback_data='u:noop')
        await show(m,f'<b>Стиль кнопок</b>\nТекущее значение: {e(theme.option(key))}\n'
                   'Цвет и вид отображения зависят от версии приложения Telegram.',rows,preview=preview)

    async def consume(m):
        row=a.db.execute('SELECT kind,item,query,page FROM ui_drafts WHERE user_id=?',(m.from_user.id,)).fetchone()
        if not row or row[0] not in ('edit','search'):return False
        if not theme.is_owner(m.from_user.id):
            a.db.execute('DELETE FROM ui_drafts WHERE user_id=?',(m.from_user.id,));a.db.commit()
            return False
        await a.clean_input(m)
        value=m.text or ''
        if row[0]=='search':
            return await listing(m,'all',0,value[:100]) or True
        key=row[1]
        try:
            theme.validate(key,value)
            # No production change before explicit confirmation. The draft is durable.
            a.db.execute("UPDATE ui_drafts SET kind='preview',query=? WHERE user_id=?",(value,m.from_user.id));a.db.commit()
            sample={field:'123' for field in theme.fields(value)}
            preview=value.format_map(sample)
            if theme.CATALOG[key]['kind']=='button':
                rows=[]
                preview_button=theme.button(theme.Label(preview,key),callback_data='u:noop')
                preview_text='Предпросмотр кнопки ниже.'
            else:
                rows=[]
                preview_button=None
                # Some catalog entries are HTML fragments; show these as escaped code.
                try:
                    parser=theme.Markup();parser.feed(preview);parser.close()
                    if parser.stack:raise ValueError()
                    preview_text=preview
                except ValueError:preview_text='<pre>'+e(preview[:2500])+'</pre>'
            rows += [[('Сохранить',f'u:save:{key}'),('Править',f'u:edit:{key}')],[('Отмена',f'u:cancel:{key}')]]
            await show(m,'<b>Предпросмотр</b>\nПоля временно заменены на 123.\n\n'+preview_text,rows,preview=preview_button)
        except (ValueError,KeyError) as exc:
            await show(m,'<b>Проверьте оформление</b>\n'+e(str(exc))+'\nОтправьте исправленный текст.',[[('Отмена',f'u:cancel:{key}')]])
        return True

    @a.dp.callback_query(F.data.startswith('u:'))
    async def click(c):
        if not c.message or c.message.chat.type!='private' or c.message.chat.id!=c.from_user.id or not theme.is_owner(c.from_user.id):
            await c.answer('Только создатель бота.',show_alert=True);return
        await c.answer()
        p=c.data.split(':');act=p[1];m=c.message;uid=c.from_user.id
        try:
            if act=='home':return await home(m)
            if act=='noop':return
            if act=='list':return await listing(m,p[2],int(p[3]))
            if act in ('page','back'):
                row=a.db.execute('SELECT item,query,page FROM ui_drafts WHERE user_id=?',(uid,)).fetchone()
                if row and row[0] in ('text','button','all'):
                    return await listing(m,row[0],int(p[2]) if act=='page' else row[2],row[1])
                return await listing(m,'all')
            if act=='search':
                a.clear_draft(uid)
                a.db.execute("INSERT OR REPLACE INTO ui_drafts VALUES(?,'search','','',0)",(uid,));a.db.commit()
                return await show(m,'<b>Найти элемент</b>\nОтправьте часть текущего или исходного текста, например «Модератор» или «мут».',[[('Отмена','u:home')]])
            if act in ('item','edit','cancel','resetitemask','resetitem','save'):
                key=p[2]
                if key not in theme.CATALOG:raise ValueError('Неизвестный элемент.')
                if act=='item':return await item(m,key)
                if act=='cancel':a.clear_draft(uid);return await item(m,key)
                if act=='edit':
                    a.clear_draft(uid)
                    a.db.execute("INSERT INTO ui_drafts VALUES(?,'edit',?,'',0)",(uid,key));a.db.commit()
                    value=theme.get(key)
                    return await show(m,'<b>Изменить элемент</b>\nОтправьте новое значение одним сообщением. '
                        'Сохраните поля {v0}, {v1}, если они есть. Для текста доступны HTML-теги Telegram, для кнопок — обычный текст и эмодзи.\n\n'
                        '<pre>'+e(value[:3000])+'</pre>',[[('Отмена',f'u:cancel:{key}')]])
                if act=='save':
                    row=a.db.execute("SELECT query FROM ui_drafts WHERE user_id=? AND kind='preview' AND item=?",(uid,key)).fetchone()
                    if not row:return await item(m,key,'Предпросмотр устарел. Создайте его заново.')
                    theme.save(a.db,uid,key,row[0]);a.clear_draft(uid)
                    return await item(m,key,'Сохранено. Изменения видны при следующем открытии экрана.')
                if act=='resetitemask':
                    return await show(m,'<b>Вернуть исходный текст и стиль элемента?</b>',[[('Вернуть',f'u:resetitem:{key}')],[('Отмена',f'u:item:{key}')]])
                theme.reset(a.db,uid,key);return await item(m,key,'Исходное оформление восстановлено.')
            if act=='styles':return await styles(m,p[2])
            if act=='style':
                theme.set_option(a.db,uid,'style' if p[2]=='global' else 'style:'+p[2],p[3]);return await styles(m,p[2])
            if act=='layout':
                return await show(m,'<b>Раскладка кнопок</b>\nПрименяется к панелям; порядок и действия кнопок сохраняются.',[
                    [('Исходная','u:columns:original')],[('По одной','u:columns:1'),('По две','u:columns:2'),('По три','u:columns:3')],[('‹ Назад','u:home')]])
            if act=='columns':theme.set_option(a.db,uid,'columns',p[2]);return await home(m)
            if act=='resetask':
                return await show(m,'<b>Сбросить всё оформление?</b>\nТексты, стили и раскладка вернутся к исходным значениям.',[[('Сбросить','u:reset')],[('Отмена','u:home')]])
            if act=='reset':theme.reset(a.db,uid);return await home(m)
        except Exception as exc:
            a.logging.exception('Owner appearance action failed')
            await show(m,'Не удалось изменить оформление. '+e(str(exc))[:500],[[('Оформление','u:home')]])

    a.owner_design=home
    a.owner_design_input=consume

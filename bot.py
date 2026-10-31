# Copyright 2026 rustamnova
# SPDX-License-Identifier: Apache-2.0

import json
import logging
import os
import time
import urllib.request
from html import escape
from pathlib import Path

from yandex import Client, SourceError, format_stop

STATE = Path(os.environ.get('STATE_DIRECTORY', '/var/lib/yandexbus'))
TOKEN = None
SOURCE = Client()
CURRENT_STATE = None
DEFAULT = 'https://yandex.ru/maps/213807/boltino/stops/stop__9675162/?tab=overview'
KEYBOARD = {'keyboard': [[{'text': '📍 Остановки'}], [{'text': '➕ Добавить остановку'}, {'text': '➖ Удалить остановку'}]], 'resize_keyboard': True}


def api(method, **data):
    global TOKEN
    if TOKEN is None:
        TOKEN = Path(os.environ.get('YANDEXBUS_TOKEN_FILE', '/etc/yandexbus/token')).read_text().strip()
    req = urllib.request.Request('https://api.telegram.org/bot' + TOKEN + '/' + method,
                                 data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=40) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError('Telegram API rejected request')
    return result['result']


def send(chat, text, markup=None, html=False):
    # Keep complete route rows even when ten stops produce long answers.
    chunks = []
    current = ''
    for line in text.splitlines():
        if len(current) + len(line) + 1 > 3900:
            chunks.append(current)
            current = ''
        current += line + '\n'
    if current:
        chunks.append(current)
    for chunk in chunks:
        api('sendMessage', chat_id=chat, text=chunk, reply_markup=markup or KEYBOARD, disable_web_page_preview=True,
            **({'parse_mode': 'HTML'} if html else {}))


def allowed_users():
    path = Path(os.environ.get('YANDEXBUS_ALLOWED_USERS_FILE', '/etc/yandexbus/allowed_users.json'))
    try:
        values = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(values, list) or not values or any(type(v) is not int or v <= 0 for v in values):
            raise ValueError()
        return frozenset(values)
    except (OSError, ValueError, TypeError):
        raise RuntimeError('A valid nonempty allowed-users file is required') from None


def load():
    try:
        state = json.loads((STATE / 'state.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'version': 2, 'offset': 0, 'users': {}}
    if 'users' not in state:
        owner = state.get('owner')
        state = {'version': 2, 'offset': state.get('offset', 0),
                 'users': {str(owner): {'owner': owner, 'stops': state.get('stops', [])}} if owner else {}}
    for profile in state['users'].values():
        profile['stops'] = [({'url': s, 'name': 'ЖК Мытищи Лайт' if '9675162' in s else 'Остановка'}
                             if isinstance(s, str) else s) for s in profile['stops']]
        for stop in profile['stops']:
            stop['url'] = stop['url'].replace('&lang=ru_RU', '')
    return state


def save_profile(profile):
    # main owns the complete state; helpers must never replace it with one profile.
    if CURRENT_STATE is not None:
        CURRENT_STATE['users'][str(profile['owner'])] = profile
        save(CURRENT_STATE)


def save(state):
    temp = STATE / 'state.tmp'
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    temp.replace(STATE / 'state.json')


def add_stop(state, text):
    url = SOURCE.resolve(text)
    stop_id = url.split('/stops/')[-1].split('/')[0]
    existing = next((stop for stop in state['stops']
                     if stop['url'].split('/stops/')[-1].split('/')[0] == stop_id), None)
    if existing:
        return 'Эта остановка уже добавлена: ' + escape(existing['name'])
    if len(state['stops']) >= 10:
        raise ValueError('Лимит — 10 остановок. Удали ненужную через /remove N.')
    snapshot = SOURCE.get(url)
    preview = format_stop(snapshot, html=True)
    state['stops'].append({'url': snapshot.url, 'name': snapshot.name})
    save_profile(state)
    return 'Остановка добавлена.\n\n' + preview


def show_stops(state):
    if not state['stops']:
        send(state['owner'], 'Остановок пока нет. Нажми «➕ Добавить остановку» и пришли ссылку Яндекс Карт.')
        return
    buttons = [[{'text': stop['name'],
                 'callback_data': 'arrival:' + stop['url'].split('/stops/')[-1].split('/')[0]}]
               for stop in state['stops']]
    send(state['owner'], '📍 Выбери остановку — покажу прибытие автобусов только по ней:',
         {'inline_keyboard': buttons})


def show_arrivals(state, stop_id):
    stop = next((stop for stop in state['stops']
                 if stop['url'].split('/stops/')[-1].split('/')[0] == stop_id), None)
    if stop is None:
        send(state['owner'], 'Эта остановка уже удалена. Выбери другую.')
        show_stops(state)
        return
    send(state['owner'], 'Получаю прибытие: ' + stop['name'] + '…')
    actions = {'inline_keyboard': [
        [{'text': '🔄 Обновить', 'callback_data': 'arrival:' + stop_id}],
        [{'text': '📍 Другие остановки', 'callback_data': 'stops'}],
    ]}
    try:
        snapshot = SOURCE.get(stop['url'])
        stop['name'] = snapshot.name
        save_profile(state)
        send(state['owner'], format_stop(snapshot, html=True), actions, html=True)
    except SourceError as error:
        send(state['owner'], stop['name'] + '\n' + str(error), actions)



def main():
    global CURRENT_STATE
    permitted = allowed_users()  # Fail closed before contacting Telegram.
    STATE.mkdir(parents=True, exist_ok=True)
    root = load()
    CURRENT_STATE = root
    save(root)  # Persist migration before processing any updates.
    while True:
        try:
            updates = api('getUpdates', offset=root['offset'], timeout=25, allowed_updates=['message', 'callback_query'])
            for update in updates:
                callback = update.get('callback_query')
                if callback:
                    user_id = callback.get('from', {}).get('id')
                    chat = callback.get('message', {}).get('chat', {})
                    authorized = (user_id in permitted and chat.get('type') == 'private'
                                  and chat.get('id') == user_id)
                    try:
                        api('answerCallbackQuery', callback_query_id=callback['id'],
                            **({} if authorized else {'text': 'Доступ закрыт', 'show_alert': True}))
                    except Exception:
                        logging.warning('Could not acknowledge callback')
                    state = root['users'].get(str(user_id)) if authorized else None
                    if state is not None:
                        data = callback.get('data', '')
                        if data == 'stops':
                            show_stops(state)
                        elif data.startswith('arrival:'):
                            try:
                                show_arrivals(state, data.split(':', 1)[1])
                            except Exception as error:
                                logging.warning('Arrival handler failed: %s', type(error).__name__)
                                send(user_id, 'Не удалось получить прибытие. Открой «📍 Остановки» и попробуй ещё раз.')
                        elif data.startswith('delete:'):
                            stop_id = data.split(':', 1)[1]
                            state['stops'] = [s for s in state['stops'] if s['url'].split('/stops/')[-1].split('/')[0] != stop_id]
                            save_profile(state)
                            send(user_id, 'Остановка удалена.')
                message = update.get('message', {})
                chat = message.get('chat', {})
                text = message.get('text', '')
                if chat.get('type') == 'private':
                    chat_id = chat['id']
                    sender = message.get('from', {}).get('id')
                    if sender not in permitted or sender != chat_id:
                        send(chat_id, '⛔ Доступ закрыт. Бот пока доступен только разрешённым пользователям.', {'remove_keyboard': True})
                    else:
                        state = root['users'].setdefault(str(sender), {
                            'owner': sender, 'stops': [{'url': DEFAULT, 'name': 'ЖК Мытищи Лайт'}]})
                        try:
                            if text.startswith('/start'):
                                send(chat_id, 'Готов! Открой «📍 Остановки» и выбери нужную кнопкой. Твой список остановок доступен только тебе. 🟢 — прогноз, 🕒 — расписание.')
                                show_stops(state)
                            elif text in ('📍 Остановки', '/stops', '🚌 Прибытие', '/bus'):
                                show_stops(state)
                            elif text == '➕ Добавить остановку':
                                send(chat_id, 'Пришли ссылку на остановку Яндекс Карт — короткую или полную. Я определю название и добавлю её в меню «Остановки».')
                            elif text == '➖ Удалить остановку':
                                buttons = [[{'text': 'Удалить: ' + s['name'], 'callback_data': 'delete:' + s['url'].split('/stops/')[-1].split('/')[0]}] for s in state['stops']]
                                if buttons:
                                    send(chat_id, 'Выбери остановку для удаления:', {'inline_keyboard': buttons})
                                else:
                                    send(chat_id, 'Остановок пока нет.')
                            elif text.startswith('/remove '):
                                index = int(text.split()[1]) - 1
                                if not 0 <= index < len(state['stops']):
                                    raise ValueError('Нет остановки с таким номером.')
                                state['stops'].pop(index)
                                save_profile(state)
                                send(chat_id, 'Остановка удалена.')
                            elif 'https://' in text:
                                send(chat_id, add_stop(state, text), html=True)
                            else:
                                send(chat_id, 'Открой «📍 Остановки» и выбери нужную кнопкой или пришли ссылку для добавления.')
                        except (ValueError, SourceError) as error:
                            send(chat_id, str(error))
                        except Exception as error:
                            logging.warning('Message handler failed: %s', type(error).__name__)
                            send(chat_id, 'Не удалось обработать запрос. Попробуй позже: ошибка не обязательно связана с форматом ссылки.')
                root['offset'] = update['update_id'] + 1
                save(root)
        except Exception as error:
            logging.warning('Polling failed: %s', type(error).__name__)
            time.sleep(5)


if __name__ == '__main__':
    main()

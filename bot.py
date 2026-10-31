import json
import logging
import os
import time
import urllib.request
from pathlib import Path

from yandex import Client, SourceError, format_stop

STATE = Path(os.environ.get('STATE_DIRECTORY', '/var/lib/yandexbus'))
TOKEN = None
SOURCE = Client()
DEFAULT = 'https://yandex.ru/maps/213807/boltino/stops/stop__9675162/?tab=overview'
KEYBOARD = {'keyboard': [[{'text': '🚌 Прибытие'}], [{'text': '📍 Остановки'}], [{'text': '➕ Добавить остановку'}, {'text': '➖ Удалить остановку'}]], 'resize_keyboard': True}


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


def send(chat, text, markup=None):
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
        api('sendMessage', chat_id=chat, text=chunk, reply_markup=markup or KEYBOARD, disable_web_page_preview=True)


def load():
    try:
        state = json.loads((STATE / 'state.json').read_text(encoding='utf-8'))
        state['stops'] = [({'url': s, 'name': 'ЖК Мытищи Лайт' if '9675162' in s else 'Остановка'} if isinstance(s, str) else s) for s in state['stops']]
        for stop in state['stops']:
            stop['url'] = stop['url'].replace('&lang=ru_RU', '')
        return state
    except FileNotFoundError:
        return {'offset': 0, 'owner': None, 'stops': [{'url': DEFAULT, 'name': 'ЖК Мытищи Лайт'}]}


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
        return 'Эта остановка уже добавлена: ' + existing['name']
    if len(state['stops']) >= 10:
        raise ValueError('Лимит — 10 остановок. Удали ненужную через /remove N.')
    snapshot = SOURCE.get(url)
    preview = format_stop(snapshot)
    state['stops'].append({'url': snapshot.url, 'name': snapshot.name})
    save(state)
    return 'Остановка добавлена.\n\n' + preview



def main():
    STATE.mkdir(parents=True, exist_ok=True)
    state = load()
    while True:
        try:
            updates = api('getUpdates', offset=state['offset'], timeout=25, allowed_updates=['message', 'callback_query'])
            for update in updates:
                callback = update.get('callback_query')
                if callback:
                    try:
                        api('answerCallbackQuery', callback_query_id=callback['id'])
                    except Exception:
                        # Telegram may reject an old callback after a restart.
                        logging.warning('Could not acknowledge callback')
                    if callback.get('from', {}).get('id') == state['owner']:
                        data = callback.get('data', '')
                        if data.startswith('delete:'):
                            stop_id = data.split(':', 1)[1]
                            state['stops'] = [s for s in state['stops'] if s['url'].split('/stops/')[-1].split('/')[0] != stop_id]
                            save(state)
                            send(state['owner'], 'Остановка удалена.')
                message = update.get('message', {})
                chat = message.get('chat', {})
                text = message.get('text', '')
                if chat.get('type') == 'private':
                    chat_id = chat['id']
                    if state['owner'] is None and text.startswith('/start'):
                        state['owner'] = chat_id
                        save(state)
                    if state['owner'] != chat_id:
                        send(chat_id, 'Это личный бот. Доступ пока не настроен для этого аккаунта.')
                    else:
                        try:
                            if text.startswith('/start'):
                                send(chat_id, 'Готов! Нажми 🚌 Прибытие. В меню «Остановки» видны сохранённые названия. Добавляй остановки ссылками Яндекс Карт, удаляй кнопкой «Удалить остановку». 🟢 — прогноз, 🕒 — расписание.')
                            elif text in ('🚌 Прибытие', '/bus'):
                                send(chat_id, 'Получаю данные остановок…')
                                if not state['stops']:
                                    send(chat_id, 'Добавь остановку по ссылке Яндекс Карт.')
                                for stop in state['stops']:
                                    try:
                                        snapshot = SOURCE.get(stop['url'])
                                        stop['name'] = snapshot.name
                                        save(state)
                                        send(chat_id, format_stop(snapshot))
                                    except SourceError as error:
                                        send(chat_id, stop['name'] + '\n' + str(error) + '\n' + stop['url'])
                            elif text in ('📍 Остановки', '/stops'):
                                send(chat_id, '\n\n'.join(f'{i+1}. {s["name"]}\n{s["url"]}' for i, s in enumerate(state['stops'])) or 'Остановок пока нет.')
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
                                save(state)
                                send(chat_id, 'Остановка удалена.')
                            elif 'https://' in text:
                                send(chat_id, add_stop(state, text))
                            else:
                                send(chat_id, 'Пришли ссылку на остановку Яндекс Карт или нажми 🚌 Прибытие.')
                        except (ValueError, SourceError) as error:
                            send(chat_id, str(error))
                        except Exception as error:
                            logging.warning('Message handler failed: %s', type(error).__name__)
                            send(chat_id, 'Не удалось обработать запрос. Попробуй позже: ошибка не обязательно связана с форматом ссылки.')
                state['offset'] = update['update_id'] + 1
                save(state)
        except Exception as error:
            logging.warning('Polling failed: %s', type(error).__name__)
            time.sleep(5)


if __name__ == '__main__':
    main()

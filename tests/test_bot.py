import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from yandex import Client, Limited, canonical_url
from test_yandex import NOW, URL, page


class BotTests(unittest.TestCase):
    def run_update(self, state, update):
        # Exit after a single polling batch; no real Telegram requests.
        def telegram(method, **kwargs):
            if method == 'getUpdates':
                if telegram.done:
                    raise SystemExit()
                telegram.done = True
                return [update]
            return True
        telegram.done = False
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(bot, 'STATE', Path(temp)), \
                patch.object(bot, 'load', return_value=state), \
                patch.object(bot, 'save') as save, \
                patch.object(bot, 'send') as send, \
                patch.object(bot, 'api', side_effect=telegram):
            with self.assertRaises(SystemExit):
                bot.main()
            return send, save

    def state(self):
        return {'owner': 42, 'offset': 0, 'stops': [{'url': bot.DEFAULT, 'name': 'Тестовая'}]}

    def message(self, text, user=42):
        return {'update_id': 1, 'message': {'chat': {'id': user, 'type': 'private'}, 'text': text}}

    def test_arrival_error_is_shown_without_old_values(self):
        with patch.object(bot.SOURCE, 'get', side_effect=Limited('Временное ограничение')):
            send, save = self.run_update(self.state(), self.message('🚌 Прибытие'))
        self.assertIn('Временное ограничение', send.call_args.args[1])
        self.assertNotIn('через', send.call_args.args[1])

    def test_unauthorized_account_cannot_remove_stop(self):
        state = self.state()
        self.run_update(state, self.message('/remove 1', user=99))
        self.assertEqual(len(state['stops']), 1)

    def test_delete_button_contains_name_and_stable_identifier(self):
        send, _ = self.run_update(self.state(), self.message('➖ Удалить остановку'))
        button = send.call_args.args[2]['inline_keyboard'][0][0]
        self.assertEqual(button['text'], 'Удалить: Тестовая')
        self.assertEqual(button['callback_data'], 'delete:stop__9675162')

    def test_delete_callback_removes_selected_stop_only(self):
        state = self.state()
        state['stops'].append({'url': bot.DEFAULT.replace('9675162', '123'), 'name': 'Другая'})
        update = {'update_id': 1, 'callback_query': {
            'id': 'callback', 'from': {'id': 42}, 'data': 'delete:stop__9675162'}}
        self.run_update(state, update)
        self.assertEqual([s['name'] for s in state['stops']], ['Другая'])

    def test_add_button_requests_short_or_full_link(self):
        send, _ = self.run_update(self.state(), self.message('➕ Добавить остановку'))
        self.assertIn('короткую или полную', send.call_args.args[1])

    def test_short_and_full_link_add_save_and_deduplicate(self):
        full = 'https://yandex.ru/maps/213/moscow/stops/stop__9681509/?indoorLevel=1&lang=ru_RU&ll=37.662362%2C55.886998&tab=overview&z=19.52'
        short = 'https://yandex.ru/maps/-/CXf7zG1y'
        canonical = canonical_url(full)
        html = page().replace('stop__9675162', 'stop__9681509')
        for first, second in ((short, full), (full, short)):
            with self.subTest(first=first):
                client = Client(fetcher=lambda url: (html, canonical), clock=lambda: NOW)
                state = self.state()
                with patch.object(bot, 'SOURCE', client), patch('bot.format_stop', return_value='Данные остановки'):
                    send, save = self.run_update(state, self.message(first))
                    self.assertIn('Остановка добавлена', send.call_args.args[1])
                    self.assertEqual(state['stops'][-1]['url'], canonical)
                    self.assertEqual(state['stops'][-1]['name'], 'Тестовая остановка')
                    save.assert_called()
                    send, _ = self.run_update(state, self.message(second))
                    self.assertIn('уже добавлена', send.call_args.args[1])
                    self.assertEqual(len(state['stops']), 2)

    def test_existing_stop_at_limit_is_reported_as_duplicate(self):
        state = self.state()
        state['stops'] *= 10
        send, _ = self.run_update(state, self.message(URL))
        self.assertIn('уже добавлена', send.call_args.args[1])

    def test_source_failure_does_not_save_stop(self):
        state = self.state()
        other = URL.replace('9675162', '9681509')
        with patch.object(bot.SOURCE, 'get', side_effect=Limited('Временное ограничение')):
            send, _ = self.run_update(state, self.message(other))
        self.assertEqual(len(state['stops']), 1)
        self.assertIn('Временное ограничение', send.call_args.args[1])


if __name__ == '__main__':
    unittest.main()

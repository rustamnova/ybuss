import datetime as dt
import json
import unittest
from unittest.mock import patch

from yandex import Client, Limited, SafeRedirect, SourceError, canonical_url, format_stop, parse_page

URL = 'https://yandex.ru/maps/213807/boltino/stops/stop__9675162/?tab=overview'
NOW = dt.datetime(2026, 10, 31, 20, 58, tzinfo=dt.timezone.utc).timestamp()


def page(now=NOW, events=None):
    if events is None:
        events = [
            {'Scheduled': {'value': str(now + 180)}, 'Estimated': {'value': str(now + 420)}},
            {'Scheduled': {'value': str(now + 1200)}},
        ]
    state = {'config': {'masstransitStop': {
        'id': 'stop__9675162', 'name': 'Тестовая остановка', 'currentTime': now * 1000,
        'tzOffset': 10800, 'transports': [{
            'name': '509', 'threads': [{
                'EssentialStops': [{'name': 'Метро', 'info': {'lastStop': True}}],
                'BriefSchedule': {'Events': events},
            }],
        }],
    }}}
    return '<script class="state-view" type="application/json">' + json.dumps(state) + '</script>'


class SourceTests(unittest.TestCase):
    def test_live_estimate_wins_over_schedule_and_formats_midnight(self):
        stop = parse_page(page(), URL, NOW)
        result = format_stop(stop, NOW)
        self.assertIn('00:05 · через 7 мин · 🟢 прогноз', result)
        self.assertIn('00:18 · через 20 мин · 🕒 расписание', result)
        self.assertNotIn('≈', result)
        self.assertEqual(stop.routes[0].destination, 'Метро')

    def test_schedule_is_not_presented_as_live(self):
        html = page(events=[{'Scheduled': {'value': str(NOW + 120)}}])
        result = format_stop(parse_page(html, URL, NOW), NOW)
        self.assertIn('Живого прогноза сейчас нет', result)
        self.assertNotIn('🟢', result)

    def test_expired_vehicle_removed(self):
        html = page(events=[{'Estimated': {'value': str(NOW - 60)}}])
        result = format_stop(parse_page(html, URL, NOW), NOW)
        self.assertIn('Нет ближайших прибытий', result)
        self.assertNotIn('через 0 мин', result)

    def test_limited_and_schema_errors(self):
        for html in ('limited', '<html><pre>limited</pre></html>'):
            with self.assertRaises(Limited):
                parse_page(html, URL, NOW)
        with self.assertRaises(SourceError):
            parse_page('<html>captcha or missing data</html>', URL, NOW)

    def test_stale_and_future_source_data_rejected(self):
        for source in (NOW - 181, NOW + 61):
            with self.assertRaises(SourceError):
                parse_page(page(source), URL, NOW)
        with self.assertRaises(SourceError):
            format_stop(parse_page(page(), URL, NOW), NOW + 61)

    def test_wrong_stop_rejected(self):
        with self.assertRaises(SourceError):
            parse_page(page(), URL.replace('9675162', '123'), NOW)

    def test_service_threads_are_merged_by_route_and_direction(self):
        html = page()
        state = json.loads(html.split('>', 1)[1].split('</script>')[0])
        transport = state['config']['masstransitStop']['transports'][0]
        thread = json.loads(json.dumps(transport['threads'][0]))
        thread['BriefSchedule']['Events'] = [{'Scheduled': {'value': str(NOW + 60)}}]
        transport['threads'].append(thread)
        html = '<script type="application/json" class="state-view">' + json.dumps(state) + '</script>'
        stop = parse_page(html, URL, NOW)
        self.assertEqual(len(stop.routes), 1)
        self.assertEqual(len(stop.routes[0].arrivals), 3)
        self.assertEqual(stop.routes[0].arrivals[0].timestamp, NOW + 60)

    def test_cache_recomputes_wait_and_expires(self):
        clock = [NOW]
        calls = []

        def fetch(url):
            calls.append(url)
            return page(clock[0]), url

        client = Client(fetcher=fetch, clock=lambda: clock[0])
        first = client.get(URL)
        clock[0] += 10
        self.assertIs(client.get(URL), first)
        self.assertIn('через 7 мин', format_stop(first, clock[0]))
        clock[0] += 21
        self.assertIsNot(client.get(URL), first)
        self.assertEqual(len(calls), 2)

    def test_limited_stops_retries_for_all_stops(self):
        calls = []
        mono = [100]

        def fetch(url):
            calls.append(url)
            raise Limited('limited')

        client = Client(fetcher=fetch, clock=lambda: NOW, monotonic=lambda: mono[0])
        for url in (URL, URL.replace('9675162', '123')):
            with self.assertRaises(Limited):
                client.get(url)
        self.assertEqual(len(calls), 1)
        mono[0] += 301
        with self.assertRaises(Limited):
            client.get(URL)
        self.assertEqual(len(calls), 2)

    def test_expired_cache_not_served_when_source_fails(self):
        clock = [NOW]
        client = Client(fetcher=lambda url: (page(), url), clock=lambda: clock[0])
        client.get(URL)
        clock[0] += 31
        with patch.object(client, 'fetcher', side_effect=Limited('limited')):
            with self.assertRaises(Limited):
                client.get(URL)

    def test_url_validation_and_canonical_deduplication(self):
        self.assertEqual(canonical_url(URL + '&lang=ru_RU&z=19#x'), URL)
        for url in ('http://127.0.0.1/maps/', 'https://yandex.ru.evil.test/maps/',
                    'https://yandex.ru@localhost/maps/', 'https://yandex.ru:443/maps/',
                    'https://yandex.ru/maps/?x=1'):
            with self.assertRaises(ValueError):
                canonical_url(url)
        with self.assertRaises(SourceError):
            SafeRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/')

    def test_share_link_uses_one_response_and_populates_cache(self):
        calls = []
        def fetch(url):
            calls.append(url)
            return page(), URL
        client = Client(fetcher=fetch, clock=lambda: NOW)
        url = client.resolve('https://yandex.ru/maps/-/example')
        self.assertEqual(client.get(url).name, 'Тестовая остановка')
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()

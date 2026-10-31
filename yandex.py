"""Read the public stop page's embedded state; no browser or private API."""

import datetime as dt
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser

HEADERS = {
    'User-Agent': 'Ybuss/0.1 (+https://github.com/rustamnova/ybuss)',
    'Accept-Language': 'ru-RU,ru;q=0.9',
    'Cache-Control': 'no-cache',
}
# yandex.ru redirects this server through yandex.eu to yandex.com.
HOSTS = {'yandex.ru', 'www.yandex.ru', 'yandex.com', 'www.yandex.com', 'yandex.eu', 'www.yandex.eu'}
MAX_BYTES = 4 * 1024 * 1024


class SourceError(Exception):
    """A safe-to-display upstream error, without request credentials."""


class Limited(SourceError):
    pass


def allowed_url(url):
    try:
        p = urllib.parse.urlsplit(url)
        return (p.scheme == 'https' and p.hostname in HOSTS and not p.port
                and not p.username and not p.password and p.path.startswith('/maps/'))
    except ValueError:
        return False


def canonical_url(url):
    if not allowed_url(url):
        raise ValueError('Нужна HTTPS-ссылка на остановку Яндекс Карт.')
    p = urllib.parse.urlsplit(url)
    if not re.fullmatch(r'/maps/\d+/[^/]+/stops/[\w-]+/?', p.path):
        raise ValueError('В полной ссылке на остановку должен быть раздел /stops/.')
    return urllib.parse.urlunsplit(('https', 'yandex.ru', p.path.rstrip('/') + '/', 'tab=overview', ''))


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.hostname in HOSTS and parsed.path.startswith(('/showcaptcha', '/checkcaptcha')):
            raise Limited('Яндекс запросил проверку доступа. Повтори через несколько минут.')
        if not allowed_url(newurl):
            raise SourceError('Яндекс перенаправил запрос вне страницы Карт. Данные недоступны.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(url):
    if not allowed_url(url):
        raise ValueError('Разрешены только ссылки на Яндекс Карты.')
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.build_opener(SafeRedirect()).open(request, timeout=20) as response:
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise SourceError('Ответ Яндекса слишком большой.')
            return body.decode('utf-8'), response.url
    except urllib.error.HTTPError as error:
        if error.code in (403, 429):
            raise Limited('Яндекс временно ограничил запросы. Повтори через несколько минут.') from None
        raise SourceError(f'Яндекс ответил HTTP {error.code}. Попробуй позже.') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SourceError('Не удалось связаться с Яндексом. Попробуй позже.') from None


class StateParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script':
            self.active = (attrs.get('type') == 'application/json'
                           and 'state-view' in attrs.get('class', '').split())

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


@dataclass(frozen=True)
class Arrival:
    timestamp: float
    live: bool


@dataclass(frozen=True)
class Route:
    number: str
    destination: str
    arrivals: tuple


@dataclass(frozen=True)
class Stop:
    name: str
    url: str
    source_time: float
    fetched_at: float
    tz_offset: int
    routes: tuple


def parse_page(html, url, fetched_at=None):
    fetched_at = time.time() if fetched_at is None else fetched_at
    if re.search(r'>\s*limited\s*<', html, re.I) or html.strip().lower() == 'limited':
        raise Limited('Яндекс временно ограничил запросы. Повтори через несколько минут.')
    parser = StateParser()
    parser.feed(html)
    try:
        state = json.loads(''.join(parser.parts))
        stop = state['config']['masstransitStop']
        source_time = float(stop['currentTime']) / 1000
        tz_offset = int(stop['tzOffset'])
        name = str(stop['name'])
        transports = stop['transports']
        if not name or not isinstance(transports, list) or not math.isfinite(source_time):
            raise ValueError()
        dt.timezone(dt.timedelta(seconds=tz_offset))
        expected_id = urllib.parse.urlsplit(url).path.rstrip('/').split('/')[-1]
        if str(stop['id']) != expected_id:
            raise ValueError()
    except (KeyError, ValueError, TypeError, OverflowError):
        raise SourceError('В ответе Яндекса нет распознаваемых данных остановки.') from None
    if fetched_at - source_time > 180 or source_time - fetched_at > 60:
        raise SourceError('Яндекс вернул устаревшие данные или часы сервера неверны. Повтори позже.')
    routes = []
    try:
        for transport in transports:
            for thread in transport.get('threads', []):
                if thread.get('noBoarding'):
                    continue
                endpoints = thread.get('EssentialStops', [])
                last = next((s for s in reversed(endpoints) if s.get('info', {}).get('lastStop')), None)
                destination = str(last.get('name', '')) if last else ''
                events = []
                for event in thread.get('BriefSchedule', {}).get('Events', []):
                    live = bool(event.get('Estimated', {}).get('value'))
                    value = event.get('Estimated' if live else 'Scheduled', {}).get('value')
                    if value is None:
                        continue
                    timestamp = float(value)
                    if math.isfinite(timestamp) and timestamp >= fetched_at - 30:
                        events.append(Arrival(timestamp, live))
                events.sort(key=lambda e: e.timestamp)
                routes.append(Route(str(transport['name']), destination, tuple(events[:3])))
    except (KeyError, TypeError, ValueError):
        raise SourceError('Формат прогноза Яндекса изменился. Данные не показаны.') from None
    # One line can have several service threads with the same destination.
    # Merge those before selecting the three nearest departures.
    grouped = {}
    for route in routes:
        key = (route.number, route.destination)
        events = grouped.setdefault(key, {})
        for event in route.arrivals:
            old = events.get(event.timestamp)
            if old is None or event.live:
                events[event.timestamp] = event
    routes = [Route(number, destination, tuple(sorted(events.values(), key=lambda e: e.timestamp)[:3]))
              for (number, destination), events in grouped.items()]
    routes.sort(key=lambda route: route.arrivals[0].timestamp if route.arrivals else float('inf'))
    return Stop(name, canonical_url(url), source_time, fetched_at, tz_offset, tuple(routes))


class Client:
    def __init__(self, ttl=30, cooldown=300, fetcher=fetch_page, clock=time.time, monotonic=time.monotonic):
        self.ttl = ttl
        self.cooldown = cooldown
        self.fetcher = fetcher
        self.clock = clock
        self.monotonic = monotonic
        self.cache = {}
        self.blocked_until = 0

    def get(self, url):
        url = canonical_url(url)
        cached = self.cache.get(url)
        now = self.clock()
        if cached and 0 <= now - cached.fetched_at < self.ttl:
            return cached
        if self.monotonic() < self.blocked_until:
            raise Limited('Пауза после ограничения Яндекса. Повтори через несколько минут.')
        try:
            html, final_url = self.fetcher(url)
            stop = parse_page(html, final_url, self.clock())
        except Limited:
            self.blocked_until = self.monotonic() + self.cooldown
            raise
        self.cache[url] = stop
        return stop

    def resolve(self, text):
        match = re.search(r'https://[^\s<>]+', text)
        if not match:
            raise ValueError('Пришли ссылку на остановку Яндекс Карт.')
        url = match.group().rstrip(').,')
        if not allowed_url(url):
            raise ValueError('Нужна HTTPS-ссылка на Яндекс Карты.')
        if urllib.parse.urlsplit(url).path.startswith('/maps/-/'):
            if self.monotonic() < self.blocked_until:
                raise Limited('Пауза после ограничения Яндекса. Повтори позже.')
            try:
                html, final_url = self.fetcher(url)
                stop = parse_page(html, final_url, self.clock())
            except Limited:
                self.blocked_until = self.monotonic() + self.cooldown
                raise
            self.cache[stop.url] = stop
            return stop.url
        return canonical_url(url)


def format_stop(stop, now=None, html=False):
    now = time.time() if now is None else now
    if now - stop.fetched_at > 60 or now - stop.source_time > 180 or stop.source_time - now > 60:
        raise SourceError('Данные устарели. Запроси прибытие ещё раз.')
    zone = dt.timezone(dt.timedelta(seconds=stop.tz_offset))
    lines = ["📍 <b>" + escape(stop.name) + "</b>" if html else stop.name]
    live_count = 0
    count = 0
    for route in stop.routes:
        if not html:
            lines.extend(['', '🚌 ' + route.number + (' → ' + route.destination if route.destination else '')])
        upcoming = [event for event in route.arrivals if event.timestamp >= now - 30]
        if not upcoming:
            if html:
                lines.extend(['', '🚌 <b>' + escape(route.number) + '</b>',
                              '<i>' + escape(route.destination) + ' · нет ближайших прибытий</i>'])
            else:
                lines.append('  Нет ближайших прибытий в ответе Яндекса')
        for event in upcoming:
            count += 1
            live_count += int(event.live)
            clock = dt.datetime.fromtimestamp(event.timestamp, zone).strftime('%H:%M')
            minutes = max(0, math.ceil((event.timestamp - now) / 60))
            kind = '🟢 прогноз' if event.live else '🕒 расписание'
            if not html:
                lines.append(f'  {clock} · через {minutes} мин · {kind}')
        if html and upcoming:
            first = upcoming[0]
            wait = max(0, math.ceil((first.timestamp - now) / 60))
            marker = '🟢' if first.live else '🕒'
            clock = dt.datetime.fromtimestamp(first.timestamp, zone).strftime('%H:%M')
            lines.extend(['', f'🚌 <b>{escape(route.number)}</b>   ·   {marker} <b>через {wait} мин</b>',
                          '<i>' + escape(('→ ' + route.destination + ' · ') if route.destination else '') + f'в {clock}</i>'])
            following = []
            for event in upcoming[1:]:
                wait = max(0, math.ceil((event.timestamp - now) / 60))
                clock = dt.datetime.fromtimestamp(event.timestamp, zone).strftime('%H:%M')
                marker = '🟢' if event.live else '🕒'
                following.append(f'{marker} {wait} мин ({clock})')
            if following:
                lines.append('Далее: ' + ' · '.join(following))
    if not stop.routes:
        lines.append('\nВ ответе Яндекса нет маршрутов для посадки.')
    elif count and not live_count:
        lines.append('\nЖивого прогноза сейчас нет; показано расписание.')
    stamp = dt.datetime.fromtimestamp(stop.fetched_at, zone).strftime('%H:%M:%S')
    offset = stop.tz_offset / 3600
    label = 'МСК' if stop.tz_offset == 10800 else f'UTC{offset:+g}'
    if html:
        lines.extend(['', f'<i>Получено: {stamp} {label}</i>', '<i>🟢 прогноз · 🕒 расписание</i>',
                      '<a href="' + escape(stop.url, quote=True) + '">Открыть в Яндекс Картах</a>'])
    else:
        lines.extend(['', f'Получено: {stamp} {label} · Яндекс Карты', stop.url])
    return '\n'.join(lines)

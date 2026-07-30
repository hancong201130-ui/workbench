#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定时抓取最新财经 / 新闻 / 音乐榜单，输出静态 JSON 供 GitHub Pages 同域读取。
- 在 GitHub Actions 服务器端运行，不受大陆 CORS / 代理封锁影响。
- 仅依赖 Python 标准库，无需 pip 安装。
- 每个数据源独立抓取；失败则保留旧文件，保证数据连续性。
输出文件（仓库根目录）：
  finance.json        {updated, items:[{title,summary,source,time,tag}]}
  news.json           {updated, items:[{title,summary,source,time,tag}]}
  music-spotify.json  {updated, songs:[{rank,title,artist,platform}]}
  music-billboard.json{updated, songs:[{rank,title,artist,platform}]}
"""
import os
import re
import json
import html
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根目录
TZ8 = timezone(timedelta(hours=8))  # 北京时间


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ('utf-8', 'gbk', 'gb18030'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'ignore')


def clean(s):
    if not s:
        return ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def fmt_ts(ts):
    """新浪 ctime 为秒级时间戳，转北京时间字符串。"""
    try:
        ts = int(ts)
        if ts <= 0:
            return ''
        d = datetime.fromtimestamp(ts, tz=TZ8)
        return d.strftime('%Y/%m/%d %H:%M')
    except Exception:
        return ''


def fmt_rfc822(s):
    """RSS pubDate 如 'Thu, 30 Jul 2026 18:05:49 +0800'，转北京时间字符串。"""
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=TZ8)
        else:
            d = d.astimezone(TZ8)
        return d.strftime('%Y/%m/%d %H:%M')
    except Exception:
        return clean(s)


def fetch_finance(n=15):
    url = f'https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&k=&num={n}&order=1'
    d = json.loads(get(url))
    arr = (d.get('result') or {}).get('data') or []
    tmp = []
    for it in arr:
        title = clean(it.get('title') or it.get('stitle') or '')
        if not title:
            continue
        intro = clean(it.get('intro') or it.get('summary') or it.get('wapsummary') or '')
        if len(intro) > 110:
            intro = intro[:110] + '…'
        kw = clean(it.get('keywords') or '')
        ts = int(it.get('ctime') or 0)
        tmp.append((ts, {
            'title': title,
            'summary': intro,
            'source': clean(it.get('media_name') or '新浪财经'),
            'time': fmt_ts(ts),
            'tag': (kw.split(',')[0] if kw else '资讯'),
        }))
    tmp.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in tmp][:n]


def fetch_news(n=15):
    x = get('https://www.chinanews.com.cn/rss/world.xml')
    root = ET.fromstring(x)
    items = []
    for it in root.findall('.//item'):
        title = clean(it.findtext('title'))
        if not title:
            continue
        desc = clean(it.findtext('description'))
        if len(desc) > 110:
            desc = desc[:110] + '…'
        cat = clean(it.findtext('category')) or '国际'
        items.append({
            'title': title,
            'summary': desc,
            'source': '中国新闻网',
            'time': fmt_rfc822(it.findtext('pubDate') or ''),
            'tag': cat,
        })
    return items[:n]


def fetch_spotify(n=20):
    h = get('https://kworb.net/spotify/country/global_weekly.html')
    rows = re.findall(r'<tr>[\s\S]*?<\/tr>', h)
    out, rank = [], 0
    for r in rows:
        div = re.search(r'<td[^>]*>.*?<div>([\s\S]*?)<\/div>', r)
        if not div:
            continue
        links = re.findall(r'<a[^>]*>([^<]+)<\/a>', div.group(1))
        if len(links) < 2:
            continue
        rank += 1
        out.append({
            'rank': rank,
            'title': clean(links[1]),
            'artist': clean(links[0]),
            'platform': 'Spotify Global',
        })
        if rank >= n:
            break
    return out


def fetch_billboard(n=20):
    h = get('https://www.billboard.com/charts/hot-100/')
    parts = h.split('a-chart-result-item-container')
    out = []
    for r in parts[1:]:
        mt = re.search(r'class="[^"]*c-title[^"]*"[^>]*>([\s\S]*?)<\/h3>', r)
        sp = re.search(r'class="[^"]*c-label\s+a-no-trucate[^"]*"[^>]*>([\s\S]*?)<\/span>', r)
        if not mt or not sp:
            continue
        artist = clean(sp.group(1))
        if not artist:
            continue
        out.append({
            'rank': len(out) + 1,
            'title': clean(mt.group(1)),
            'artist': artist,
            'platform': 'Billboard Hot 100',
        })
        if len(out) >= n:
            break
    return out


def write_json(name, payload):
    path = os.path.join(REPO, name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    cnt = len(payload.get('items') or payload.get('songs') or [])
    print(f'wrote {name} ({cnt} items)')


def guard_fetch(label, fn, name, key):
    """抓取并写文件；失败则保留旧文件，不覆盖已有时效数据。"""
    try:
        data = fn()
        if not data:
            raise ValueError('empty result')
        stamp = datetime.now(TZ8).strftime('%Y/%m/%d %H:%M')
        write_json(name, {'updated': stamp, key: data})
        return True
    except Exception as e:
        print(f'[WARN] {label} fetch failed: {e} (保留旧文件)')
        return False


if __name__ == '__main__':
    ok_f = guard_fetch('finance', lambda: fetch_finance(15), 'finance.json', 'items')
    ok_n = guard_fetch('news', lambda: fetch_news(15), 'news.json', 'items')
    ok_s = guard_fetch('spotify', lambda: fetch_spotify(20), 'music-spotify.json', 'songs')
    ok_b = guard_fetch('billboard', lambda: fetch_billboard(20), 'music-billboard.json', 'songs')
    print('DONE', {'finance': ok_f, 'news': ok_n, 'spotify': ok_s, 'billboard': ok_b})

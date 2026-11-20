"""豆瓣检索响应适配；搜索空结果、网页未渲染与候选拒绝分别记录。"""
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit
import re


def lookup_error_reason(exc):
    """保留可行动的原因，不把异常类名、请求凭据或整段 HTTP 响应交给用户。"""
    name = type(exc).__name__
    if 'Timeout' in name:
        return '资料服务响应超时，本次未取得结果'
    response = getattr(exc, 'response', None)
    if response is not None:
        return f'资料服务返回 HTTP {response.status_code}，未取得有效资料'
    message = str(exc)
    if isinstance(exc, ValueError) and re.match(r'^(豆瓣详情|详情页|详情缺少|候选列表|搜索响应|HTTP \d)', message):
        return message[:160]
    if name in ('ConnectError', 'ProxyError', 'NetworkError'):
        return '无法连接资料服务，请检查服务端网络'
    if isinstance(exc, ValueError):
        return '返回内容无法解析为作品资料'
    return '资料读取未完成，请展开诊断查看失败渠道'


def search_items(payload):
    if isinstance(payload, list):
        if not all(isinstance(item, dict) for item in payload):
            raise ValueError("候选列表结构不完整")
        return payload
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "list"):
            if key in payload:
                return search_items(payload[key])
    raise ValueError("搜索响应不是候选列表")


def normalize_detail(payload):
    if not isinstance(payload, dict):
        return {}
    detail = dict(payload)
    for _ in range(3):
        nested = next((detail[k] for k in ('data', 'media_info', 'subject') if isinstance(detail.get(k), dict)), None)
        if nested is None:
            break
        detail = dict(nested)
    detail["title"] = detail.get("title") or detail.get("name") or ""
    count = detail.get("number_of_episodes") or detail.get("episodes") or detail.get('episode_count') or detail.get('total_episodes')
    if isinstance(count, (str, int)) and str(count).isdigit():
        detail["number_of_episodes"] = int(count)
    return detail


def subject_page_detail(html, subject_id):
    """仅解析豆瓣公开详情中明确呈现的标题、年份与集数；不推断播毕。"""
    if any(marker in html for marker in ("验证码", "异常请求", "captcha", "访问受限")):
        raise ValueError("豆瓣详情要求验证")
    title = re.search(r'<[^>]+property=[\'"]v:itemreviewed[\'"][^>]*>(.*?)</', html, re.S)
    info = re.search(r'<div[^>]+id=[\'"]info[\'"][^>]*>(.*?)</div>', html, re.S)
    if not title or not info:
        raise ValueError("详情页未返回可读取的作品资料")
    def plain(value):
        return unescape(re.sub(r'<[^>]+>', ' ', value)).strip()
    info_text = plain(info[1])
    count = re.search(r'集数\s*[:：]\s*(\d+)', info_text)
    year = re.search(r'(?:首播|上映日期)\s*[:：]\s*((?:19|20)\d{2})', info_text)
    if not count or not year:
        raise ValueError("详情缺少本季集数或首播年份")
    return {"title":plain(title[1]), "year":year[1], "number_of_episodes":int(count[1]),
            "douban_id":str(subject_id), "type":"电视剧", "status":"",
            "source_url":f"https://movie.douban.com/subject/{subject_id}/"}


def search_page_candidates(html):
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.candidates = {}
            self.subject = None
            self.label = []

        def handle_starttag(self, tag, attrs):
            if tag != "a":
                return
            attrs = dict(attrs)
            href = attrs.get("href", "")
            parsed = urlsplit(href)
            # 豆瓣搜索跳转链接只解析其目标，不发起跳转请求。
            if parsed.hostname and parsed.hostname.endswith(".douban.com"):
                href = (parse_qs(parsed.query).get("url") or [href])[0]
            parsed = urlsplit(unquote(href))
            m = re.fullmatch(r"/subject/(\d+)/?", parsed.path)
            if parsed.hostname == "movie.douban.com" and m:
                self.subject = m[1]
                self.label = [attrs.get("title", "")]

        def handle_data(self, data):
            if self.subject:
                self.label.append(data)

        def handle_endtag(self, tag):
            if tag == "a" and self.subject:
                title = " ".join("".join(self.label).split())
                if title or self.subject not in self.candidates:
                    self.candidates[self.subject] = title
                self.subject = None

    parser = Parser()
    parser.feed(html)
    if parser.candidates:
        return parser.candidates, "returned"
    if any(marker in html for marker in ("验证码", "异常请求", "检测到有异常请求", "captcha", "访问受限")):
        return {}, "blocked"
    if any(marker in html for marker in ("没有找到", "没有搜索到", "暂无搜索结果")):
        return {}, "empty"
    return {}, "unavailable"  # 动态壳页/登录页不等于豆瓣未收录。

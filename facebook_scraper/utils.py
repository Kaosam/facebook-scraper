import codecs
import re
from datetime import datetime, timedelta
import calendar
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import dateparser
import lxml.html
from bs4 import BeautifulSoup
from requests.cookies import RequestsCookieJar
from requests_html import DEFAULT_URL, Element, PyQuery
import json

from . import exceptions
import time
import logging

from pathlib import Path
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)
logFolder = Path("")
logFile = logFolder / "facebook.log"
fileHandler = RotatingFileHandler(logFile, maxBytes=1000000, backupCount=10)
logger.addHandler(fileHandler)
logger.setLevel(logging.DEBUG)


def find_and_search(node, selector, pattern, cast=str):
    container = node.find(selector, first=True)
    match = container and pattern.search(container.html)
    return match and cast(match.groups()[0])


def parse_int(value: str) -> int:
    return int(''.join(filter(lambda c: c.isdigit(), value)))


def convert_numeric_abbr(s):
    mapping = {'k': 1000, 'm': 1e6}
    s = s.replace(",", "")
    if s[-1].isalpha():
        return int(float(s[:-1]) * mapping[s[-1].lower()])
    return int(s)


def parse_duration(s) -> int:
    match = re.search(r'T(?P<hours>\d+H)?(?P<minutes>\d+M)?(?P<seconds>\d+S)', s)
    if match:
        result = 0
        for k, v in match.groupdict().items():
            if v:
                if k == 'hours':
                    result += int(v.strip("H")) * 60 * 60
                elif k == "minutes":
                    result += int(v.strip("M")) * 60
                elif k == "seconds":
                    result += int(v.strip("S"))
        return result


def decode_css_url(url: str) -> str:
    url = re.sub(r'\\(..) ', r'\\x\g<1>', url)
    url, _ = codecs.unicode_escape_decode(url)
    url, _ = codecs.unicode_escape_decode(url)
    return url


def filter_query_params(url, whitelist=None, blacklist=None) -> str:
    def is_valid_param(param):
        if whitelist is not None:
            return param in whitelist
        if blacklist is not None:
            return param not in blacklist
        return True  # Do nothing

    parsed_url = urlparse(url)
    query_params = parse_qsl(parsed_url.query)
    query_string = urlencode([(k, v) for k, v in query_params if is_valid_param(k)])
    return urlunparse(parsed_url._replace(query=query_string))


def remove_control_characters(html):
    """
    Strip invalid XML characters that `lxml` cannot parse.
    """
    # See: https://github.com/html5lib/html5lib-python/issues/96
    #
    # The XML 1.0 spec defines the valid character range as:
    # Char ::= #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
    #
    # We can instead match the invalid characters by inverting that range into:
    # InvalidChar ::= #xb | #xc | #xFFFE | #xFFFF | [#x0-#x8] | [#xe-#x1F] | [#xD800-#xDFFF]
    #
    # Sources:
    # https://www.w3.org/TR/REC-xml/#charsets,
    # https://lsimons.wordpress.com/2011/03/17/stripping-illegal-characters-out-of-xml-in-python/
    def strip_illegal_xml_characters(s, default, base=10):
        # Compare the "invalid XML character range" numerically
        n = int(s, base)
        if (
            n in (0xB, 0xC, 0xFFFE, 0xFFFF)
            or 0x0 <= n <= 0x8
            or 0xE <= n <= 0x1F
            or 0xD800 <= n <= 0xDFFF
        ):
            return ""
        return default

    # We encode all non-ascii characters to XML char-refs, so for example "💖" becomes: "&#x1F496;"
    # Otherwise we'd remove emojis by mistake on narrow-unicode builds of Python
    html = html.encode("ascii", "xmlcharrefreplace").decode("utf-8")
    html = re.sub(
        r"&#(\d+);?", lambda c: strip_illegal_xml_characters(c.group(1), c.group(0)), html
    )
    html = re.sub(
        r"&#[xX]([0-9a-fA-F]+);?",
        lambda c: strip_illegal_xml_characters(c.group(1), c.group(0), base=16),
        html,
    )
    # A regex matching the "invalid XML character range"
    html = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1F\uD800-\uDFFF\uFFFE\uFFFF]").sub("", html)
    return html


def make_html_element(html: str, url=DEFAULT_URL) -> Element:
    html = remove_control_characters(html)
    pq_element = PyQuery(html)[0]  # PyQuery is a list, so we take the first element
    return Element(element=pq_element, url=url)

monthNames = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12
}

month = (
    r"gen(?:naio)?|"
    r"feb(?:braio)?|"
    r"mar(?:zo)?|"
    r"apr(?:ile)?|"
    r"mag(?:gio)?|"
    r"giu(?:gno)?|"
    r"lug(?:lio)?|"
    r"ago(?:sto)?|"
    r"set(?:tembre)?|"
    r"ott(?:tobre)?|"
    r"nov(?:embre)?|"
    r"dic(?:embre)?"
)
day_of_month = r"\d{1,2}"
specific_date = f"{day_of_month} (?:{month})"
specific_date_full = f"{day_of_month} (?:{month})" + r"(?:,? \d{4})?"

date = f"{specific_date}"
dateFull = f"{specific_date_full}"

hour = r"\d{2}"
minute = r"\d{2}"

exact_time = f"(?:{date}) alle ore {hour}:{minute}"
exact_time_past_years = f"(?:{dateFull}) alle ore {hour}:{minute}"
relative_time_days = r'\b\d{1} g'
relative_time_hours = r"\b\d{1,2} h"

datetime_regex1 = re.compile(fr"({exact_time})", re.IGNORECASE)
datetime_regex2 = re.compile(fr"({relative_time_days})", re.IGNORECASE)
datetime_regex3 = re.compile(fr"({relative_time_hours})", re.IGNORECASE)
datetime_regex4 = re.compile(fr"({exact_time_past_years})", re.IGNORECASE)


def parse_datetime(text: str, search=True) -> Optional[datetime]:    
    if search:
        logger.debug(f"Parsing date {text}")
        if datetime_regex1.search(text):
            day = int(text.split(" ")[0])
            month = monthNames[text.split(" ")[1]]
            year = 2021
            hour = int(text.split(" ")[4].split(":")[0])
            minute = int(text.split(" ")[4].split(":")[1])
            return datetime(year, month, day, hour, minute)
        if datetime_regex2.search(text):
            return None
            # la data con i giorni (esempio "7 g") non viene presa mai, ma convertita nel primo caso
        if datetime_regex3.search(text):
            hours_ago = int(text.split(" ")[0])
            d = datetime.today() - timedelta(hours=hours_ago)
            return d
        if datetime_regex4.search(text):
            day = int(text.split(" ")[0])
            month = monthNames[text.split(" ")[1]]
            year = int(text.split(" ")[2])
            hour = int(text.split(" ")[5].split(":")[0])
            minute = int(text.split(" ")[5].split(":")[1])
            return datetime(year, month, day, hour, minute)
            
    result = dateparser.parse(text)
    if result:
        return result.replace(microsecond=0)
    return None


def html_element_to_string(element: Element, pretty=False) -> str:
    html = lxml.html.tostring(element.element, encoding='unicode')
    if pretty:
        html = BeautifulSoup(html, features='html.parser').prettify()
    return html


def parse_cookie_file(filename: str) -> RequestsCookieJar:
    jar = RequestsCookieJar()

    with open(filename, mode='rt') as file:
        data = file.read()

    try:
        data = json.loads(data)
        if type(data) is list:
            for c in data:
                expires = c.get("expirationDate") or c.get("Expires raw")
                if expires:
                    expires = int(expires)
                if "Name raw" in c:
                    # Cookie Quick Manager JSON format
                    host = c["Host raw"].replace("https://", "").strip("/")
                    jar.set(
                        c["Name raw"],
                        c["Content raw"],
                        domain=host,
                        path=c["Path raw"],
                        expires=expires,
                    )
                else:
                    # EditThisCookie JSON format
                    jar.set(
                        c["name"],
                        c["value"],
                        domain=c["domain"],
                        path=c["path"],
                        secure=c["secure"],
                        expires=expires,
                    )
        elif type(data) is dict:
            for k, v in data.items():
                if type(v) is dict:
                    jar.set(k, v["value"])
                else:
                    jar.set(k, v)
    except json.decoder.JSONDecodeError:
        # Netscape format
        for line in data.splitlines():
            line = line.strip()
            if line == "" or line.startswith('#'):
                continue

            domain, _, path, secure, expires, name, value = line.split('\t')
            secure = secure.lower() == 'true'
            expires = None if expires == '0' else int(expires)

            jar.set(name, value, domain=domain, path=path, secure=secure, expires=expires)

    return jar


def safe_consume(generator, sleep=0):
    result = []
    try:
        for item in generator:
            result.append(item)
            time.sleep(sleep)
    except Exception as e:
        logger.error(f"Exception when consuming {generator}: {type(e)}: {str(e)}")
    return result

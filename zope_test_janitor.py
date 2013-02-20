#!/usr/bin/python3
"""
Usage: zope-test-janitor < email.txt

Pipe an email from the Zope tests summarizer to it, get back an HTML report.
"""

import argparse
import doctest
import fileinput
import html
import os
import re
import sys
import tempfile
import time
import unittest
import webbrowser
from urllib.request import urlopen
from urllib.parse import quote, urljoin
from urllib.error import HTTPError

import lxml.html


DATE_PATTERN = re.compile(
    r'^Date: (.*)$')

TITLE_PATTERN = re.compile(
    r'^\[\d+\]\s*([A-Z].*)')

URL_PATTERN = re.compile(
    r'^\s+(https://mail.zope.org/pipermail/zope-tests/.*\.html)')

JENKINS_URL = re.compile(
    r'.*/job/[^/]+/\d+/$')

BUILDER_URL = re.compile(
    r'.*/builders/[^/]+/builds/-?\d+$')


CACHE_DIR = os.path.expanduser('~/.cache/zope-test-janitor')


def cache_filename(url):
    return os.path.join(CACHE_DIR, quote(url, safe=''))


ONE_DAY = 24*60*60 # seconds


def get_from_cache(filename, max_age):
    try:
        with open(filename, 'rb') as f:
            mtime = os.fstat(f.fileno()).st_mtime
            if time.time() - mtime > max_age:
                return None
            return f.read()
    except IOError:
        return None


def get(url):
    try:
        with urlopen(url) as f:
            return f.read()
    except HTTPError:
        return b''


def cached_get(url, max_age=ONE_DAY):
    fn = cache_filename(url)
    body = get_from_cache(fn, max_age)
    if body is None:
        if not os.path.isdir(CACHE_DIR):
            os.makedirs(CACHE_DIR)
        body = get(url)
        with open(fn, 'wb') as f:
            f.write(body)
    return body


def parse(url):
    body = cached_get(url)
    if not body:
        return lxml.html.Element('html')
    return lxml.html.fromstring(body.decode(), base_url=url)


def tostring(etree):
    return lxml.html.tostring(etree).decode()


class Failure(object):
    def __init__(self, title, url):
        self.title = title
        self.url = url
        self.body = None
        self.etree = None
        self.pre = None
        self.first_link = None
        self.first_link_body = None
        self.buildbot_steps = []
        self.console_text = None

    def __repr__(self):
        return '{0.__class__.__name__}({0.title!r}, {0.url!r})'.format(self)

    def analyze(self):
        self.etree = parse(self.url)
        self.pre = tostring(self.etree.xpath('//pre')[0])
        links = self.etree.xpath('//pre/a')
        if links:
            self.first_link = links[0].get('href')
        if self.first_link and BUILDER_URL.match(self.first_link):
            # always get latest build results please
            self.first_link = self.first_link.rpartition('/')[0] + '/-1'
            self.first_link_etree = parse(self.first_link)
            self.parse_buildbot()
        if self.first_link and JENKINS_URL.match(self.first_link):
            self.first_link = self.first_link.rpartition('/')[0].rpartition('/')[0] + '/lastBuild/'
            self.console_text = cached_get(self.first_link + 'consoleText').decode()

    def parse_buildbot(self):
        for step in self.first_link_etree.cssselect('div.result'):
            css_class = step.get('class') # "success result"|"failure result"
            step_title = step.cssselect('a')[0].text
            step_link_rel = step.cssselect('a')[0].get('href')
            step_link = urljoin(self.first_link, step_link_rel) + '/logs/stdio'
            step_etree = parse(step_link)
            spans = step_etree.cssselect('span.stdout, span.stderr')
            step_text = '<pre>{}</pre>'.format(''.join(map(tostring, spans)))
            self.buildbot_steps.append((step_title, step_link, css_class,
                                        step_text))



CSS = """
.collapsible {
  cursor: pointer;
  margin-bottom: 0;
}
.collapsible:before {
  content: "▼ ";
  color: #888;
}
.collapsible.collapsed:before {
  content: "► ";
}
.collapsible.collapsed + article {
  display: none;
}

h2 {
  background: #da4;
  color: white;
  padding: 4px;
  margin: 12px -8px 0 -8px;
}

a.result {
  padding: 2px;
  text-decoration: none;
}
a.success {
  border: 1px solid #2F8F0F;
  background: #8FDF5F;
  color: white;
}
a.failure {
  border: 1px solid #8F0F0F;
  background: #E98080;
  color: white;
}
pre {
  border: 1px solid #eee;
  background: #f8f8f8;
  border-radius: 4px;
  padding: 6px;
  white-space: pre-wrap;
  margin-top: 6px;
  margin-left: 1em;
}
pre .collapsible {
  background: white;
  border-top: 1px solid #eee;
  border-bottom: 1px solid #eee;
  margin: 0 -6px 0 -6px;
  padding: 4px;
  display: block;
}
pre article {
  border: 1px dotted #eee;
  background: #f0f0f0;
  margin: 0 -6px 0 -6px;
  padding: 0 6px; 0 6px;
}
span.stderr {
  color: red;
}
"""

JQUERY_URL = "http://code.jquery.com/jquery-1.9.1.min.js"

JAVASCRIPT = """
$(function(){
    $('.collapsible').click(function(e) {
        if (e.target.tagName != "A") {
            $(this).toggleClass('collapsed');
        }
    });
    $('#footer').append(' ');
    $('#footer').append($('<a href="#">Expand all</a>').click(function(e){
        e.preventDefault();
        $('h2.collapsible').removeClass('collapsed');
    }))
    $('#footer').append(' ');
    $('#footer').append($('<a href="#">Collapse all</a>').click(function(e){
        e.preventDefault();
        $('h2.collapsible').addClass('collapsed');
    }))
});
"""


class Report:
    def __init__(self):
        self.date = '<unknown date>'
        self.failures = []

    def analyze(self, email_lines):
        self.parse_email(email_lines)
        self.fetch_emails()

    def parse_email(self, email_lines):
        title = '<unknown title>'
        for line in email_lines:
            line = line.rstrip()
            m = DATE_PATTERN.match(line)
            if m:
                self.date = m.group(1)
                continue
            m = TITLE_PATTERN.match(line)
            if m:
                title = m.group(1)
                continue
            m = URL_PATTERN.match(line)
            if m:
                url = m.group(1)
                self.failures.append(Failure(title, url))
                continue

    def fetch_emails(self):
        for failure in self.failures:
            failure.analyze()

    def truncate_pre(self, pre, first=4, last=20, min_middle=5, escape=True):
        if escape:
            pre = '<pre>{}</pre>'.format(html.escape(pre))
        lines = pre.splitlines(True)
        if len(lines) < first+min_middle+last:
            return pre
        first_bit = lines[:first]
        middle_bit = lines[first:-last]
        last_bit = lines[-last:]
        return (''.join(first_bit) +
                '<span class="collapsible collapsed">({} more lines)</span>'
                    .format(len(middle_bit)) +
                '<article>' +
                ''.join(middle_bit) +
                '</article>' +
                ''.join(last_bit))

    def write(self, filename=None):
        if not filename:
            filename = os.path.join(tempfile.mkdtemp(
                prefix='zope-test-janitor-'), 'report.html')
        with open(filename, 'w') as f:
            title = 'Zope tests for {}'.format(self.date)
            f.write(
                '<html>\n'
                '  <head>\n'
                '    <meta charset="UTF-8">\n'
                '    <title>{title}</title>\n'
                '    <style type="text/css">{css}</style>\n'
                '    <script type="text/javascript" src="{jquery}"></script>\n'
                '    <script type="text/javascript">{js}</script>\n'
                '  </head>\n'
                '<body>\n'
                '  <h1>{title}</h1>\n'
                '\n'
                    .format(title=html.escape(title),
                            css=CSS,
                            jquery=JQUERY_URL,
                            js=JAVASCRIPT)
            )
            for failure in self.failures:
                f.write(
                    '  <h2 class="collapsible">{title}</h2>\n'
                    '  <article>\n'
                    '    <p class="{css_class}"><a href="{url}">Summary email</a></p>\n'
                    '    <article>{pre}</article>\n'
                        .format(title=html.escape(failure.title),
                                url=html.escape(failure.url),
                                css_class="collapsible collapsed"
                                                if failure.buildbot_steps or failure.console_text
                                                else "collapsible",
                                pre=self.truncate_pre(failure.pre, escape=False))
                )
                if failure.console_text:
                    f.write('    <p class="collapsible">Console text from last build:</p>\n'
                            '    <article>{}</article>\n'
                                .format(self.truncate_pre(failure.console_text,
                                                          escape=True)))
                if failure.buildbot_steps:
                    f.write('    <p>Buildbot steps from latest build:')
                    for title, url, css_class, text in failure.buildbot_steps:
                        f.write(
                            ' <a class="{css_class}" href="{url}">{title}</a>'
                                .format(title=html.escape(title),
                                        css_class=html.escape(css_class),
                                        url=html.escape(url)))
                    f.write('</p>\n')
                    for title, url, css_class, text in failure.buildbot_steps:
                        f.write('    <p class="collapsible{}">{}</p>\n'
                                '    <article>{}</article>\n'
                                .format("" if "failure" in css_class
                                           else " collapsed",
                                        html.escape(title),
                                        self.truncate_pre(text, escape=False)))
                f.write(
                    '  </article>\n'
                )
            f.write(
                '  <hr>\n'
                '  <p id="footer">{n} failures today.</p>\n'
                '</body>\n'
                '</html>\n'
                    .format(n=len(self.failures))
            )
        return filename


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.strip().partition('\n\n')[-1])
    parser.add_argument('files', metavar='filename', nargs='*')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('--selftest', action='store_true')
    parser.add_argument('--pdb', action='store_true')
    parser.add_argument('--pm', action='store_true')
    args = parser.parse_args()

    if args.selftest:
        del sys.argv[1:]
        unittest.main(defaultTest='test_suite')

    if sys.stdin.isatty() and not args.files:
        parser.error("supply a filename or pipe something to stdin")

    report = Report()
    summary_email = list(fileinput.input(args.files))
    try:
        report.analyze(summary_email)
    except:
        if args.pdb or args.pm:
            import traceback
            traceback.print_exc()
            import pdb; pdb.post_mortem(sys.exc_info()[-1])
        raise
    if args.pdb:
        import pdb; pdb.set_trace()
    filename = report.write()
    if args.verbose:
        print(filename)
    webbrowser.open(filename)


def test_suite():
    return doctest.DocTestSuite()


def doctest_DATE_PATTERN():
    r"""

        >>> m = DATE_PATTERN.match('Date: today')
        >>> m is not None
        True
        >>> list(m.groups())
        ['today']

        >>> m = DATE_PATTERN.match(' Date: today')
        >>> m is None
        True
    """

def doctest_TITLE_PATTERN():
    r"""

        >>> m = TITLE_PATTERN.match('[42] FAIL everything is bad')
        >>> m is not None
        True
        >>> list(m.groups())
        ['FAIL everything is bad']

        >>> m = TITLE_PATTERN.match(
        ...     'Anything else')
        >>> m is None
        True

    """

def doctest_URL_PATTERN():
    r"""

        >>> m = URL_PATTERN.match(
        ...     ' https://mail.zope.org/pipermail/zope-tests/whatever.html')
        >>> m is not None
        True
        >>> list(m.groups())
        ['https://mail.zope.org/pipermail/zope-tests/whatever.html']

        >>> m = URL_PATTERN.match(
        ...     'https://mail.zope.org/pipermail/zope-tests/whatever.html')
        >>> m is None
        True

    """

def doctest_Report_parse_email():
    r"""

        >>> report = Report()
        >>> report.parse_email([
        ...     'Date: today\n',
        ...     '[1] FAIL: everything\n',
        ...     ' https://mail.zope.org/pipermail/zope-tests/whatever.html\n',
        ... ])
        >>> report.failures
        [Failure('FAIL: everything', 'https://mail.zope.org/pipermail/zope-tests/whatever.html')]

    """

if __name__ == '__main__':
    main()

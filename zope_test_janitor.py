#!/usr/bin/python3
"""
Usage: zope-test-janitor < email.txt

Pipe an email from the Zope tests summarizer to it, get back an HTML report.
"""

__version__ = '0.2.2'
__author__ = 'Marius Gedminas <marius@gedmin.as>'
__url__ = 'https://gist.github.com/mgedmin/4995950'
__licence__ = 'GPL v2 or later' # or ask me for MIT

import argparse
import doctest
import fileinput
import html
import logging
import os
import re
import sys
import tempfile
import textwrap
import time
import unittest
import webbrowser
from urllib.request import urlopen
from urllib.parse import quote, urljoin
from urllib.error import HTTPError

import lxml.html


log = logging.getLogger('zope-test-janitor')


DATE_PATTERN = re.compile(
    r'^Date: (.*)$')

TITLE_PATTERN = re.compile(
    r'^(\[\d+\]\s*[A-Z].*)')

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
        log.info('Downloading %s', url)
        with urlopen(url) as f:
            return f.read()
    except HTTPError as e:
        log.debug('Download of %s failed: %s', url, e)
        return b''


def cached_get(url, max_age=ONE_DAY):
    fn = cache_filename(url)
    body = get_from_cache(fn, max_age)
    if body is None:
        if not os.path.isdir(CACHE_DIR):
            log.debug('Creating cache directory %s', CACHE_DIR)
            os.makedirs(CACHE_DIR)
        body = get(url)
        with open(fn, 'wb') as f:
            f.write(body)
    else:
        log.debug('Using cached copy of %s', url)
    return body


def parse(url):
    body = cached_get(url)
    if not body:
        return lxml.html.Element('html')
    return lxml.html.fromstring(body.decode(), base_url=url)


def tostring(etree):
    return lxml.html.tostring(etree).decode()


class Failure(object):

    # public API: information about the error email
    title = None        # subject
    url = None          # link to mailman archive page of the email
    pre = None          # email body text as HTML markup within '<pre>..</pre>'
    # if buildbot/jenkins detected:
    build_number = None             # number of the build
    build_link = None               # link to build page
    console_text = None             # console output, if jenkins
    buildbot_steps = None           # list of buildbot steps, if buildbot
    # peeking to the future
    last_build_link = None          # link to last build page
    last_build_number = None        # number of the last build
    last_console_text = None        # console output, if jenkins
    last_build_steps = None         # list of buildbot steps, if buildbot
    last_build_successful = None    # was the last build successful?

    def __init__(self, title, url):
        self.title = title
        self.url = url

    def __repr__(self):
        return '{0.__class__.__name__}({0.title!r}, {0.url!r})'.format(self)

    def analyze(self):
        self.pre, first_link = self.parse_email(self.url)
        if self.is_buildbot_link(first_link):
            self.build_link, _ = self.parse_buildbot_link(
                first_link, latest=False)
            self.last_build_link, _ = self.parse_buildbot_link(
                first_link, latest=True)
            self.buildbot_steps, self.build_number = \
                    self.parse_buildbot(self.build_link)
            self.last_build_steps, self.last_build_number = \
                    self.parse_buildbot(self.last_build_link,
                                        skip_if=self.build_number,
                                        normalize_url=True)
            self.last_build_successful = self.buildbot_success(
                self.last_build_steps)
        if self.is_jenkins_link(first_link):
            self.build_link, self.build_number = self.parse_jenkins_link(
                first_link, latest=False)
            self.last_build_link, _ = self.parse_jenkins_link(
                first_link, latest=True)
            self.console_text = self.parse_jenkins(self.build_link)
            self.last_build_number = self.parse_jenkins_build_number(
                self.last_build_link)
            if self.last_build_number != self.build_number:
                url = self.normalize_jenkins_url(self.last_build_link,
                                                 self.last_build_number)
                self.last_console_text = self.parse_jenkins(url)
                self.last_build_successful = self.jenkins_success(
                    self.last_console_text)

    def parse_email(self, url):
        etree = parse(url)
        pre = tostring(etree.xpath('//pre')[0])
        links = etree.xpath('//pre/a')
        if links:
            return (pre, links[0].get('href'))
        else:
            return (pre, None)

    def is_buildbot_link(self, url):
        return bool(url and BUILDER_URL.match(url))

    def parse_buildbot_link(self, url, latest):
        # url is '.../buildnumber', i.e. has no trailing slash
        assert self.is_buildbot_link(url)
        if latest:
            return (url.rpartition('/')[0] + '/-1', 'latest')
        else:
            return (url, url.rpartition('/')[-1])

    def normalize_buildbot_url(self, url, build_number):
        assert url.endswith('/-1')
        assert build_number.isdigit()
        return url.rpartition('/')[0] + '/%s' % build_number

    def parse_buildbot(self, url, skip_if=None, normalize_url=False):
        etree = parse(url)
        title = etree.xpath('//title/text()')[0]
        build_number = title.rpartition('#')[-1]
        steps = []
        if skip_if is not None and build_number == skip_if:
            return steps, build_number
        if normalize_url:
            url = self.normalize_buildbot_url(url, build_number)
        for step in etree.cssselect('div.result'):
            css_class = step.get('class') # "success result"|"failure result"
            step_title = step.cssselect('a')[0].text
            step_link_rel = step.cssselect('a')[0].get('href')
            if normalize_url:
                assert step_link_rel.startswith('-1/')
                step_link_rel = '%s/%s' % (build_number,
                                           step_link_rel.partition('/')[-1])
            step_link = urljoin(url, step_link_rel) + '/logs/stdio'
            step_etree = parse(step_link)
            spans = step_etree.cssselect('span.stdout, span.stderr')
            step_text = '<pre>{}</pre>'.format(''.join(map(tostring, spans)))
            steps.append((step_title, step_link, css_class, step_text))
        return steps, build_number

    def buildbot_success(self, steps):
        return all(css_class == "success result"
                   for title, url, css_class, text in steps)

    def is_jenkins_link(self, url):
        return bool(url and JENKINS_URL.match(url))

    def parse_jenkins_link(self, url, latest):
        # url is '.../buildnumber/', i.e. has a trailing slash
        assert self.is_jenkins_link(url)
        if latest:
            return (url.rpartition('/')[0].rpartition('/')[0] + '/lastBuild/',
                    'latest')
        else:
            return (url, url.rpartition('/')[0].rpartition('/')[-1])

    def normalize_jenkins_url(self, url, build_number):
        assert url.endswith('/lastBuild/')
        assert build_number.isdigit()
        return url.rpartition('/')[0].rpartition('/')[0] + '/%s/' % build_number

    def parse_jenkins_build_number(self, url):
        etree = parse(url)
        title = etree.xpath('//title/text()')[0]
        build_number = title.rpartition('#')[-1].partition(' ')[0]
        return build_number

    def parse_jenkins(self, url):
        # url is '.../buildnumber/', i.e. has a trailing slash
        return cached_get(url + 'consoleText').decode()

    def jenkins_success(self, console_text):
        return console_text.rstrip().endswith('Finished: SUCCESS')


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

a.headerlink:link,
a.headerlink:visited {
  visibility: hidden;
  color: #eee;
  text-decoration: none;
}

h2:hover > a.headerlink {
  visibility: visible;
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
  background: #f0f0f0;
  color: green;
  border-top: 1px solid #eee;
  border-bottom: none;
  margin: 0 -6px 0 -6px;
  padding: 4px;
  display: block;
}
pre .collapsible.collapsed {
  border-bottom: 1px solid #eee;
}
pre article {
  border-bottom: 1px solid #eee;
  border-top: none;
  background: #f0f0f0;
  margin: 0 -6px 0 -6px;
  padding: 0 6px; 0 6px;
}
span.stderr {
  color: red;
}
article .steps {
  margin-left: 1em;
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

    def truncate_pre(self, pre, first=4, last=30, min_middle=5, escape=True):
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

    def format_buildbot_steps(self, steps):
        return ' '.join('<a class="{css_class}" href="{url}">{title}</a>'
                                .format(title=html.escape(title),
                                        css_class=html.escape(css_class),
                                        url=html.escape(url))
                        for title, url, css_class, text in steps)

    def emit(self, html, **kw):
        self.f.write(html.format(**kw))

    def page_header(self, title):
        self.emit(textwrap.dedent('''\
            <html>
              <head>
                <meta charset="UTF-8">
                <title>{title}</title>
                <style type="text/css">{css}</style>
                <script type="text/javascript" src="{jquery}"></script>
                <script type="text/javascript">{js}</script>
              </head>
            <body>
              <h1>{title}</h1>
        '''), title=html.escape(title), css=CSS, jquery=JQUERY_URL, js=JAVASCRIPT)

    def failure_header(self, failure, id):
        self.emit(
            '  <h2 id="{id}" class="collapsible">\n'
            '    {title}\n'
            '    <a href="#{id}" class="headerlink">¶</a>\n'
            '  </h2>\n'
            '  <article>\n',
            id=id, title=html.escape(failure.title))

    def summary_email(self, failure):
        self.emit(
            '    <p class="{css_class}"><a href="{url}">Summary email</a></p>\n'
            '    <article>{pre}</article>\n',
            url=html.escape(failure.url),
            css_class="collapsible collapsed"
                            if failure.buildbot_steps or failure.console_text
                            else "collapsible",
            pre=self.truncate_pre(failure.pre, escape=False))

    def console_text(self, title, build, url, text, collapsed=False, **kw):
        self.emit(
            '    <p class="{css_class}">%s</p>\n'
            '    <article>{console_text}</article>\n' % title,
            css_class="collapsible collapsed" if collapsed
                            else "collapsible",
            build=build,
            url=url,
            console_text=self.truncate_pre(text, escape=True),
            **kw)

    def buildbot_steps(self, title, build, url, steps, collapsed=False, **kw):
        self.emit(
            '    <p class="{css_class}">%s</p>'
            '    <article class="steps">\n' % title,
            css_class="collapsible collapsed" if collapsed
                            else "collapsible",
            build=build,
            url=url,
            steps=self.format_buildbot_steps(steps),
            **kw)
        for title, url, css_class, text in steps:
            self.emit(
                '    <p class="{css_class}">{title}</p>\n'
                '    <article>{pre}</article>\n',
                css_class="collapsible" if "failure" in css_class
                                        else "collapsible collapsed",
                title=html.escape(title),
                pre=self.truncate_pre(text, escape=False))
        self.emit(
            '    </article>\n')

    def failure_footer(self):
        self.emit(
            '  </article>\n')

    def page_footer(self):
        self.emit(textwrap.dedent('''\
              <hr>
              <p id="footer">{n} failures today.</p>
            </body>
            </html>
        '''), n=len(self.failures))

    def write(self, filename=None):
        if not filename:
            filename = os.path.join(tempfile.mkdtemp(
                prefix='zope-test-janitor-'), 'report.html')
        with open(filename, 'w') as self.f:
            self.page_header('Zope tests for {}'.format(self.date))
            for n, failure in enumerate(self.failures, 1):
                self.failure_header(failure, 'f{}'.format(n))
                self.summary_email(failure)
                have_last_build = failure.last_build_number != failure.build_number
                if failure.console_text:
                    self.console_text('Console text from <a href="{url}">{build}</a>:',
                                      build='build #%s' % failure.build_number,
                                      url=failure.build_link,
                                      text=failure.console_text,
                                      collapsed=have_last_build)
                    if have_last_build:
                        self.console_text('<a href="{url}">{build}</a> was {successful}:',
                                          build='Last build (#%s)' % failure.last_build_number,
                                          successful="successful" if failure.last_build_successful
                                                     else "also unsuccessful",
                                          url=failure.last_build_link,
                                          text=failure.last_console_text,
                                          collapsed=failure.last_build_successful)
                if failure.buildbot_steps:
                    self.buildbot_steps('Buildbot steps from <a href="{url}">{build}</a>: {steps}',
                                        build='build #%s' % failure.build_number,
                                        url=failure.build_link,
                                        steps=failure.buildbot_steps,
                                        collapsed=have_last_build)
                    if have_last_build:
                        self.buildbot_steps('<a href="{url}">{build}</a> was {successful}: {steps}',
                                            build='Last build (#%s)' % failure.last_build_number,
                                            successful="successful" if failure.last_build_successful
                                                       else "also unsuccessful",
                                            url=failure.last_build_link,
                                            steps=failure.last_build_steps,
                                            collapsed=failure.last_build_successful)
                self.failure_footer()
            self.page_footer()
        return filename


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.strip().partition('\n\n')[-1])
    parser.add_argument('files', metavar='filename', nargs='*')
    parser.add_argument('-v', '--verbose', action='count', default=1)
    parser.add_argument('-q', '--quiet', action='count', default=0)
    parser.add_argument('--selftest', action='store_true')
    parser.add_argument('--pdb', action='store_true')
    parser.add_argument('--pm', action='store_true')
    args = parser.parse_args()

    if args.selftest:
        del sys.argv[1:]
        unittest.main(defaultTest='test_suite')

    if sys.stdin.isatty() and not args.files:
        parser.error("supply a filename or pipe something to stdin")

    root = logging.getLogger()
    root.addHandler(logging.StreamHandler(sys.stdout))
    verbosity = args.verbose - args.quiet
    root.setLevel(logging.ERROR if verbosity < 1 else
                  logging.INFO if verbosity == 1 else
                  logging.DEBUG)

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
    log.debug("Created %s", filename)
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
        ['[42] FAIL everything is bad']

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

def doctest_Failure_is_buildbot_link():
    """Test for Failure.is_buildbot_link

        >>> f = Failure(None, None)
        >>> f.is_buildbot_link('http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/185')
        True

        >>> f.is_buildbot_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184/')
        False

    """

def doctest_Failure_parse_buildbot_link():
    """Test for Failure.parse_buildbot_link

        >>> f = Failure(None, None)
        >>> f.parse_buildbot_link('http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/185', latest=False)
        ('http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/185', '185')

        >>> f.parse_buildbot_link('http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/185', latest=True)
        ('http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/-1', 'latest')

    """

def doctest_Failure_is_jenkins_link():
    """Test for Failure.is_jenkins_link

        >>> f = Failure(None, None)
        >>> f.is_jenkins_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184/')
        True

    Currently we assume the URL has a trailing slash

        >>> f.is_jenkins_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184')
        False

    """

def doctest_Failure_parse_jenkins_link():
    """Test for Failure.parse_jenkins_link

        >>> f = Failure(None, None)
        >>> f.parse_jenkins_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184/', latest=False)
        ('http://jenkins.starzel.de/job/zopetoolkit_trunk/184/', '184')

        >>> f.parse_jenkins_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184/', latest=True)
        ('http://jenkins.starzel.de/job/zopetoolkit_trunk/lastBuild/', 'latest')

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
        [Failure('[1] FAIL: everything', 'https://mail.zope.org/pipermail/zope-tests/whatever.html')]

    """

if __name__ == '__main__':
    main()

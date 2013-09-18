#!/usr/bin/python
# -*- coding: UTF-8 -*-
"""
Usage: zope-test-janitor [-v|-q] [filename]

Pipe an email from the Zope tests summarizer to it, get back an HTML report.
"""

from __future__ import unicode_literals

__version__ = '0.5.3'
__author__ = 'Marius Gedminas <marius@gedmin.as>'
__url__ = 'https://gist.github.com/mgedmin/4995950'
__licence__ = 'GPL v2 or later' # or ask me for MIT

import argparse
import doctest
import fileinput
import logging
import os
import re
import socket
import sys
import tempfile
import textwrap
import time
import unittest
import webbrowser
from collections import namedtuple
from contextlib import closing

try:
    from urllib.request import urlopen
    from urllib.parse import quote, urljoin
    from urllib.error import HTTPError
except ImportError:
    from urllib import urlopen, quote
    from urlparse import urljoin
    HTTPError = IOError

try:
    from html import escape
except ImportError:
    from cgi import escape

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


KNOWN_FAILURES = [
    ("Error: Couldn't open /home/zope/.jenkins/jobs/zopetoolkit_trunk/workspace/development-python.cfg",
     "bad jenkins config"),
    ("ERROR: 'xslt-config' is not recognized as an internal or external command",
     "no lxml on winbot"),
    ("A    MOVED_TO_GITHUB",
     "moved to Github"),
    (r"IOError: [Errno 2] No such file or directory: 'setuptools\\cli.exe'",
     "distribute issue #376"),
    (r"IOError: [Errno 0] Error: 'setuptools\\cli.exe'",
     "distribute issue #376"),
    (re.compile(r"pkg_resources\.VersionConflict: \(setuptools .*, Requirement\.parse\('setuptools&gt;=0\.7b'\)\)"),
     "setuptools issue #5"), # https://bitbucket.org/pypa/setuptools/issue/5/distribute_setuppy-fails-with
    (re.compile("Error: There is a version conflict.\s+We already have: webtest \d"),
     "webtest version conflict"),
    ('fatal: unable to connect to github.com:',
     "github unreachable"),
]


def cache_filename(url):
    return os.path.join(CACHE_DIR, quote(url, safe=''))


ONE_HOUR = 60*60 # seconds
ONE_DAY = 24*ONE_HOUR


def get_from_cache(filename, max_age):
    try:
        with open(filename, 'rb') as f:
            mtime = os.fstat(f.fileno()).st_mtime
            age = time.time() - mtime
            if age > max_age:
                return None
            return f.read()
    except IOError:
        return None


def get(url):
    try:
        log.info('Downloading %s', url)
        with closing(urlopen(url)) as f:
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


def parse(url, max_age=ONE_DAY):
    body = cached_get(url, max_age=max_age)
    if not body:
        return lxml.html.Element('html')
    return lxml.html.fromstring(body.decode('UTF-8'), base_url=url)


def tostring(etree):
    return lxml.html.tostring(etree).decode()


BuildStep = namedtuple('BuildStep', 'title link css_class text')


class Failure(object):

    # public API: information about the error email
    title = None        # subject
    url = None          # link to mailman archive page of the email
    pre = None          # email body text as HTML markup within '<pre>..</pre>'
    # if buildbot/jenkins detected:
    build_number = None             # number of the build
    build_link = None               # link to build page
    build_source = None             # source tree infomration of the build
    console_text = None             # console output, if jenkins
    buildbot_steps = None           # list of buildbot steps, if buildbot
    # peeking to the future
    last_build_link = None          # link to last build page
    last_build_number = None        # number of the last build
    last_build_source = None        # source tree infomration of the last build
    last_console_text = None        # console output, if jenkins
    last_build_steps = None         # list of buildbot steps, if buildbot
    last_build_successful = None    # was the last build successful?
    # summary
    tag = None                      # known failure tag

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
            self.build_source = self.buildbot_source(
                self.buildbot_steps)
            self.last_build_steps, self.last_build_number = \
                    self.parse_buildbot(self.last_build_link,
                                        skip_if=self.build_number,
                                        max_age=ONE_HOUR,
                                        normalize_url=True)
            self.last_build_successful = self.buildbot_success(
                self.last_build_steps)
            self.last_build_source = self.buildbot_source(
                self.last_build_steps)
            if int(self.last_build_number) < int(self.build_number):
                log.warning("Last build (%s) older than current build (%s)?!\n%s",
                            self.last_build_number, self.build_number,
                            self.last_build_link)
        if self.is_jenkins_link(first_link):
            self.build_link, self.build_number = self.parse_jenkins_link(
                first_link, latest=False)
            self.last_build_link, _ = self.parse_jenkins_link(
                first_link, latest=True)
            self.console_text = self.parse_jenkins(self.build_link)
            self.last_build_number = self.parse_jenkins_build_number(
                self.last_build_link, max_age=ONE_HOUR)
            if self.last_build_number and self.last_build_number != self.build_number:
                url = self.normalize_jenkins_url(self.last_build_link,
                                                 self.last_build_number)
                self.last_console_text = self.parse_jenkins(url)
                self.last_build_successful = self.jenkins_success(
                    self.last_console_text)
        self.look_for_known_failures()

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

    def parse_buildbot(self, url, skip_if=None, normalize_url=False,
                       max_age=ONE_DAY):
        etree = parse(url, max_age=max_age)
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
            step_text = self.prepare_step_text(step_etree)
            steps.append(BuildStep(step_title, step_link, css_class, step_text))
        return steps, build_number

    def prepare_step_text(self, step_etree):
        spans = step_etree.cssselect('span.stdout, span.stderr')
        step_meta = step_etree.cssselect('span.header')
        command_line = exit_status = ''
        if len(step_meta) >= 1:
            first_line = step_meta[0].text.split('\n')[0].rstrip()
            command_line = '<span class="header">{}</span>\n'.format(
                escape(first_line))
        if len(step_meta) >= 2:
            first_line = step_meta[-1].text.split('\n')[0].rstrip()
            exit_status = '<span class="header">{}</span>\n'.format(
                escape(first_line))
        return '<pre>{}</pre>'.format(''.join(
            [command_line] + list(map(tostring, spans)) + [exit_status]))

    def buildbot_success(self, steps):
        return steps and all(step.css_class == "success result"
                             for step in steps)

    def buildbot_source(self, steps):
        github_rx = re.compile('From [a-z]+://github[.]com/([a-zA-Z0-9_.]+/[a-zA-Z0-9_.]+)')
        commit_rx = re.compile('HEAD is now at ([0-9a-f]+)')
        github_repo = commit = None
        for step in steps:
            if step.title == 'git':
                m = github_rx.search(step.text)
                if m:
                    github_repo = m.group(1)
                    if github_repo.endswith('.git'):
                        github_repo = github_repo[:-len('.git')]
                m = commit_rx.search(step.text)
                if m:
                    commit = m.group(1)
        if github_repo and commit:
            return GitHubSource(github_repo, commit)
        else:
            return None

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

    def parse_jenkins_build_number(self, url, max_age=ONE_HOUR):
        etree = parse(url, max_age=max_age)
        try:
            title = etree.xpath('//title/text()')[0]
        except IndexError:
            return None
        build_number = title.rpartition('#')[-1].partition(' ')[0]
        return build_number

    def parse_jenkins(self, url, max_age=ONE_DAY):
        # url is '.../buildnumber/', i.e. has a trailing slash
        return cached_get(url + 'consoleText', max_age=max_age).decode('UTF-8', 'replace')

    def jenkins_success(self, console_text):
        return console_text.rstrip().endswith('Finished: SUCCESS')

    def look_for_known_failures(self):
        if self.last_build_successful:
            self.tag = 'last build successful'
            return
        if self.last_console_text:
            self.analyze_text(self.last_console_text)
        else:
            self.analyze_text(self.console_text)
        if self.last_build_steps:
            self.analyze_steps(self.last_build_steps)
        else:
            self.analyze_steps(self.buildbot_steps)

    def analyze_steps(self, steps):
        if not steps: # could be None
            return
        for step in steps:
            self.analyze_text(step.text)

    def analyze_text(self, text):
        if not text:
            return
        for sign, tag in KNOWN_FAILURES:
            if hasattr(sign, 'search'): # regexp!
                if sign.search(text):
                    self.tag = tag
            else:
                if sign in text:
                    self.tag = tag


class GitHubSource(object):

    def __init__(self, repo, commit):
        self.repo = repo
        self.commit = commit

    def get_revision(self):
        return 'commit %s' % self.commit

    def get_url(self):
        return 'https://github.com/{repo}/commit/{commit}'.format(
            repo=self.repo, commit=self.commit)

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.repo, self.commit)


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
span.error {
  color: red;
}
span.header {
  color: #888;
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

    def format_console_text(self, text):
        return '<pre>{}</pre>'.format(
            re.sub(r'^([+].*)',
                   r'<span class="section">\1</span>',
                   re.sub(r'^(Traceback.*(\n .*)*\n[^ ].*|ERROR:.*)',
                          r'<span class="error">\1</span>',
                          escape(text),
                          flags=re.MULTILINE,
                         ),
                      flags=re.MULTILINE,
                  )
        )

    def split_to_sections(self, lines):
        pending = []
        result = []
        for line in lines:
            if line.startswith('<span class="section">'):
                if pending:
                    result.append(pending)
                    pending = []
            pending.append(line)
        if pending:
            result.append(pending)
        return result

    def collapsed_text(self, lines):
        n_errors = sum(1 for line in lines if '<span class="error">' in line)
        title = '%d more lines' % len(lines)
        if n_errors == 1:
            title += ' and 1 error'
        elif n_errors:
            title += ' and %d errors' % n_errors
        return ('<span class="collapsible collapsed">({})</span>'
                    .format(title) +
                '<article>' +
                ''.join(lines) +
                '</article>')

    def truncate_pre(self, pre, first=4, last=30, min_middle=5):
        lines = pre.strip().splitlines(True)
        if len(lines) < first+min_middle+last:
            return pre
        first_bit = lines[:first]
        middle_bit = lines[first:-last]
        last_bit = lines[-last:]
        result = first_bit
        for section in self.split_to_sections(middle_bit):
            if section[0].startswith('<span class="section">'):
                result += [section[0], self.collapsed_text(section[1:])]
            else:
                result += [self.collapsed_text(section)]
        result += last_bit
        return ''.join(result)

    def format_buildbot_steps(self, steps):
        return ' '.join('<a class="{css_class}" href="{url}">{title}</a>'
                                .format(title=escape(step.title),
                                        css_class=escape(step.css_class),
                                        url=escape(step.link))
                        for step in steps)

    def format_source(self, source, prefix='', suffix=''):
        if not source:
            return ''
        return '{prefix}<a href="{url}">{revision}</a>{suffix}'.format(
            prefix=prefix, suffix=suffix,
            url=source.get_url(),
            revision=source.get_revision())

    def emit(self, html, **kw):
        self.f.write(html.format(**kw).encode('UTF-8'))

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
        '''), title=escape(title), css=CSS, jquery=JQUERY_URL, js=JAVASCRIPT)

    def failure_header(self, failure, id):
        title = failure.title
        if failure.tag:
            title += ' - ' + failure.tag
        self.emit(
            '  <h2 id="{id}" class="{css_class}">\n'
            '    {title}\n'
            '    <a href="#{id}" class="headerlink">¶</a>\n'
            '  </h2>\n'
            '  <article>\n',
            id=id,
            css_class="collapsible collapsed" if failure.tag else "collapsible",
            title=escape(title))

    def summary_email(self, failure):
        self.emit(
            '    <p class="{css_class}"><a href="{url}">Summary email</a></p>\n'
            '    <article>{pre}</article>\n',
            url=escape(failure.url),
            css_class="collapsible collapsed"
                            if failure.buildbot_steps or failure.console_text
                            else "collapsible",
            pre=self.truncate_pre(failure.pre))

    def console_text(self, title, build, url, text, collapsed=False, **kw):
        self.emit(
            '    <p class="{css_class}">%s</p>\n'
            '    <article>{console_text}</article>\n' % title,
            css_class="collapsible collapsed" if collapsed
                            else "collapsible",
            build=build,
            url=url,
            console_text=self.truncate_pre(self.format_console_text(text)),
            **kw)

    def buildbot_steps(self, title, build, url, steps, collapsed=False,
                       source=None, **kw):
        self.emit(
            '    <p class="{css_class}">%s{source}</p>'
            '    <article class="steps">\n' % title,
            css_class="collapsible collapsed" if collapsed
                            else "collapsible",
            build=build,
            url=url,
            steps=self.format_buildbot_steps(steps),
            source=self.format_source(source, prefix=' (', suffix=')'),
            **kw)
        for step in steps:
            self.emit(
                '    <p class="{css_class}">{title}</p>\n'
                '    <article>{pre}</article>\n',
                css_class="collapsible" if "failure" in step.css_class
                                        else "collapsible collapsed",
                title=escape(step.title),
                pre=self.truncate_pre(step.text))
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
                have_last_build = (failure.last_build_number and
                                   failure.last_build_number != failure.build_number)
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
                                        source=failure.build_source,
                                        steps=failure.buildbot_steps,
                                        collapsed=have_last_build)
                    if have_last_build:
                        self.buildbot_steps('<a href="{url}">{build}</a> was {successful}: {steps}',
                                            build='Last build (#%s)' % failure.last_build_number,
                                            successful="successful" if failure.last_build_successful
                                                       else "also unsuccessful",
                                            url=failure.last_build_link,
                                            source=failure.last_build_source,
                                            steps=failure.last_build_steps,
                                            collapsed=failure.last_build_successful)
                self.failure_footer()
            self.page_footer()
        return filename


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.strip().partition('\n\n')[-1])
    parser.add_argument('files', metavar='filename', nargs='*')
    parser.add_argument('--version', action='version',
                        version="%(prog)s version " + __version__)
    parser.add_argument('-v', '--verbose', action='count', default=1)
    parser.add_argument('-q', '--quiet', action='count', default=0)
    parser.add_argument('--timeout', type=float, default=30)
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

    socket.setdefaulttimeout(args.timeout)

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
    return doctest.DocTestSuite(optionflags=doctest.REPORT_NDIFF)


def doctest_DATE_PATTERN():
    r"""

        >>> m = DATE_PATTERN.match('Date: today')
        >>> m is not None
        True
        >>> list(m.groups())
        [u'today']

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
        [u'[42] FAIL everything is bad']

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
        [u'https://mail.zope.org/pipermail/zope-tests/whatever.html']

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
        (u'http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/185', u'185')

        >>> f.parse_buildbot_link('http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/185', latest=True)
        (u'http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/-1', u'latest')

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
        (u'http://jenkins.starzel.de/job/zopetoolkit_trunk/184/', u'184')

        >>> f.parse_jenkins_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184/', latest=True)
        (u'http://jenkins.starzel.de/job/zopetoolkit_trunk/lastBuild/', u'latest')

    """

def doctest_Failure_buildbot_source():
    """Test for Failure.buildbot_source

        >>> f = Failure(None, None)
        >>> f.buildbot_source([])

        >>> f.buildbot_source([BuildStep(title='git', link=None, css_class='',
        ...                              text='''
        ... <pre><span class="header">starting git operation</span>
        ... <span class="stderr">From git://github.com/zopefoundation/ZODB
        ...  * branch            HEAD       -&gt; FETCH_HEAD
        ... </span><span class="stdout">HEAD is now at 6b484f8 Correctly quote Windows pathnames
        ... </span><span class="stderr">warning: refname 'HEAD' is ambiguous.
        ... </span><span class="stdout">6b484f8a2ce6cd627139cd6a2c8e9219ecf0ecf2
        ... </span><span class="stderr">warning: refname 'HEAD' is ambiguous.
        ... </span><span class="header">elapsedTime=0.407000</span>
        ... </pre>
        ... ''')])
        GitHubSource(u'zopefoundation/ZODB', u'6b484f8')

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
        [Failure(u'[1] FAIL: everything', u'https://mail.zope.org/pipermail/zope-tests/whatever.html')]

    """

def doctest_Report_format_source():
    """Test for Report.format_source

        >>> report = Report()
        >>> report.format_source(None, prefix='lalala')
        u''

        >>> report.format_source(GitHubSource(u'zopefoundation/ZODB', u'6b484f8'), prefix=' (', suffix=')')
        u' (<a href="https://github.com/zopefoundation/ZODB/commit/6b484f8">commit 6b484f8</a>)'

    """

def doctest_Report_format_console_text():
    """Test for Report.format_console_text

        >>> report = Report()
        >>> text = '''
        ... + bin/test
        ... blah blah blah
        ... also <hehe markup> & stuff
        ... when suddenly
        ... Traceback (most recent call last):
        ...   File something something
        ...     code code
        ... Exception: something happen!
        ... and continued
        ... '''
        >>> print(report.format_console_text(text))
        <pre>
        <span class="section">+ bin/test</span>
        blah blah blah
        also &lt;hehe markup&gt; &amp; stuff
        when suddenly
        <span class="error">Traceback (most recent call last):
          File something something
            code code
        Exception: something happen!</span>
        and continued
        </pre>

    """

def doctest_Report_split_to_sections():
    """Test for Report.split_to_sections

        >>> report = Report()
        >>> text = '''
        ... blah
        ... <span class="section">+ bin/test</span>
        ... blah blah blah
        ... more blah
        ... <span class="section">+ bin/test --more</span>
        ... blah blah
        ... etc.
        ... '''.lstrip()
        >>> from pprint import pprint
        >>> pprint(report.split_to_sections(text.splitlines()), width=40)
        [[u'blah'],
         [u'<span class="section">+ bin/test</span>',
          u'blah blah blah',
          u'more blah'],
         [u'<span class="section">+ bin/test --more</span>',
          u'blah blah',
          u'etc.']]

    """

def doctest_Report_collapsed_text():
    r"""Test for Report.collapsed_text

        >>> report = Report()
        >>> print(report.collapsed_text(['a\n', 'b\n', 'c\n']))
        <span class="collapsible collapsed">(3 more lines)</span><article>a
        b
        c
        </article>

    """

def doctest_Report_truncate_pre():
    r"""Test for Report.truncate_pre

        >>> report = Report()
        >>> pre = '''<pre>Here
        ... is
        ... some
        ... text:
        ... a
        ... b
        ... c
        ... d
        ... e</pre>
        ...
        ...
        ... '''
        >>> print(report.truncate_pre(pre, first=4, min_middle=1, last=1))
        <pre>Here
        is
        some
        text:
        <span class="collapsible collapsed">(4 more lines)</span><article>a
        b
        c
        d
        </article>e</pre>

    """

if __name__ == '__main__':
    main()
